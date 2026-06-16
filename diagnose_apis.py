#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnose_apis.py - 在GitHub Actions环境中诊断可用API
输出每个数据源的连通性和返回结果
"""
import urllib.request, json, ssl, re, time, sys

CTX = ssl.create_default_context()
HDRS = {
    'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.0; Diagnostics)',
    'Accept': '*/*',
}

def test(name, url, parse_json=True, timeout=10):
    """Test a single API endpoint"""
    req = urllib.request.Request(url, headers=HDRS)
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, context=CTX, timeout=timeout)
        elapsed = time.time() - t0
        body = resp.read().decode('utf-8', errors='replace')
        print('[OK] %s | HTTP %d | %.1fs | %d bytes' % (name, resp.status, elapsed, len(body)))
        
        if parse_json:
            try:
                data = json.loads(body)
                # Print first 500 chars of JSON
                s = json.dumps(data, ensure_ascii=False)[:500]
                print('     JSON: %s' % s)
            except:
                pass
        else:
            # Search for prices in HTML
            prices = re.findall(r'(\d{3}\.\d{2})', body)
            if prices:
                print('     Prices: %s' % prices[:10])
            print('     BODY: %s' % body[:300])
        
        return True, body
    except Exception as e:
        elapsed = time.time() - t0
        print('[FAIL] %s | %.1fs | Error: %s' % (name, elapsed, str(e)[:150]))
        return False, str(e)

print('='*60)
print('  API Connectivity Diagnostics for GitHub Actions')
print('  Time: ' + __import__('datetime').datetime.now().isoformat())
print('='*60)

results = []

# ---- Group 1: International APIs (most likely to work in GH Actions) ----
print('\n--- Group 1: International APIs ---')

results.append(test(
    'Frankfurter (USD/CNY)',
    'https://api.frankfurter.app/latest?from=USD&to=CNY'
))

results.append(test(
    'Metals.dev (gold)',
    'https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz'
))

results.append(test(
    'Metal-Price-API',
    'https://api.metal-price-api.com/v1/latest?base=USD&currencies=CNY&api_key=demo'
))

results.append(test(
    'Kitco Gold',
    'https://www.kitco.com/charts.live.html',
    parse_json=False
))

# ---- Group 2: Chinese Financial APIs ----
print('\n--- Group 2: Chinese Financial APIs ---')

results.append(test(
    'EastMoney Kline AU9999',
    'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=113.AU9999&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt=1'
))

results.append(test(
    'EastMoney Push2',
    'https://push2.eastmoney.com/qt/clist?pn=1&pz=5&po=1&np=1&ut=bd1a6f3c&fltt=2&invt=2&fid=f3&fs=m:0+t:13&fields=f12,f14,f2,f3'
))

results.append(test(
    'Sina Gold',
    'https://finance.sina.com/service/cp/cnhjz/gold/api/openApi.php/CNHJZ_Gold.getGoldPriceBaseInfo'
))

# ---- Group 3: cngold.org ----
print('\n--- Group 3: cngold.org ---')

ok, body = test(
    'cngold Bank Gold',
    'https://www.cngold.org/img_date/bank_gold.html',
    parse_json=False
)
if ok:
    # Check if page has any price data embedded
    has_price = bool(re.search(r'\d{3}\.\d{2}', body))
    has_bank = bool(re.search(r'工商银行|建设银行|中国银行', body))
    print('     Has prices: %s | Has bank names: %s' % (has_price, has_bank))

# ---- Group 4: Alternative international sources ----
print('\n--- Group 4: Alternative Sources ---')

results.append(test(
    'Open Exchange Rates',
    'https://open.er-api.com/v6/latest/USD'
))

results.append(test(
    'ExchangeRate-API',
    'https://open.er-api.com/v6/latest/USD'
))

results.append(test(
    'Fixer.io (demo)',
    'http://data.fixer.io/api/latest?access_key=demo&symbols=CNY&base=USD'
))

# Summary
print('\n' + '='*60)
print('  SUMMARY')
print('='*60)
ok_count = sum(1 for r, _ in results if r)
total = len(results)
print('  Passed: %d / %d (%.0f%%)' % (ok_count, total, 100.0 * ok_count / total if total > 0 else 0))
print('='*60)
