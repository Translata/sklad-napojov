#!/usr/bin/env python3
"""Nočná synchronizácia predajov pre Sklad nápojov.

Stiahne odoslané objednávky zo Shoptetu (PPP, trvalý odkaz na export) a objednávky zo Shopify (GSO),
prepočíta položky na plechovky podľa mapping.json a zapíše výsledok do Sklad-app/predaje.json na SharePointe.

Premenné prostredia (lokálne v .env vedľa skriptu alebo v ../import/.env, na GitHube ako secrets):
  SHOPTET_EXPORT_URL                     trvalý odkaz na export objednávok
  SHOPIFY_STORE, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET   (alebo SHOPIFY_TOKEN)
  GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET     zápis na SharePoint (bez nich sa súbor len uloží lokálne)
  GSO_ICO                                voliteľné, inak sa GSO rozpozná podľa názvu firmy

Výpis neobsahuje mená zákazníkov ani odkazy (logy na GitHube sú verejné).

Použitie:  python3 sync.py [--from 2026-07-01] [--out predaje.json] [--no-upload]
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Bratislava")
SITE_HOST, SITE_PATH = "translata.sharepoint.com", "/sites/liquidpositive"
TARGET = "Sklad-app/predaje.json"
GSO_NAME = re.compile(r"good\s*stuff\s*only", re.I)
B2C_PRIVATE = "E-shop (súkromné osoby)"

SHOPIFY_QUERY = """
query($q: String!, $after: String) {
  orders(first: 50, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      name createdAt cancelledAt
      lineItems(first: 50) {
        pageInfo { hasNextPage }
        nodes { title currentQuantity variant { id } }
      }
    }
  }
}
"""


def load_env():
    for f in (HERE / ".env", HERE.parent / "import" / ".env"):
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    os.environ.setdefault(k.strip(), v)


def env(name, required=True):
    v = os.environ.get(name, "")
    if required and not v:
        sys.exit(f"Chýba premenná {name}")
    return v


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        # bez query stringu, odkazy môžu obsahovať tajné hashe
        sys.exit(f"HTTP {e.code} z {url.split('?')[0][:80]}: {body}")


# ---------- Shoptet (PPP) ----------

def shoptet_lines(mapping, since):
    raw = http(env("SHOPTET_EXPORT_URL"), headers={"User-Agent": "sklad-app-sync/1.0"})
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1250")
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    need = {"code", "date", "orderItemType", "orderItemCode", "orderItemAmount"}
    if rows and not need <= set(rows[0]):
        sys.exit("Shoptet export nemá potrebné stĺpce: " + ", ".join(sorted(need - set(rows[0]))))
    codes, ignore = mapping["shoptet"]["codes"], set(mapping["shoptet"]["ignorovat"])
    # kód -> {nápoj: plechovky na 1 kus}; zmiešané balenia sa rozpočítajú
    per_unit = {c: {name: 1} for c, name in codes.items()}
    per_unit.update(mapping["shoptet"].get("mixed", {}))
    gso_ico = os.environ.get("GSO_ICO", "").strip()

    agg = defaultdict(float)
    unmapped = defaultdict(float)
    orders = set()
    for r in rows:
        d = (r.get("date") or "")[:10]
        if d < since or r.get("orderItemType") != "product":
            continue
        if r.get("statusName") and r["statusName"] != "Odoslaná":
            continue
        code = (r.get("orderItemCode") or "").strip()
        qty = float((r.get("orderItemAmount") or "0").replace(",", ".") or 0)
        if code not in per_unit:
            if code and code not in ignore:
                unmapped[f'{code} {(r.get("orderItemName") or "")[:50]}'] += qty
            continue
        ico = (r.get("billCompanyId") or r.get("customerIdentificationNumber") or "").strip()
        company = (r.get("billCompany") or "").strip()
        gso = bool(GSO_NAME.search(company)) or (gso_ico and ico == gso_ico)
        customer = "GSO" if gso else (company or (f"IČO {ico}" if ico else B2C_PRIVATE))
        for name, n in per_unit[code].items():
            agg[(d, r["code"], customer, gso, name)] += qty * n
        orders.add(r["code"])

    lines = [{"d": d, "o": o, "c": c, "gso": g, "k": k, "q": round(q)}
             for (d, o, c, g, k), q in sorted(agg.items()) if round(q)]
    return lines, dict(unmapped), len(orders)


# ---------- Shopify (GSO) ----------

def shopify_token(store):
    if os.environ.get("SHOPIFY_TOKEN"):
        return os.environ["SHOPIFY_TOKEN"]
    body = urllib.parse.urlencode({
        "client_id": env("SHOPIFY_CLIENT_ID"), "client_secret": env("SHOPIFY_CLIENT_SECRET"),
        "grant_type": "client_credentials"}).encode()
    res = json.loads(http(f"https://{store}.myshopify.com/admin/oauth/access_token", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"}))
    return res["access_token"]


def shopify_lines(mapping, since):
    store = env("SHOPIFY_STORE")
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    token = shopify_token(store)
    url = f"https://{store}.myshopify.com/admin/api/{version}/graphql.json"
    headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": token}

    # variant -> ({nápoj: plechovky na kus}, typ); jednotlivé plechovky (cans 1) predáva GSO firmám (B2B)
    variants = {}
    for d in mapping["drinks"]:
        for v in d["variants"]:
            variants[v["shopifyVariantId"]] = ({d["name"]: v["cans"]}, "B2B" if v["cans"] == 1 else "B2C")
    for p in mapping.get("mixed", []):
        for v in p["variants"]:
            variants[v["shopifyVariantId"]] = (dict(v["cans"]), "B2C")
    ignore = set(mapping.get("ignorovat", []))

    agg = defaultdict(int)
    unmapped = defaultdict(int)
    n_orders = 0
    after = None
    while True:
        payload = json.dumps({"query": SHOPIFY_QUERY,
                              "variables": {"q": f"created_at:>={since}", "after": after}}).encode()
        for _ in range(6):
            res = json.loads(http(url, data=payload, headers=headers))
            if res.get("errors") and "THROTTLED" in json.dumps(res["errors"]):
                time.sleep(3)
                continue
            break
        if res.get("errors"):
            sys.exit("Shopify GraphQL chyba: " + json.dumps(res["errors"])[:300])
        page = res["data"]["orders"]
        for o in page["nodes"]:
            if o["cancelledAt"]:
                continue
            if o["lineItems"]["pageInfo"]["hasNextPage"]:
                sys.exit(f"Objednávka {o['name']} má viac ako 50 položiek, treba upraviť skript.")
            n_orders += 1
            # dátum vytvorenia v miestnom čase
            day = datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).astimezone(TZ).date().isoformat()
            for li in o["lineItems"]["nodes"]:
                qty = li["currentQuantity"]  # už bez refundovaných a odobraných kusov
                vid = (li["variant"] or {}).get("id")
                if not qty or vid in ignore:
                    continue
                if vid not in variants:
                    unmapped[li["title"][:60]] += qty
                    continue
                cans, kind = variants[vid]
                for name, n in cans.items():
                    agg[(day, name, kind)] += n * qty
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    lines = [{"d": d, "k": k, "t": t, "q": q} for (d, k, t), q in sorted(agg.items()) if q]
    return lines, dict(unmapped), n_orders


# ---------- SharePoint ----------

def upload(data):
    tenant = env("GRAPH_TENANT_ID")
    body = urllib.parse.urlencode({
        "client_id": env("GRAPH_CLIENT_ID"), "client_secret": env("GRAPH_CLIENT_SECRET"),
        "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}).encode()
    tok = json.loads(http(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token", data=body))["access_token"]
    auth = {"Authorization": f"Bearer {tok}"}
    site = json.loads(http(f"https://graph.microsoft.com/v1.0/sites/{SITE_HOST}:{SITE_PATH}", headers=auth))["id"]
    http(f"https://graph.microsoft.com/v1.0/sites/{site}/drive/root:/{TARGET}:/content",
         data=json.dumps(data, ensure_ascii=False).encode(), method="PUT",
         headers={**auth, "Content-Type": "application/json"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-01", help="od dátumu vytvorenia objednávky (YYYY-MM-DD)")
    ap.add_argument("--out", help="uložiť aj lokálne do súboru")
    ap.add_argument("--no-upload", action="store_true", help="nezapisovať na SharePoint")
    a = ap.parse_args()
    load_env()
    mapping = json.loads((HERE / "mapping.json").read_text())

    sh_lines, sh_unmapped, sh_orders = shoptet_lines(mapping, a.since)
    sf_lines, sf_unmapped, sf_orders = shopify_lines(mapping, a.since)
    data = {
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": a.since,
        "shoptet": sh_lines,
        "shopify": sf_lines,
        "unmapped": {"shoptet": sh_unmapped, "shopify": sf_unmapped},
    }

    gso = sum(l["q"] for l in sh_lines if l["gso"])
    direct = sum(l["q"] for l in sh_lines if not l["gso"])
    print(f"Od {a.since}: Shoptet {sh_orders} objednávok (GSO {gso} ks, priamo {direct} ks), "
          f"Shopify {sf_orders} objednávok ({sum(l['q'] for l in sf_lines)} ks)")
    print(f"Nesledované položky: Shoptet {len(sh_unmapped)}, Shopify {len(sf_unmapped)}")

    if a.out:
        Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print("Uložené lokálne:", a.out)
    if a.no_upload:
        return
    if not os.environ.get("GRAPH_CLIENT_SECRET"):
        print("GRAPH_* nie sú nastavené, na SharePoint sa nezapisuje.")
        return
    upload(data)
    print("Zapísané na SharePoint:", TARGET)


if __name__ == "__main__":
    main()
