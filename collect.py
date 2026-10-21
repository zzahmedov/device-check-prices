#!/usr/bin/env python3
"""Qiymet cedvelini yenileyen robot (tap.az + lalafo.az).

Prinsipler:
- Yalniz aciq axtaris sehifeleri oxunur, giris telebi olan sehifelere girilmir.
- Her sorgu arasinda fasile var; gunde 1 defe isleyir.
- Elanin metni, sekilleri, telefon nomresi, satici adi SAXLANILMIR.
  Yalniz: qiymet, yeni/ikinci el, kanal, model, yaddas, il, tarix, menbe.
- Netice evvelki cedvelden kəskin ferqlenirse, fayl uzerine yazilmir.
"""
import json, os, re, sys, time, datetime, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "DeviceCheckPriceBot/0.1 (+contact: zzahmedov@gmail.com)"
DELAY = 6          # saniye, sorgular arasi
MAX_PAGES = 3      # her sorgu ucun maksimum sehife
MAX_AGE_DAYS = 60
KEEP_UNSEEN_DAYS = 10
MIN_PRICE, MAX_PRICE = 100, 20000
BAD_WORDS = re.compile(r"\b(plus|fe|edge|\+|case|qoruyucu|carx|ekran|stiker|kabel|adapter)\b|s26|s24|s23|s21|s20", re.I)

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "az,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")

def next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    return json.loads(m.group(1)) if m else None

def parse_storage(title):
    m = re.search(r"(\d{2,4})\s?(gb|tb)\b", title, re.I)
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2).lower() == "tb":
        n *= 1024
    return n if n in (64, 128, 256, 512, 1024) else None

def iso_from_ts(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")

# ---- tap.az ----
def parse_tap(html):
    d = next_data(html)
    out = []
    if not d:
        return out
    aps = d["props"]["pageProps"]["apolloState"]
    rq = aps["ROOT_QUERY"]
    key = next((k for k in rq if k.startswith("adSearch")), None)
    if not key:
        return out
    ads = next((v for k, v in rq[key].items() if k.startswith("ads(")), None)
    if not ads:
        return out
    for n in ads.get("nodes", []):
        ad = aps.get(n.get("__ref"), n)
        feats = (((ad.get("features") or {}).get("adFeatures")) or {})
        is_shop = bool(ad.get("shop")) or feats.get("label") == "RETAILER"
        price = ad.get("price")
        if not isinstance(price, (int, float)):
            continue
        out.append({
            "site": "tap.az", "id": str(ad.get("legacyResourceId") or ad.get("id")),
            "title": ad.get("title") or "", "price": int(price),
            "date": (ad.get("updatedAt") or "")[:10], "shop": is_shop,
        })
    return out

# ---- lalafo.az ----
def parse_lalafo(html):
    d = next_data(html)
    out = []
    if not d:
        return out
    qs = d["props"]["pageProps"]["dehydratedState"]["queries"]
    for q in qs:
        key = q.get("queryKey") or []
        data = (q.get("state") or {}).get("data")
        if key and key[0] == "listingFeed" and isinstance(data, dict):
            for page in data.get("pages", []):
                for it in page.get("items", []):
                    price = it.get("price")
                    if not isinstance(price, (int, float)) or it.get("currency") not in (None, "AZN"):
                        continue
                    ts = it.get("updated_time") or it.get("created_time")
                    if not ts:
                        continue
                    out.append({
                        "site": "lalafo.az", "id": str(it.get("id")),
                        "title": it.get("title") or "", "price": int(price),
                        "date": iso_from_ts(ts), "shop": bool((it.get("user") or {}).get("pro")),
                    })
    return out

def search_urls(site, q):
    if site == "tap.az":
        base = "https://tap.az/elanlar?" + urllib.parse.urlencode({"q[keywords]": q})
        return [base]  # tap.az: ilk sehife (24 elana qeder)
    base = "https://lalafo.az/azerbaijan/q-" + urllib.parse.quote(q)
    return [base] + [f"{base}?page={p}" for p in range(2, MAX_PAGES + 1)]

def classify(raw, cfg):
    t = raw["title"]
    if not re.search(cfg["title_regex"], t, re.I):
        return None
    if BAD_WORDS.search(t):
        return None
    if not (MIN_PRICE <= raw["price"] <= MAX_PRICE):
        return None
    try:
        age = (datetime.date.today() - datetime.date.fromisoformat(raw["date"])).days
    except ValueError:
        return None
    if age > MAX_AGE_DAYS or age < -1:
        return None
    return {
        "key": f'{raw["site"]}:{raw["id"]}',
        "price": raw["price"], "isNew": bool(raw["shop"]),
        "channel": "reseller" if raw["shop"] else None,
        "model": cfg["model"], "storageGb": parse_storage(t),
        "year": cfg["year"], "date": raw["date"], "source": raw["site"],
    }

def collect_live(cfg_all):
    found = {}
    for cfg in cfg_all["models"]:
        for q in cfg["queries"]:
            for site, parser in (("tap.az", parse_tap), ("lalafo.az", parse_lalafo)):
                for url in search_urls(site, q):
                    try:
                        html = fetch(url)
                        for raw in parser(html):
                            rec = classify(raw, cfg)
                            if rec:
                                found[rec["key"]] = rec
                    except Exception as e:
                        print(f"XETA {url}: {type(e).__name__}", file=sys.stderr)
                    time.sleep(DELAY)
    return found

def main():
    cfg = json.load(open(os.path.join(HERE, "models.json"), encoding="utf-8"))
    state_path = os.path.join(HERE, "state.json")
    out_path = os.path.join(HERE, "price_table.json")
    today = datetime.date.today().isoformat()
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    found = collect_live(cfg)
    print(f"tapildi: {len(found)}")
    for k, rec in found.items():
        rec["lastSeen"] = today
        state[k] = rec
    # uzun muddet gorunmeyen ve ya kohne elanlari sil
    cutoff_seen = (datetime.date.today() - datetime.timedelta(days=KEEP_UNSEEN_DAYS)).isoformat()
    cutoff_date = (datetime.date.today() - datetime.timedelta(days=MAX_AGE_DAYS)).isoformat()
    state = {k: v for k, v in state.items() if v["lastSeen"] >= cutoff_seen and v["date"] >= cutoff_date}
    listings = sorted(state.values(), key=lambda r: (r["model"], r["storageGb"] or 0, r["isNew"], r["price"]))
    table = {"version": 1, "updated": today, "listings": [
        {k: r[k] for k in ("price", "isNew", "channel", "model", "storageGb", "year", "date", "source")} for r in listings]}
    # Qoruma: cedvel cox kiciksə ve ya evvelkinin yarisindan azdirsa, yazma
    old_n = 0
    if os.path.exists(out_path):
        old_n = len(json.load(open(out_path, encoding="utf-8")).get("listings", []))
    if len(listings) < 10 or (old_n and len(listings) < old_n * 0.5):
        print(f"QORUMA: yeni cedvel suspektdir ({len(listings)} elan, evvel {old_n}). Yazilmadi.", file=sys.stderr)
        sys.exit(1)
    json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(table, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"yazildi: {len(listings)} elan")

if __name__ == "__main__":
    main()
