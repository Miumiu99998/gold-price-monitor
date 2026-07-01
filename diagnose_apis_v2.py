#!/usr/bin/env python3
"""API Diagnostic Script - Test all gold/FX APIs on GitHub Actions"""
import json, ssl, time, re, sys
from urllib.request import Request, urlopen

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
CTX = ssl.create_default_context()

def test(name, url, parse_fn=None, timeout=15):
    """Test one API endpoint. Returns (ok, value_or_error)"""
    t0 = time.time()
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
            "Accept": "*/*"
        })
        resp = urlopen(req, context=CTX, timeout=timeout)
        elapsed = time.time() - t0
        body = resp.read().decode('utf-8', errors='replace')
        
        if parse_fn:
            val = parse_fn(body)
            if val:
                return True, ("%.2fs | HTTP %d | Value: %s" % (elapsed, resp.status, str(val)))
            else:
                return False, ("%.2fs | HTTP %d | Parse failed: %s" % (elapsed, resp.status, body[:200]))
        return True, ("%.2fs | HTTP %d | Body: %s" % (elapsed, resp.status, body[:200]))
    except Exception as e:
        elapsed = time.time() - t0
        return False, ("%.2fs | ERR: %s" % (elapsed, str(e)[:150]))

def parse_json_gold_usd(body):
    """Try to extract gold USD/oz from JSON"""
    try:
        d = json.loads(body)
        # Try various structures
        paths = [
            ['metals','gold','price'],
            ['rates','XAU'],  # inverted
            ['data','price'],
            ['goldPrice'],
            ['value'],
            ['price'],
        ]
        for path in paths:
            obj = d
            ok = True
            for key in path:
                if isinstance(obj, dict) and key in obj:
                    obj = obj[key]
                else:
                    ok = False; break
            if ok and obj:
                v = float(obj)
                # If it's XAU/USD rate (< 0.01), invert
                if v < 0.01:
                    v = 1.0 / v
                if 500 < v < 15000:
                    return "$%.2f/oz" % v
        # Try values array (TwelveData)
        vals = d.get('values', [])
        if vals:
            p = float(vals[0].get('close', 0) or vals[0].get('open', 0))
            if p > 500: return "$%.2f/oz" % p
        # Try meta (Yahoo)
        meta = d.get('chart',{}).get('result',[{}])[0].get('meta',{}) if d.get('chart') else {}
        for f in ['regularMarketPrice','previousClose']:
            p = float(meta.get(f,0) or 0)
            if 1000 < p < 15000: return "$%.2f/oz" % p
        return None
    except: return None

def parse_fx(body):
    """Extract USD/CNY from JSON"""
    try:
        d = json.loads(body)
        r = d.get('rates',{})
        cny = r.get('CNY')
        if cny: return "%.4f" % float(cny)
        return None
    except: return None

def parse_html_gold(body):
    """Extract gold price from HTML"""
    patterns = [
        r'Bid\s*:\s*\$?([\d,]+\.?\d*)',
        r'\$([4-9]\d{3}\.\d{2})',
        r'(\d{1,2},?\d{3}\.\d{2})\s*(?:USD|per oz)',
    ]
    for pat in patterns:
        m = re.search(pat, body, re.I|re.S)
        if m:
            raw = m.group(1).replace(',','')
            try:
                p = float(raw)
                if 1000 < p < 15000: return "$%.2f/oz" % p
            except: pass
    return None

# ============================================================
# Run Tests
# ============================================================
print("=" * 60)
print("API DIAGNOSTIC - Gold Price & FX Sources")
print("Time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 60)

results = []

# --- Gold Price APIs ---
print("\n--- GOLD PRICE SOURCES ---")

tests = [
    ("TwelveData", "https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1min&outputsize=1&apikey=demo", parse_json_gold_usd),
    ("Yahoo Finance", "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m", parse_json_gold_usd),
    ("Yahoo v2", "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m", parse_json_gold_usd),
    ("Metals.dev(USD)", "https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz", parse_json_gold_usd),
    ("Metal-Price-API", "https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo", parse_json_gold_usd),
    ("Metals-API2", "https://metals-api.com/api/latest?access_key=demo&base=USD&symbols=XAU", parse_json_gold_usd),
    ("Kitco Live", "https://www.kitco.com/charts.live.html", parse_html_gold),
    ("GoldPrice.org", "https://goldprice.org/", parse_html_gold),
    ("FGI China", "https://api.fgi.gov.cn/goldPrice?_=%d" % int(time.time()), parse_json_gold_usd),
]

for name, url, fn in tests:
    ok, info = test(name, url, fn)
    status = "OK" if ok else "FAIL"
    print("\n[%s] %s" % (status, name))
    print("  URL: %s" % url[:80])
    print("  Result: %s" % info)
    results.append((name, ok, info))

# --- FX Rate APIs ---
print("\n--- FX RATE SOURCES ---")

fx_tests = [
    ("Frankfurter", "https://api.frankfurter.app/latest?from=USD&to=CNY", parse_fx),
    ("ER-API", "https://open.er-api.com/v6/latest/USD", parse_fx),
    ("Frankfurter-Latest", "https://api.frankfurter.app/latest?amount=1&from=USD&to=CNY", parse_fx),
]

for name, url, fn in fx_tests:
    ok, info = test(name, url, fn)
    status = "OK" if ok else "FAIL"
    print("\n[%s] %s" % (status, name))
    print("  Result: %s" % info)
    results.append(("FX:"+name, ok, info))

# --- Summary ---
print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
ok_count = sum(1 for _,o,_ in results if o)
fail_count = sum(1 for _,o,_ in results if not o)
print("Total: %d | OK: %d | FAIL: %d" % (len(results), ok_count, fail_count))
for name, ok, info in results:
    s = "OK" if ok else "FAIL"
    print("  [%s] %s: %s" % (s, name, info[:80]))
