#!/usr/bin/env python3
"""
旧サイト https://www.wakaba.jpn.com (WordPress + Welcart) から
文章・商品・トピックス記事・画像一覧を取得し、Shopify 移行用データとして保存する。

使い方:
    python3 scripts/scrape.py            # 文章・商品・記事・画像一覧を取得
    python3 scripts/scrape.py --images   # 上記に加えて画像本体もダウンロード

出力先: data/ (README.md を参照)
"""
import argparse
import csv
import json
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import hashlib

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html2md

BASE = "https://www.wakaba.jpn.com"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
UA = "Mozilla/5.0 (site-migration; wakabafarm)"
SESSION = requests.Session()
SESSION.headers["User-Agent"] = UA
LOCK = threading.Lock()

# 画像の使用箇所: original_url -> {"alts": set(), "used_on": set()}
IMAGES: dict = {}


CACHE = ROOT / ".cache" / "html"


def get(url, binary=False, tries=6):
    """HTML はローカルにキャッシュし、再実行時に旧サーバーへ再アクセスしない。"""
    cfile = None
    if not binary:
        cfile = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".html")
        if cfile.exists():
            return cfile.read_text(encoding="utf-8")
    for i in range(tries):
        try:
            r = SESSION.get(url, timeout=60)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            if binary:
                return r.content
            r.encoding = "utf-8"
            cfile.parent.mkdir(parents=True, exist_ok=True)
            cfile.write_text(r.text, encoding="utf-8")
            return r.text
        except requests.RequestException as e:
            if i == tries - 1:
                print(f"  ! failed {url}: {e}", file=sys.stderr)
                return None
            time.sleep(5 * 2 ** i)


def sitemap(name):
    xml = get(f"{BASE}/wp-sitemap-{name}-1.xml") or ""
    return re.findall(r"<loc>([^<]+)</loc>", xml)


SIZE_RE = re.compile(r"-(\d+)x(\d+)(\.[a-zA-Z]+)$")


def original_image_url(src):
    """サムネイル (-300x200 等) を外したオリジナル画像 URL を返す。"""
    src = urljoin(BASE, src).replace("/cmsdir/wp-content/", "/wp-content/")
    src = src.split("?")[0]
    return SIZE_RE.sub(r"\3", src)


def record_images(soup_fragment, page_url):
    if soup_fragment is None:
        return []
    found = []
    for img in soup_fragment.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src or src.startswith("data:"):
            continue
        orig = original_image_url(src)
        # リンク先がフルサイズ画像ならそちらを優先
        a = img.find_parent("a")
        if a and a.get("href", "").lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            orig = original_image_url(a["href"])
        alt = (img.get("alt") or "").strip()
        with LOCK:
            e = IMAGES.setdefault(orig, {"alts": set(), "used_on": set()})
            if alt:
                e["alts"].add(alt)
            e["used_on"].add(page_url)
        if orig not in found:
            found.append(orig)
    return found


def clean_html(el, keep_forms=False):
    if el is None:
        return ""
    drop = ["script", "style", "noscript", "iframe"] + ([] if keep_forms else ["form"])
    for t in el.find_all(drop):
        t.decompose()
    html = str(el)
    return html.replace("/cmsdir/wp-content/", "/wp-content/")


def to_text(html):
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def to_md(html):
    return html2md(html, heading_style="ATX").strip() + "\n"


def slug_from_url(url):
    path = unquote(urlparse(url).path).strip("/")
    return path.replace("/", "__") or "home"


# ---------------------------------------------------------------- pages
def scrape_page(url):
    html = get(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc = soup.find("meta", attrs={"name": "description"})
    for h in soup.find_all(["header", "footer"]):
        record_images(h, url)  # ロゴ等
    # 固定ページは <main> の外にも本文があるため、body からヘッダー/フッター/メニューを除いて取得
    main = soup.body
    for t in main.find_all(["header", "footer", "nav"]):
        t.decompose()
    for t in main.select("#secondary, .widget-area, #drawer, .drawer, .sp-menu"):
        t.decompose()
    imgs = record_images(main, url)
    body = clean_html(main, keep_forms=True)
    return {
        "url": url,
        "slug": slug_from_url(url),
        "title": title,
        "meta_description": desc.get("content", "") if desc else "",
        "images": imgs,
        "body_html": body,
        "body_text": to_text(body),
    }


# ---------------------------------------------------------------- products
def scrape_product(url):
    html = get(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    art = soup.find("article")
    pid = re.search(r"/archives/(\d+)/", url).group(1)
    tx = lambda sel: (art.select_one(sel).get_text(" ", strip=True) if art and art.select_one(sel) else "")

    images = []
    slides = soup.select_one(".item-slider") or soup.select_one("#item-image") or soup.select_one(".item__img-box")
    images = record_images(slides, url)
    alt = ""
    if slides and slides.find("img"):
        alt = slides.find("img").get("alt", "")

    meta = {}
    for dl in art.select("dl.item__meta") if art else []:
        for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
            meta[dt.get_text(strip=True).rstrip("：:")] = dd.get_text(" ", strip=True)

    skus = []
    for f in art.select(".skuform") if art else []:
        price_in = f.find("input", attrs={"name": re.compile(r"^skuPrice")})
        code = re.search(r"\[\d+\]\[([^\]]+)\]", price_in["name"]).group(1) if price_in else ""
        status = f.select_one(".zaikostatus")
        name = f.select_one(".skuname")
        skus.append({
            "sku_code": code,
            "sku_name": name.get_text(strip=True) if name else "",
            "price": int(price_in["value"]) if price_in and price_in.get("value", "").isdigit() else None,
            "stock_status": status.get_text(strip=True).replace("在庫状態 :", "").strip() if status else "",
        })

    content = art.select_one("#item-content") if art else None
    record_images(content, url)
    content_html = clean_html(content)
    classes = art.get("class", []) if art else []
    return {
        "post_id": int(pid),
        "url": url,
        "handle_hint": alt,
        "name": tx(".item__name"),
        "price_display": tx(".item__price .price"),
        "catch_copy": tx(".item__copy"),
        "description": tx(".item__description"),
        "meta": meta,
        "skus": skus,
        "images": images,
        "content_html": content_html,
        "content_text": to_text(content_html),
        "wp_classes": [c for c in classes if c.startswith(("category-", "tag-"))],
    }


# ---------------------------------------------------------------- topics
def scrape_topic(url):
    html = get(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    art = soup.select_one("article.single-post")
    if not art:
        return {"url": url, "error": "article not found"}
    pid = (art.get("id") or "").replace("post-", "")
    date = art.select_one(".single-post__date")
    cats = [a.get_text(strip=True) for a in art.select(".single-post__category a")]
    title = art.select_one(".single-post__ttl")
    content = art.select_one(".entry-content")
    imgs = record_images(content, url)
    body = clean_html(content)
    return {
        "post_id": int(pid) if pid.isdigit() else None,
        "url": url,
        "slug": unquote(urlparse(url).path.strip("/").split("/")[-1]),
        "title": title.get_text(strip=True) if title else "",
        "date": (date.get_text(strip=True).replace("記事作成：", "").replace(".", "-") if date else ""),
        "categories": cats,
        "images": imgs,
        "body_html": body,
        "body_text": to_text(body),
    }


def run_parallel(fn, urls, label, workers=2):
    results = []
    done = 0

    def wrap(u):
        nonlocal done
        r = fn(u)
        time.sleep(0.2)  # 旧サーバーへの負荷を抑える
        with LOCK:
            done += 1
            if done % 50 == 0 or done == len(urls):
                print(f"  {label}: {done}/{len(urls)}", flush=True)
        return r

    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(wrap, urls):
            if r:
                results.append(r)
    return results


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- Shopify CSV
SHOPIFY_COLS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value", "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
    "Variant Inventory Policy", "Variant Fulfillment Service", "Variant Price", "Variant Requires Shipping",
    "Variant Taxable", "Image Src", "Image Position", "Image Alt Text", "SEO Title", "SEO Description", "Status",
]


def product_handle(p):
    h = (p.get("handle_hint") or "").strip().lower()
    h = re.sub(r"[^a-z0-9]+", "-", h).strip("-")
    return h or f"wp-{p['post_id']}"


def product_body_html(p):
    parts = []
    if p["catch_copy"]:
        parts.append(f"<h2>{p['catch_copy']}</h2>")
    if p["description"]:
        parts.append(f"<p>{p['description']}</p>")
    if p["meta"]:
        rows = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in p["meta"].items())
        parts.append(f"<table>{rows}</table>")
    if p["content_text"]:
        parts.append(re.sub(r"</?div[^>]*>|<span class=\"spacer[^\"]*\"></span>", "", p["content_html"]).strip())
    return "\n".join(parts)


def write_shopify_csv(products, path):
    seen = set()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHOPIFY_COLS)
        w.writeheader()
        for p in sorted(products, key=lambda x: x["post_id"]):
            handle = product_handle(p)
            while handle in seen:
                handle += f"-{p['post_id']}"
            seen.add(handle)
            skus = p["skus"] or [{"sku_code": "", "sku_name": "", "price": None}]
            multi = len(skus) > 1
            rows = max(len(skus), len(p["images"]), 1)
            for i in range(rows):
                row = {c: "" for c in SHOPIFY_COLS}
                row["Handle"] = handle
                if i == 0:
                    row.update({
                        "Title": p["name"], "Body (HTML)": product_body_html(p), "Vendor": "わかば農園",
                        "Type": "", "Tags": "", "Published": "FALSE", "Status": "draft",
                        "SEO Title": p["name"], "SEO Description": (p["catch_copy"] or p["description"])[:160],
                        "Option1 Name": "種類" if multi else "Title",
                    })
                if i < len(skus):
                    s = skus[i]
                    row.update({
                        "Option1 Value": (s["sku_name"] if multi else "Default Title"),
                        "Variant SKU": f"{p['post_id']}-{s['sku_code']}" if s["sku_code"] else "",
                        "Variant Grams": "0", "Variant Inventory Tracker": "", "Variant Inventory Policy": "deny",
                        "Variant Fulfillment Service": "manual",
                        "Variant Price": str(s["price"]) if s["price"] is not None else "",
                        "Variant Requires Shipping": "TRUE", "Variant Taxable": "TRUE",
                    })
                    if not multi and i == 0:
                        row["Option1 Name"] = "Title"
                if i < len(p["images"]):
                    row["Image Src"] = p["images"][i]
                    row["Image Position"] = str(i + 1)
                    row["Image Alt Text"] = p["name"]
                w.writerow(row)


# ---------------------------------------------------------------- images
def image_local_path(url):
    path = unquote(urlparse(url).path)
    path = path.replace("/wp-content/uploads/", "uploads/").replace("/wp-content/themes/", "themes/").lstrip("/")
    return OUT / "images" / path


def download_image(url):
    dest = image_local_path(url)
    if urlparse(url).hostname not in ("www.wakaba.jpn.com", "wakaba.jpn.com"):
        return url, dest, False  # 外部サイト（Facebook 等）の画像は取得対象外
    if dest.exists() and dest.stat().st_size > 0:
        return url, dest, True
    data = get(url, binary=True)
    if data is None:
        return url, dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    time.sleep(0.1)
    return url, dest, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", action="store_true", help="画像本体もダウンロードする")
    ap.add_argument("--limit-topics", type=int, default=0, help="テスト用: トピック取得件数の上限")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)

    # ---- 固定ページ
    page_urls = sitemap("posts-page") + [f"{BASE}/shops/", f"{BASE}/topics/", f"{BASE}/archives/category/item/"]
    page_urls = [u for u in dict.fromkeys(page_urls) if "usces-" not in u]
    print(f"pages: {len(page_urls)}")
    pages = run_parallel(scrape_page, page_urls, "pages")
    for p in pages:
        d = OUT / "pages"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{p['slug']}.html").write_text(p["body_html"], encoding="utf-8")
        md = f"# {p['title']}\n\n元URL: {p['url']}\n\n" + to_md(p["body_html"])
        (d / f"{p['slug']}.md").write_text(md, encoding="utf-8")
    write_json(OUT / "pages" / "pages.json", pages)

    # ---- 商品
    prod_urls = sitemap("posts-post")
    print(f"products: {len(prod_urls)}")
    products = run_parallel(scrape_product, prod_urls, "products")
    products = [p for p in products if p.get("name")]
    write_json(OUT / "products" / "products.json", products)
    write_shopify_csv(products, OUT / "products" / "shopify_products_import.csv")

    # ---- トピックス
    topic_urls = sitemap("posts-topic")
    if args.limit_topics:
        topic_urls = topic_urls[: args.limit_topics]
    print(f"topics: {len(topic_urls)}")
    topics = run_parallel(scrape_topic, topic_urls, "topics")
    topics.sort(key=lambda t: (t.get("date", ""), t.get("post_id") or 0))
    write_json(OUT / "topics" / "topics.json", topics)
    tdir = OUT / "topics" / "markdown"
    tdir.mkdir(parents=True, exist_ok=True)
    for t in topics:
        if "error" in t:
            continue
        fm = (
            f"---\ntitle: {json.dumps(t['title'], ensure_ascii=False)}\ndate: {t['date']}\n"
            f"categories: {json.dumps(t['categories'], ensure_ascii=False)}\n"
            f"wp_post_id: {t['post_id']}\nsource_url: {t['url']}\n---\n\n"
        )
        (tdir / f"{t['date']}_{t['post_id']}.md").write_text(fm + to_md(t["body_html"]), encoding="utf-8")
    with open(OUT / "topics" / "topics_index.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["wp_post_id", "date", "categories", "title", "image_count", "url"])
        for t in topics:
            if "error" in t:
                continue
            w.writerow([t["post_id"], t["date"], " / ".join(t["categories"]), t["title"], len(t["images"]), t["url"]])

    # ---- テーマ CSS の背景画像 / PDF などの追加素材
    theme = f"{BASE}/wp-content/themes/wakaba/"
    css = get(theme + "style.css") or ""
    for rel in sorted(set(re.findall(r"url\(['\"]?([^)'\"]+)", css))):
        if rel.startswith("data:") or "/fonts/" in rel:
            continue
        u = urljoin(theme, rel.split("?")[0])
        IMAGES.setdefault(u, {"alts": set(), "used_on": set()})["used_on"].add(theme + "style.css (背景画像)")
    IMAGES.setdefault(theme + "story.pdf", {"alts": set(), "used_on": set()})["used_on"].add(BASE + "/ (リンク: story.pdf)")

    # ---- 画像一覧
    print(f"images referenced: {len(IMAGES)}")
    results = {}
    if args.images:
        urls = sorted(IMAGES)
        for url, dest, ok in run_parallel(download_image, urls, "images", workers=2):
            results[url] = (dest, ok)
    with open(OUT / "images_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "original_url", "local_file", "bytes", "used_on_count", "used_on (先頭5件)", "alt",
            "区分(サイト共通/商品/固定ページ/記事)",
            "著作権確認 (自社撮影/外部提供/不明)", "対応方針 (そのまま使用/新規撮影/新規制作/不要)", "メモ",
        ])
        for url in sorted(IMAGES):
            e = IMAGES[url]
            used = sorted(e["used_on"])
            dest, ok = results.get(url, (image_local_path(url), False))
            size = dest.stat().st_size if dest.exists() else ""
            if "/themes/" in url:
                kind = "サイト共通(デザイン素材)"
            elif any("/archives/" in u and "/topic" not in u for u in used if re.search(r"/archives/\d+/", u)):
                kind = "商品"
            elif all("/topics/" in u for u in used):
                kind = "記事"
            else:
                kind = "固定ページ"
            w.writerow([
                url, str(dest.relative_to(ROOT)) if dest.exists() else "", size, len(used), " | ".join(used[:5]),
                " / ".join(sorted(e["alts"])), kind, "", "", "",
            ])
    print("done.")


if __name__ == "__main__":
    main()
