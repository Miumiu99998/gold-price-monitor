#!/usr/bin/env python3
"""
diagnose_raw.py - 在GitHub Actions上诊断所有API的原始返回数据
输出每个数据源的原始值，用于定位价格不准+不变的根因
"""
import urllib.request, json, ssl, re, time, sys, os

sys.stdout = open(os.environ.get('GITHUB_OUTPUT', sys.stdout.fileno()) if isinstance(sys.stdout, int) else sys.stdout, 'w') if False else sys.stdout  # no-op, just for safety

CTX = ssl.create_default_context()
H = {'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.3-Diag)', 'Accept': '*/*'}

def test(name, url, is_json=True, timeout=15):
    req = urllib.request.Request(url, headers=H)
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, context=CTX, timeout=timeout)
        b = r.read().decode('utf-8', errors='replace')
        el = time.time() - t0
        print('\n===== %s =====' % name)
        print('URL: %s' % url)
        print('HTTP: %d | Time: %.1fs | Size: %d bytes' % (r.status_code, el, len(b)))
        
        if is_json:
            try:
                d = json.loads(b)
                print('JSON keys: %s' % list(d.keys())[:10])
                print('RAW JSON (first 800):')
                print(json.dumps(d, ensure_ascii=False)[:800])
            except:
                print('NOT VALID JSON:')
                print(b[:500])
        else:
            # HTML - extract prices
            prices = re.findall(r'\$?([\d,]+\.\d{2})', b)
            print('Found $/price patterns: %s' % prices[:20])
            print('BODY (first 600):')
            print(b[:600])
            
        return True, b
    except Exception as e:
        print('\n===== %s ===== FAIL =====' % name)
        print('Error (%.1fs): %s' % (time.time()-t0, str(e)[:200]))
        return False, str(e)

print('='*60)
print('  RAW API DIAGNOSTICS')
print('  Time: %s UTC' % time.strftime('%Y-%m-%d %H:%M:%S'))
print('='*60)

# ---- Group A: Gold Price APIs ----
print('\n\n### GROUP A: GOLD PRICE SOURCES ###')

test('A1: metals-dev (USD/oz)', 
    'https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz')

test('A2: metals-dev (CNY/g)', 
    'https://api.metals.dev/v1/latest?api_key=demo&currency=CNY&unit=gram')

test('A3: metal-price-api (XAU rate)', 
    'https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo')

test('A4: metal-price-api (direct CNY)', 
    'https://api.metal-price-api.com/v1/latest?base=XAU&currencies=CNY&api_key=demo')

test('A5: Kitco Live Gold', 
    'https://www.kitco.com/charts.live.html', is_json=False)

test('A6: Kitco Gold Price (JSON-ish)', 
    'https://www.kitco.com/gold-price-today-usa/', is_json=False)

# ---- Group B: Exchange Rate ----
print('\n\n### GROUP B: EXCHANGE RATES ###')

test('B1: Frankfurter USD/CNY', 
    'https://api.frankfurter.app/latest?from=USD&to=CNY')

test('B2: Frankfurter EUR/CNY (alt)', 
    'https://api.frankfurter.app/latest?from=EUR&to=CNY')

test('B3: Open ER API USD', 
    'https://open.er-api.com/v6/latest/USD')

test('B4: Fixer.io Demo', 
    'http://data.fixer.io/api/latest?access_key=demo&symbols=CNY')

# ---- Group C: Chinese Finance (likely fail from US) ----
print('\n\n### GROUP C: CHINESE SOURCES (from US) ###')

test('C1: cngold bank gold page', 
    'https://www.cngold.org/img_date/bank_gold.html', is_json=False)

test('C2: cngold main page', 
    'https://www.cngold.org/', is_json=False)

test('C3: EastMoney AU9999 kline', 
    'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=113.AU9999&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt=3')

# ---- Group D: Alternative International Sources ----
print('\n\n### GROUP D: ALTERNATIVE INTERNATIONAL ###')

test('D1: Metals-API (free tier)', 
    'https://metals-api.com/api/latest?access_key=demo&base=USD&currencies=XAU')

test('D2: Gold API (free)', 
    'https://data-asg.goldprice.org/dbXRates/USD', is_json=False)

test('D3: JSONVat gold rates', 
    'https://ratesjson.com/USD/XAU.json')

# ---- Summary & Calculation ----
print('\n\n' + '='*60)
print('  CALCULATION SUMMARY')
print('='*60)

# Try to get a working price from any source
spot_usd = None
usd_cny = None

# Try metals-dev
try:
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', headers=H),
        context=CTX, timeout=12).read().decode())
    if d.get('metals',{}).get('gold',{}).get('price'):
        spot_usd = float(d['metals']['gold']['price'])
        print('Using metals-dev spot: $%.2f/oz' % spot_usd)
except: pass

# Try frankfurter for FX
try:
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request('https://api.frankfurter.app/latest?from=USD&to=CNY', headers=H),
        context=CTX, timeout=12).read().decode())
    if d.get('rates',{}).get('CNY'):
        usd_cny = float(d['rates']['CNY'])
        print('Using frankfurter FX: %.4f' % usd_cny)
except: pass

if spot_usd and usd_cny:
    OZ_G = 31.1034768
    cny_g_spot = (spot_usd * usd_cny) / OZ_G
    bank_g = cny_g_spot * 0.958
    
    print('')
    print('Spot Gold:     $%.2f/oz' % spot_usd)
    print('FX Rate:       %.4f CNY/USD' % usd_cny)
    print('Spot CNY/g:    ¥%.2f' % cny_g_spot)
    print('Bank Paper(×0.958): ¥%.2f' % bank_g)
    print('')
    print('Estimated Bank Prices (with fee markup):')
    print('  招商银行买入: ¥%.2f (+5)' % (bank_g + 5))
    print('  浙商银行买入: ¥%.2f (+4)' % (bank_g + 4))
    print('  工商银行买入: ¥%.2f (+0)' % bank_g)
    print('  建设银行买入: ¥%.2f (+5.5)' % (bank_g + 5.5))
else:
    print('WARNING: Could not get valid price/FX data!')
    if not spot_usd: print('  -> No gold price source worked')
    if not usd_cny: print('  -> No FX rate source worked')

print('\n=== DIAGNOSTIC COMPLETE ===')
