"""旧サイト(wakaba.jpn.com)のURL → Shopify のURL の 301 リダイレクト表を作る。
出力:
  data/seo/redirects_shopify_import.csv  … Shopify 管理画面の「URLリダイレクト → インポート」用
  data/seo/redirects_map_detail.csv      … 種類・メモ付きの確認用一覧
"""
import json, csv, collections
from urllib.parse import quote, urlparse

BLOG = '/blogs/' + quote('ニュース').lower()
def tag(n): return BLOG + '/tagged/' + quote(n).lower()

IMPORTED = {'wp-' + h for h in (
    "6244 6296 6299 6312 6330 6374 6439 6445 6457 6552 6712 6795 6804 6818 "
    "6870 6888 6936 6947 6964 6968 6988 7028 7064 7262").split()}

PRODUCT_HANDLES = {
    13: 'sudachiponzu', 21: 'mugichatb', 68: 'mugichatsubu', 415: 'chunosauce',
    850: 'usutasauce', 1436: 'sudachi', 1799: 'dashi', 2210: 'brown-cider',
    3367: 'hakumai-4-5kg-6years', 3408: 'hakumai-4-5kg-3-5years', 3412: 'hakumai-4-5kg-1-2years',
    3418: 'genmai-5kg-1-2years', 3420: 'genmai-5kg-3-5years', 3424: 'genmai-5kg-6years',
    4229: 'carrotjuice350', 4578: 'carrotjuice1000',
    4717: 'otameshi-yasai-set-reizo', 4723: 'otameshi-yasai-set-joon',
    6027: 'carrotjuice1000', 6036: 'carrotjuice350',  # 12本/20本セット（Shopifyに無し）→ 単品へ
    6518: 'winter-gift-carrot-juice-1000ml-1', 6597: 'winter-gift-carrot-juice-350ml-1',
    6606: 'winter-gift-carrot-juice-1000ml-2', 6609: 'winter-gift-carrot-juice-350ml-2',
    6611: 'winter-gift-carrot-juice-350ml-3',
}

rows = []
def add(f, t, kind, note=''): rows.append((f, t, kind, note))

for f, t, n in [
    ('/about/', '/pages/about', ''), ('/company/', '/pages/about', '会社概要→若葉農園について'),
    ('/delivery/', '/pages/delivery', ''), ('/guide/', '/pages/guide', ''),
    ('/inquiry/', '/pages/contact', ''), ('/shops/', '/collections/all', ''),
    ('/shop/', '/collections/all', ''), ('/archives/category/item/', '/collections/all', ''),
    ('/archives/category/itempremium/', '/collections/all', ''), ('/topics/', BLOG, ''),
    ('/usces-cart/', '/cart', 'カート'), ('/usces-member/', '/account', '会員ページ'),
    ('/premium-member/', '/account', '')]:
    add(f, t, '固定ページ', n)

for slug, name in {'farm': '畑情報', 'recipe': 'レシピ', 'trainee': '研修生ストーリー',
                   quote('お客様の声').lower(): 'お客様の声', quote('お知らせ').lower(): 'お知らせ',
                   quote('研修申し込み').lower(): '研修申し込み'}.items():
    add(f'/archives/topiccat/{slug}/', tag(name), 'カテゴリ', name)
add('/archives/topiccat/yasaiseto/', '/pages/delivery', 'カテゴリ', 'お野菜ラインナップ→定期宅配')

for x in json.load(open('data/products/products.json')):
    add(urlparse(x['url']).path, '/products/' + PRODUCT_HANDLES[int(x['post_id'])], '商品', x['name'])

count = collections.Counter()
for x in json.load(open('data/topics/topics.json')):
    path = urlparse(x['url']).path
    h = 'wp-' + str(x['post_id'])
    cats = x['categories'] or []
    if h in IMPORTED:
        add(path, f'{BLOG}/{h}', '記事(移行済み)', x['title']); count['移行済み'] += 1
    elif 'お野菜ラインナップ' in cats:
        add(path, '/pages/delivery', '記事(お野菜ラインナップ)', x['title']); count['お野菜ラインナップ'] += 1
    else:
        add(path, tag(cats[0]) if cats else BLOG, '記事(未移行→カテゴリ一覧)', f"{x['date']} {x['title']}")
        count['未移行'] += 1

assert len({r[0] for r in rows}) == len(rows), 'duplicate source path'
with open('data/seo/redirects_shopify_import.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['Redirect from', 'Redirect to']); w.writerows([r[:2] for r in rows])
with open('data/seo/redirects_map_detail.csv', 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f); w.writerow(['旧URL（パス）', '新URL（パス）', '種類', 'メモ']); w.writerows(rows)
print(dict(count), 'total', len(rows))
