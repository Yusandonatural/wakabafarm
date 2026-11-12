#!/usr/bin/env python3
"""旧サイトの記事（お野菜ラインナップ以外）を Shopify ブログへ下書きとして投入する。

  python3 scripts/shopify_blog_import.py --dry-run        # 変換結果だけ確認（API 不使用）
  python3 scripts/shopify_blog_import.py --limit 3        # 3件だけ投入して確認
  python3 scripts/shopify_blog_import.py                  # 全件投入（途中から再開可）

必要: 環境変数 SHOPIFY_SHOP（例 n4yjhv-dx.myshopify.com）。
アクセストークンはネットワークプロキシが X-Shopify-Access-Token ヘッダーに付与する前提。
（ローカルで使う場合は SHOPIFY_TOKEN を設定）

処理内容
1. 本文 HTML を整理（Facebook 画像・不可視文字・WordPress 由来の属性を除去）
2. 本文中の旧サイト画像を Shopify の「ファイル」に取り込み、URL を差し替え
3. 記事を「下書き（非公開）」で作成。元の投稿日・カテゴリ（タグ）を保持
4. 旧 URL → 新 URL の対応表を data/shopify/blog_import_log.csv に記録（リダイレクト設定用）
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from bs4 import BeautifulSoup, Comment

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "shopify"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "blog_import_log.csv"
IMGMAP = OUT / "image_map.json"

SHOP = os.environ.get("SHOPIFY_SHOP", "n4yjhv-dx.myshopify.com")
API = f"https://{SHOP}/admin/api/2025-10/graphql.json"
HEADERS = {"Content-Type": "application/json"}
if os.environ.get("SHOPIFY_TOKEN"):
    HEADERS["X-Shopify-Access-Token"] = os.environ["SHOPIFY_TOKEN"]
else:
    HEADERS["X-Shopify-Access-Token"] = "placeholder-replaced-by-proxy"

EXCLUDE_CATS = {"お野菜ラインナップ"}
INVISIBLE = re.compile("[⁡​‌‍⁠﻿]")
KEEP_ATTRS = {"a": {"href", "target", "rel"}, "img": {"src", "alt", "width", "height"},
              "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}


def gql(query, variables=None, tries=5):
    for i in range(tries):
        r = requests.post(API, headers=HEADERS, json={"query": query, "variables": variables or {}}, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 ** i)
            continue
        r.raise_for_status()
        d = r.json()
        if d.get("errors"):
            if any("THROTTLED" in json.dumps(e) for e in d["errors"]):
                time.sleep(2 ** i)
                continue
            raise RuntimeError(json.dumps(d["errors"], ensure_ascii=False))
        return d["data"]
    raise RuntimeError("API retry exceeded")


def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    root = soup.find("div", class_="entry-content") or soup
    for img in root.find_all("img"):
        src = img.get("src", "")
        host = urlparse(src).hostname or ""
        if "wakaba.jpn.com" not in host:
            img.decompose()  # Facebook 等の外部画像・絵文字画像は除去（移行対象外）
    for tag in root.find_all(["script", "style", "iframe", "video", "source"]):
        tag.decompose()
    for tag in root.find_all(True):
        allowed = KEEP_ATTRS.get(tag.name, set())
        for a in list(tag.attrs):
            if a not in allowed:
                del tag.attrs[a]
        if tag.name == "img":
            tag["src"] = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", tag["src"].replace("/cmsdir/wp-content/", "/wp-content/"))
            tag.attrs.pop("width", None)
            tag.attrs.pop("height", None)
    for tag in root.find_all(["span", "font"]):
        tag.unwrap()
    for a in root.find_all("a"):
        href = a.get("href", "")
        if re.search(r"\.(jpe?g|png|gif)$", href, re.I) and a.find("img"):
            a.unwrap()  # 画像の原寸リンクは不要
    for p in root.find_all(["p", "div"]):
        if not p.get_text(strip=True).replace("\xa0", "") and not p.find("img"):
            p.decompose()
    for d in root.find_all("div"):
        d.name = "p" if not d.find(["p", "ul", "ol", "table"]) else d.name
        if d.name == "div":
            d.unwrap()
    html = "".join(str(c) for c in root.contents) if root is not soup else str(soup)
    html = INVISIBLE.sub("", html).replace("\xa0", " ")
    html = re.sub(r"<a>\s*</a>", "", html)
    # SNS 投稿の名残（区切りの「*」だけの行、冒頭の「WAKABA FARM」）は整形として除去。文章は変更しない
    html = re.sub(r"<p>\s*[\*＊]+\s*</p>", "", html)
    html = re.sub(r"^<p>\s*WAKABA[\s\u3000]*FARM\s*</p>", "", html.strip())
    html = re.sub(r"\n{2,}", "\n", html).strip()
    return html


def body_images(html):
    return list(dict.fromkeys(re.findall(r'<img[^>]+src="([^"]+)"', html)))


def handle_for(t):
    return f"wp-{t['post_id']}"


def summary_for(html):
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return text[:120]


def load_articles():
    topics = json.load(open(ROOT / "data/topics/topics.json", encoding="utf-8"))
    sel = [t for t in topics if t.get("categories") and not (set(t["categories"]) & EXCLUDE_CATS)]
    for t in sel:
        t["clean_html"] = clean_html(t["body_html"])
        t["clean_images"] = body_images(t["clean_html"])
    return sel


# ---------------------------------------------------------------- images
def upload_images(urls, imgmap):
    todo = [u for u in urls if u not in imgmap]
    print(f"images: {len(todo)} to upload ({len(imgmap)} done)")
    q = """mutation($files:[FileCreateInput!]!){ fileCreate(files:$files){ files{ id alt } userErrors{ field message } } }"""
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        files = []
        for u in batch:
            path = unquote(urlparse(u).path).split("/wp-content/uploads/")[-1]
            fname = "wakaba-" + re.sub(r"[^A-Za-z0-9._-]", "_", path.replace("/", "-"))
            files.append({"originalSource": u, "contentType": "IMAGE", "alt": "", "filename": fname,
                          "duplicateResolutionMode": "REPLACE"})
        d = gql(q, {"files": files})["fileCreate"]
        if d["userErrors"]:
            print("  userErrors:", d["userErrors"], file=sys.stderr)
        for u, f in zip(batch, d["files"] or []):
            imgmap[u] = {"id": f["id"], "url": None}
        IMGMAP.write_text(json.dumps(imgmap, ensure_ascii=False, indent=1))
        print(f"  queued {min(i + 50, len(todo))}/{len(todo)}")
    # 処理完了を待って URL を取得
    q2 = """query($ids:[ID!]!){ nodes(ids:$ids){ ... on MediaImage { id fileStatus image{ url } } } }"""
    for _ in range(60):
        pending = [u for u, v in imgmap.items() if u in urls and not v.get("url") and v.get("status") != "FAILED"]
        if not pending:
            break
        for i in range(0, len(pending), 100):
            ids = [imgmap[u]["id"] for u in pending[i:i + 100]]
            nodes = gql(q2, {"ids": ids})["nodes"]
            byid = {n["id"]: n for n in nodes if n}
            for u in pending[i:i + 100]:
                n = byid.get(imgmap[u]["id"])
                if not n:
                    continue
                if n["fileStatus"] == "READY" and n.get("image"):
                    imgmap[u]["url"] = n["image"]["url"].split("?")[0]
                elif n["fileStatus"] == "FAILED":
                    imgmap[u]["status"] = "FAILED"
        IMGMAP.write_text(json.dumps(imgmap, ensure_ascii=False, indent=1))
        left = sum(1 for u in urls if not imgmap.get(u, {}).get("url") and imgmap.get(u, {}).get("status") != "FAILED")
        print(f"  waiting for processing: {left} left")
        if left:
            time.sleep(5)
    return imgmap


# ---------------------------------------------------------------- blog
def get_blog():
    d = gql("""{ blogs(first: 10){ nodes{ id title handle } } }""")["blogs"]["nodes"]
    for b in d:
        if b["handle"] in ("news", "ニュース"):
            return b["id"]
    return gql("""mutation{ blogCreate(blog:{title:"新着情報", handle:"news"}){ blog{ id } userErrors{ message } } }""")["blogCreate"]["blog"]["id"]


def existing_handles(blog_id):
    handles, after = set(), None
    while True:
        d = gql("""query($id:ID!,$after:String){ blog(id:$id){ articles(first:250, after:$after){ nodes{ handle } pageInfo{ hasNextPage endCursor } } } }""",
                {"id": blog_id, "after": after})["blog"]["articles"]
        handles |= {n["handle"] for n in d["nodes"]}
        if not d["pageInfo"]["hasNextPage"]:
            return handles
        after = d["pageInfo"]["endCursor"]


CDN = "https://cdn.shopify.com/s/files/1/0993/0149/1056/files/"


def shopify_filename(u):
    path = unquote(urlparse(u).path).split("/wp-content/uploads/")[-1]
    return ("wakaba-old-" + re.sub(r"[^A-Za-z0-9._-]", "_", path.replace("/", "-"))).lower()


def write_jsonl(arts, blog_id):
    """バルク操作用 JSONL を 2 つ出力する。
    files.jsonl    … fileCreate（旧サイト画像を Shopify ファイルへ取り込み）
    articles.jsonl … articleCreate（画像 URL は取り込み後の CDN URL を予測して差し替え）
    """
    arts = [a for a in arts if a["clean_html"].strip()]
    urls = sorted({u for a in arts for u in a["clean_images"]})
    with open(OUT / "files.jsonl", "w", encoding="utf-8") as f:
        for u in urls:
            f.write(json.dumps({"files": [{"originalSource": u, "contentType": "IMAGE", "alt": "",
                                           "filename": shopify_filename(u), "duplicateResolutionMode": "REPLACE"}]},
                               ensure_ascii=False) + "\n")
    with open(OUT / "articles.jsonl", "w", encoding="utf-8") as f, \
         open(LOG, "w", newline="", encoding="utf-8") as lf:
        w = csv.writer(lf)
        w.writerow(["wp_post_id", "date", "categories", "title", "old_url", "new_path"])
        for a in arts:
            html = a["clean_html"]
            for u in a["clean_images"]:
                html = html.replace(f'src="{u}"', f'src="{CDN}{shopify_filename(u)}"')
            h = handle_for(a)
            inp = {"blogId": blog_id, "title": a["title"], "handle": h, "body": html, "summary": summary_for(html),
                   "tags": a["categories"] + ["旧サイト移行"], "isPublished": False, "author": {"name": "若葉農園"},
                   "publishDate": f"{a['date']}T09:00:00+09:00"}
            if a["clean_images"]:
                inp["image"] = {"url": CDN + shopify_filename(a["clean_images"][0]), "altText": a["title"]}
            f.write(json.dumps({"a": inp}, ensure_ascii=False) + "\n")
            w.writerow([a["post_id"], a["date"], " / ".join(a["categories"]), a["title"], a["url"], f"/blogs/news/{h}"])
    print(f"files.jsonl: {len(urls)} images / articles.jsonl: {len(arts)} articles → {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jsonl", action="store_true", help="バルク操作用の JSONL を出力（API トークン不要）")
    ap.add_argument("--blog-id", default="gid://shopify/Blog/122108379504")
    args = ap.parse_args()

    arts = load_articles()
    if args.limit:
        arts = arts[: args.limit]
    print(f"articles: {len(arts)}")

    if args.jsonl:
        write_jsonl(arts, args.blog_id)
        return

    if args.dry_run:
        sample = OUT / "blog_dry_run.json"
        sample.write_text(json.dumps([{k: a[k] for k in ("post_id", "date", "title", "categories", "url", "clean_html", "clean_images")}
                                      for a in arts], ensure_ascii=False, indent=1))
        print(f"images in bodies: {len({u for a in arts for u in a['clean_images']})}")
        print(f"html bytes: {sum(len(a['clean_html'].encode()) for a in arts)} → {sample}")
        return

    imgmap = json.loads(IMGMAP.read_text()) if IMGMAP.exists() else {}
    upload_images(sorted({u for a in arts for u in a["clean_images"]}), imgmap)

    blog_id = get_blog()
    done = existing_handles(blog_id)
    new_log = not LOG.exists()
    logf = open(LOG, "a", newline="", encoding="utf-8")
    w = csv.writer(logf)
    if new_log:
        w.writerow(["wp_post_id", "date", "title", "old_url", "new_path", "status"])
    q = """mutation($a: ArticleCreateInput!){ articleCreate(article:$a){ article{ id handle } userErrors{ field message } } }"""
    for i, a in enumerate(arts, 1):
        h = handle_for(a)
        if h in done:
            continue
        html = a["clean_html"]
        for u in a["clean_images"]:
            new = imgmap.get(u, {}).get("url")
            html = html.replace(f'src="{u}"', f'src="{new}"') if new else re.sub(rf'<img[^>]+src="{re.escape(u)}"[^>]*/?>', "", html)
        inp = {"blogId": blog_id, "title": a["title"], "handle": h, "body": html, "summary": summary_for(html),
               "tags": a["categories"], "isPublished": False, "author": {"name": "若葉農園"},
               "publishDate": f"{a['date']}T09:00:00+09:00"}
        first = next((imgmap[u]["url"] for u in a["clean_images"] if imgmap.get(u, {}).get("url")), None)
        if first:
            inp["image"] = {"url": first, "altText": a["title"]}
        d = gql(q, {"a": inp})["articleCreate"]
        status = "ok" if not d["userErrors"] else json.dumps(d["userErrors"], ensure_ascii=False)
        w.writerow([a["post_id"], a["date"], a["title"], a["url"], f"/blogs/news/{h}", status])
        logf.flush()
        print(f"  [{i}/{len(arts)}] {a['date']} {a['title'][:30]} {status}")
    logf.close()
    print("done")


if __name__ == "__main__":
    main()
