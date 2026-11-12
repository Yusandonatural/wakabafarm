#!/usr/bin/env python3
"""ダウンロード済み画像の品質チェック。

- 破損・読み込み不可
- 解像度（Shopify 推奨: 商品画像 2048px 四方、最低でも長辺 800px 程度）
- ファイル容量（2MB 超は圧縮推奨）
- 重複画像（知覚ハッシュで同一・ほぼ同一を検出）
- 画像形式（HEIC 等 Web 非対応形式）
出力: data/audit/images_audit.csv, data/audit/images_duplicates.csv
"""
import csv
import collections
from pathlib import Path

import imagehash
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "data" / "images"
OUT = ROOT / "data" / "audit"
OUT.mkdir(parents=True, exist_ok=True)

manifest = {}
with open(ROOT / "data" / "images_manifest.csv", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        manifest[r["original_url"]] = r

rows, hashes = [], collections.defaultdict(list)
for url, m in manifest.items():
    kind = m["区分(サイト共通/商品/固定ページ/記事)"]
    local = m["local_file"]
    rec = {"original_url": url, "local_file": local, "区分": kind, "used_on_count": m["used_on_count"],
           "used_on": m["used_on (先頭5件)"], "status": "", "format": "", "width": "", "height": "",
           "MB": "", "指摘": ""}
    issues = []
    if not local:
        host = url.split("/")[2]
        rec["status"] = "未取得"
        issues.append("外部サイトの画像（%s）。新サイトでは使用不可・差し替え要" % host
                      if "wakaba.jpn.com" not in host else "旧サーバーから取得できず（リンク切れの可能性）")
        rec["指摘"] = " / ".join(issues)
        rows.append(rec)
        continue
    p = ROOT / local
    if p.suffix.lower() == ".pdf" or p.suffix.lower() == ".svg":
        rec.update(status="OK", format=p.suffix[1:].upper(), MB=round(p.stat().st_size / 1048576, 2))
        rows.append(rec)
        continue
    try:
        with Image.open(p) as im:
            im.load()
            fmt = im.format
            im2 = ImageOps.exif_transpose(im)
            w, h = im2.size
            rotated = im2.size != im.size
            hsh = imagehash.phash(im2.convert("RGB"))
    except Exception as e:
        rec.update(status="破損", 指摘="画像として開けません（%s）" % type(e).__name__)
        rows.append(rec)
        continue
    mb = p.stat().st_size / 1048576
    rec.update(status="OK", format=fmt, width=w, height=h, MB=round(mb, 2))
    long_side = max(w, h)
    if kind.startswith("商品") and long_side < 1000:
        issues.append(f"商品画像としては小さい（長辺{long_side}px）→ 新規撮影推奨")
    elif not kind.startswith("サイト共通") and long_side < 600:
        issues.append(f"解像度が低い（長辺{long_side}px）")
    if mb > 2:
        issues.append(f"容量が大きい（{mb:.1f}MB）→ 圧縮推奨")
    if fmt not in ("JPEG", "PNG", "GIF", "WEBP"):
        issues.append(f"Web非推奨形式（{fmt}）")
    if rotated:
        issues.append("EXIFで回転指定あり（表示の向きに注意）")
    if kind.startswith("商品") and w and h and abs(w - h) / max(w, h) > 0.15:
        issues.append(f"商品画像の縦横比が不揃い（{w}x{h}）→ 正方形に統一推奨")
    rec["指摘"] = " / ".join(issues)
    hashes[str(hsh)].append(url)
    rows.append(rec)

with open(OUT / "images_audit.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

dups = [v for v in hashes.values() if len(v) > 1]
with open(OUT / "images_duplicates.csv", "w", newline="", encoding="utf-8") as f:
    wr = csv.writer(f)
    wr.writerow(["group", "count", "original_url"])
    for i, g in enumerate(sorted(dups, key=len, reverse=True), 1):
        for u in g:
            wr.writerow([i, len(g), u])

c = collections.Counter(r["status"] for r in rows)
print("status:", dict(c))
print("duplicate groups:", len(dups), "files:", sum(len(g) for g in dups))
issue_c = collections.Counter()
for r in rows:
    for s in filter(None, r["指摘"].split(" / ")):
        issue_c[s.split("（")[0]] += 1
for k, v in issue_c.most_common():
    print(f"  {v:5d}  {k}")
