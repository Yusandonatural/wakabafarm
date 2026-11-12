#!/usr/bin/env python3
"""文章チェック（校正結果 CSV）と画像チェック結果を統合し、
data/audit/content_review.csv と data/audit/移行前チェック一覧.xlsx を作成する。

使い方: python3 scripts/build_review.py <out_A.csv> <out_B.csv> <out_C.csv>
"""
import csv
import json
import re
import sys
import collections
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "data" / "audit"

FIX = [  # 表示ガイドラインの記述を正確な表現に揃える
    ("「無農薬」「無肥料」等の表示は使用禁止事項", "「無農薬」「無化学肥料」等の表示は禁止事項（「無肥料」も誤認を避けるため言い換え推奨）"),
    ("「無農薬」「無肥料」表示はガイドライン上の禁止表示", "「無農薬」はガイドライン上の禁止表示（「無肥料」も言い換え推奨）"),
]

rows = []
for f in sys.argv[1:]:
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            for a, b in FIX:
                r["修正案"] = r["修正案"].replace(a, b)
            rows.append(r)
prio = {"高": 0, "中": 1, "低": 2}
kind = {"商品": 0, "固定ページ": 1, "記事": 2}
rows.sort(key=lambda r: (prio.get(r["優先度"], 9), kind.get(r["対象種別"], 9), r["区分"]))

# ---- 記事全体の構造的な指摘（スクリプトで集計）
topics = json.load(open(ROOT / "data/topics/topics.json", encoding="utf-8"))
inv = re.compile("[⁡​‌‍⁠﻿]")
lineup = [t for t in topics if "お野菜ラインナップ" in t["categories"]]
titles = collections.Counter(t["title"] for t in topics)
summary = [
    ("記事の構成", f"「お野菜ラインナップ」（発送ごとのセット内容）が {len(lineup)} 件と全体の約7割。"
     "内容が短く日付依存のため、全件移行はおすすめしません（直近1年分のみ、またはブログ化せずトップに「今週のお野菜」を表示）", "高"),
    ("タイトル重複", f"同じタイトルの記事が {sum(v for v in titles.values() if v > 1)} 件（例：「4月8日発送 お野菜セットラインナップ」が5件）。"
     "年が入っていないため。移行時はタイトルに年を付ける", "中"),
    ("Facebook 由来の画像", "368 記事に Facebook のサーバー上の画像が埋め込まれているが、移行対象外とする（確認済み・対応不要）。"
     "移行時は該当の画像タグを本文から自動で除去する", "低"),
    ("不可視文字", f"{sum(1 for t in topics if inv.search(t['body_text'] + t['title']))} 記事に Instagram 由来の不可視文字（U+2061 等）が混入。移行時に一括削除可能", "低"),
    ("URL（スラッグ）", f"日本語や絵文字を含む URL が {sum(1 for t in topics if re.search(r'[^\x00-\x7f]', t['slug']))} 件、"
     f"「-2-2-2…」と連なる URL が {sum(1 for t in topics if re.search(r'(-2){3,}', t['slug']))} 件。Shopify では英数字のハンドルに付け替え、旧URLから301リダイレクトを設定", "中"),
    ("本文なし", f"本文テキストが空または30文字未満の記事が {sum(1 for t in topics if len(t['body_text']) < 30)} 件（画像のみ等）", "中"),
    ("カテゴリ未設定", f"{sum(1 for t in topics if not t['categories'])} 件。旧サイトの新着情報一覧では PHP の警告が表示されている", "低"),
    ("取得できなかった記事", "サイトマップ 1,483 件中 1 件は旧サーバーのエラーで取得できず（scripts/scrape.py の再実行で再取得可）", "低"),
]

# ---- CSV（Excel で文字化けしないよう BOM 付き）
cols = ["No", "優先度", "対象種別", "区分", "タイトル", "該当箇所", "修正案", "URL", "対応状況", "メモ"]
with open(AUDIT / "content_review.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for i, r in enumerate(rows, 1):
        w.writerow({"No": i, **{c: r.get(c, "") for c in cols[1:8]}, "対応状況": "", "メモ": ""})

# ---- Excel
wb = Workbook()
head_fill = PatternFill("solid", fgColor="2F5D3A")
head_font = Font(color="FFFFFF", bold=True)
pfill = {"高": PatternFill("solid", fgColor="F8D7DA"), "中": PatternFill("solid", fgColor="FFF3CD")}


def sheet(ws, header, data, widths, prio_col=None):
    ws.append(header)
    for c in ws[1]:
        c.fill, c.font = head_fill, head_font
    for d in data:
        ws.append(d)
    for i, wdt in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = wdt
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if prio_col is not None and row[prio_col].value in pfill:
            row[prio_col].fill = pfill[row[prio_col].value]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


ws = wb.active
ws.title = "概要"
cnt = collections.Counter((r["優先度"], r["区分"]) for r in rows)
ws.append(["若葉農園 旧サイト 移行前チェック一覧"])
ws["A1"].font = Font(bold=True, size=14)
ws.append([])
ws.append(["シート", "内容"])
for a, b in [("記事全体の傾向", "記事全体に共通する構造的な問題（スクリプトで集計）"),
             ("文章の指摘", f"誤字脱字・表現リスク・古い情報など {len(rows)} 件（商品・固定ページ・記事を人の目で確認）"),
             ("画像の指摘", "画像ごとの解像度・容量・取得可否などのチェック結果"),
             ("重複画像", "同じ（ほぼ同じ）画像のグループ")]:
    ws.append([a, b])
ws.append([])
ws.append(["区分", "高", "中", "低", "計"])
for k in sorted({r["区分"] for r in rows}):
    vals = [cnt[(p, k)] for p in "高中低"]
    ws.append([k, *vals, sum(vals)])
ws.column_dimensions["A"].width = 22
ws.column_dimensions["B"].width = 80

sheet(wb.create_sheet("記事全体の傾向"), ["項目", "内容", "優先度"], summary, [18, 100, 8], prio_col=2)
sheet(wb.create_sheet("文章の指摘"), cols,
      [[i, r["優先度"], r["対象種別"], r["区分"], r["タイトル"], r["該当箇所"], r["修正案"], r["URL"], "", ""]
       for i, r in enumerate(rows, 1)],
      [6, 7, 10, 12, 30, 45, 60, 30, 12, 20], prio_col=1)

img = list(csv.DictReader(open(AUDIT / "images_audit.csv", encoding="utf-8")))
img_rows = [r for r in img if r["指摘"]]
img_rows.sort(key=lambda r: ({"商品": 0, "サイト共通(デザイン素材)": 1, "固定ページ": 2}.get(r["区分"], 3), r["status"]))
sheet(wb.create_sheet("画像の指摘"),
      ["区分", "状態", "指摘", "幅", "高さ", "MB", "画像URL", "使用ページ", "著作権確認", "対応方針"],
      [[r["区分"], r["status"], r["指摘"], r["width"], r["height"], r["MB"], r["original_url"], r["used_on"], "", ""]
       for r in img_rows],
      [14, 8, 50, 7, 7, 6, 50, 50, 14, 16])
dups = list(csv.DictReader(open(AUDIT / "images_duplicates.csv", encoding="utf-8")))
sheet(wb.create_sheet("重複画像"), ["グループ", "枚数", "画像URL"],
      [[d["group"], d["count"], d["original_url"]] for d in dups], [10, 8, 90])

wb.save(AUDIT / "移行前チェック一覧.xlsx")
print("rows", len(rows), dict(collections.Counter(r["優先度"] for r in rows)))
