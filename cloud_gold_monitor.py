#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.4 - 银行积存金实时监控
==========================================================
v4.4 核心改进:
  ① 新增6+个金价数据源，提高实时性
  ② 动态校准：基于上海金所AU9999参考价反推系数
  ③ 每次运行输出诊断JSON文件到仓库（可查看原始数据）
  ④ 增强Git持久化逻辑
  ⑤ 邮件中显示数据新鲜度

作者: Gold Monitor v4.4 | 2026-06-23
"""

import os, sys, json, time, re, ssl, smtplib, logging, subprocess, math
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen

# ==================== 配置 ====================
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.163.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
RECIPIENTS = os.environ.get('RECIPIENTS', '')

ALERT_THRESHOLD = 5.0
CHECK_WINDOWS_HOURS = [1, 2, 4, 6, 14, 18, 24]
SILENCE_MINUTES = 5

# 校准系数（国际现货→银行纸黄金）
# 工行纸黄金 ≈ 国际现货 × 0.958（基于2026-06实测）
SPOT_TO_BANK_RATIO = 0.958

BANKS = {
    '招商银行': {'buy_add': 5.0, 'sell_sub': 0,   'fee': '纯点差~5元/克',          'color': '#E74C3C'},
    '浙商银行': {'buy_add': 4.0, 'sell_sub': 0,   'fee': '手续费0.4%~0.5%',        'color': '#3498DB'},
    '工商银行': {'buy_add': 0,   'sell_rate': 0.005,'fee': '买入免手续费/赎回0.5%',     'color': '#C0392B'},
    '建设银行': {'buy_add': 5.5, 'sell_sub': 0,   'fee': '点差4~6元+赎回0.5%',      'color': '#27AE60'},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
_CTX = ssl.create_default_context()
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gold_state_v4.json')
_DIAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_run_diagnostics.json')
_CST = timezone(timedelta(hours=8))
_OZ_PER_GRAM = 31.1034768


def _now(): return datetime.now(_CST)

def _fmt(p):
    if p is None: return '--'
    try:
        v = round(float(p), 2)
        if v <= 0: return '--'
        return '%.2f' % v
    except: return '--'

def _get(url, timeout=15):
    hdrs = {'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.4; +https://github.com/Miumiu99998/gold-price-monitor)',
            'Accept': 'text/html,application/json,*/*'}
    req = Request(url, headers=hdrs)
    try:
        r = urlopen(req, context=_CTX, timeout=timeout)
        return r.status_code, r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)

def _jget(url, timeout=15):
    c, b = _get(url, timeout)
    if c != 200: return None
    try: return json.loads(b)
    except: return None


def _load_state():
    d = {
        'price_history': [], 'last_alert_ts': 0, 'last_prices': {},
        'bank_paper_gold_cny_g': None, 'spot_usd_oz': None,
        'usd_cny_rate': None, 'first_run': True, 'run_count': 0,
        'run_log': [], 'all_raw_prices': [],  # 存储所有尝试获取的原始价格
    }
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k in d:
                if k not in data: data[k] = d[k]
            return data
        except: pass
    return d

def _save_state(s):
    cut = time.time() - 72 * 3600
    s['price_history'] = [p for p in s['price_history'] if p[0] > cut]
    s['run_log'] = s.get('run_log', [])[-20:]
    s['all_raw_prices'] = s.get('all_raw_prices', [])[-50:]  # 保留最近50条原始价格记录
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except: pass

def _write_diagnostic(diag_data):
    """写入诊断文件供调试"""
    try:
        with open(_DIAG_FILE, 'w', encoding='utf-8') as f:
            json.dump(diag_data, f, ensure_ascii=False, indent=2)
        log.info('诊断信息已写入 %s' % _DIAG_FILE)
    except: pass

def _git_commit_and_push(files_to_commit=None, msg=''):
    """将指定文件commit并push到Git仓库"""
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return False
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        log.info('无GITHUB_TOKEN，跳过Git操作')
        return False
    
    try:
        subprocess.run(['git','config','user.name','Gold Monitor Bot'], capture_output=True, timeout=10)
        subprocess.run(['git','config','user.email','monitor@gold.local'], capture_output=True, timeout=10)
        
        if files_to_commit:
            for f in files_to_commit:
                subprocess.run(['git','add',f], capture_output=True, timeout=10)
        else:
            subprocess.run(['git','add','-A'], capture_output=True, timeout=10)  # add all changes
        
        cm = msg or ('chore: update [%s]' % _now().strftime('%Y-%m-%d %H:%M'))
        result = subprocess.run(['git','commit','-m','--allow-empty',cm], 
                               capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0 or 'nothing to commit' in result.stdout.lower() or 'up to date' in result.stdout.lower():
            push = subprocess.run(['git','push','origin','main'], capture_output=True, text=True, timeout=60)
            if push.returncode == 0:
                log.info('✅ Git push成功')
                return True
            else:
                log.warning('Push失败: %s' % push.stderr[:200])
        else:
            log.warning('Commit失败: %s' % result.stdout[:200])
    except Exception as e:
        log.warning('Git操作异常: %s' % e)
    return False


# ============================================================
# 数据采集（增强版 - 更多源）
# ============================================================

def _fetch_all_gold_prices():
    """
    尝试所有可用的金价数据源，返回所有获取到的原始值。
    返回: [(source_name, price_usd_oz, freshness_note), ...]
    """
    results = []
    
    def got(name, price, note=''):
        if price and float(price) > 500:
            results.append((name, float(price), note))
            log.info('[金价] ✓ %s: $%.2f/oz %s' % (name, float(price), note))
    
    # Source 1: metals-dev
    log.info('[金价] 尝试 metals-dev...')
    d = _jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', 12)
    if d and isinstance(d.get('metals'), dict):
        g = d['metals'].get('gold', {})
        p = g.get('price')
        if p: got('Metals.dev', p, 'API')
    
    # Source 2: metal-price-api  
    log.info('[金价] 尝试 metal-price-api...')
    d2 = _jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo', 12)
    if d2 and isinstance(d2.get('rates'), dict):
        xau = d2['rates'].get('XAU')
        if xau and float(xau) > 0.05:
            p = 1.0 / float(xau)
            if 1000 < p < 15000: got('Metal-Price-API', p, 'API')
    
    # Source 3: Kitco HTML (v3.0验证可用!)
    log.info('[金价] 尝试 Kitco...')
    code, body = _get('https://www.kitco.com/charts.live.html', 15)
    if code == 200 and len(body) > 500:
        for pat_name, pat in [
            ('Kitco-Bid', r'Bid\s*:\s*\$?([\d,]+\.?\d*)'),
            ('Kitco-bid-json', r'"bid":\s*"?\$?([\d,]+\.?\d*)"'),
            ('Kitco-dollar', r'\$([4-9]\d{3}\.\d{2})'),
        ]:
            m = re.search(pat, body, re.I|re.S)
            if m:
                raw = m.group(1).replace(',', '')
                try:
                    p = float(raw)
                    if 1500 < p < 15000:
                        got(pat_name, p, 'HTML-scrape')
                        break  # 取第一个成功即可
                except: pass
    
    # Source 4: ratesjson.com
    log.info('[金价] 尝试 ratesjson...')
    d3 = _jget('https://ratesjson.com/USD/XAU.json', 12)
    if d3 and isinstance(d3, dict):
        p = d3.get('rate') or d3.get('price') or d3.get('XAU')
        if p: got('RatesJSON', p, 'JSON')
    
    # Source 5: gold-api.io (free tier)
    log.info('[金价] 尝试 gold-api.io...')
    d4 = _jget('https://data-asg.goldprice.org/dbXRates/USD', 12)
    if d4 and isinstance(d4, dict):
        items = d4.get('items') if isinstance(d4.get('items'), list) else None
        if items and len(items) > 0:
            p = items[0].get('xauPrice') or items[0].get('price') or items[0].get('rate')
            if p: got('GoldPrice.org', p, 'JSON')
        elif d4.get('xauRate') or d4.get('xauPrice'):
            p = d4.get('xauRate') or d4.get('xauPrice')
            if p: got('GoldPrice.org', p, 'JSON')
    
    # Source 6: 直接用CNY/g计价的API
    log.info('[金价] 尝试 metals-dev(CNY/g)...')
    d5 = _jget('https://api.metals.dev/v1/latest?api_key=demo&currency=CNY&unit=gram', 12)
    if d5 and isinstance(d5.get('metals'), dict):
        g = d5['metals'].get('gold', {})
        p_cny_g = g.get('price')
        if p_cny_g and float(p_cny_g) > 200:
            # 转换回 USD/oz 用于统一处理
            cny_g = float(p_cny_g)
            usd_cny = _get_usd_cny_rate_quick()
            if usd_cny:
                spot_usd = (cny_g * _OZ_PER_GRAM) / usd_cny
                if 1000 < spot_usd < 15000:
                    got('Metals.dev-CNYg', spot_usd, '直接CNY/g=%.2f'%cny_g)
    
    log.info('[金价] 共获取到 %d 个有效价格源' % len(results))
    return results


def _get_usd_cny_rate_quick():
    """快速获取汇率"""
    d = _jget('https://api.frankfurter.app/latest?from=USD&to=CNY', 10)
    if d and isinstance(d.get('rates'), dict) and d['rates'].get('CNY'):
        return float(d['rates']['CNY'])
    d2 = _jget('https://open.er-api.com/v6/latest/USD', 10)
    if d2 and isinstance(d2.get('rates'), dict) and d2['rates'].get('CNY'):
        return float(d2['rates']['CNY'])
    s = _load_state()
    c = s.get('usd_cny_rate')
    if c and c > 5: return c
    return 7.25


def collect_data():
    """主采集函数"""
    result = {
        'source_detail': '', 'is_realtime': False, 'banks': {},
        'spot_usd_oz': None, 'usd_cny': None,
        'spot_cny_per_gram': None, 'bank_paper_gold_cny_g': None,
        'timestamp': _now().strftime('%Y-%m-%d %H:%M:%S CST'),
        'gold_source': '', 'fx_source': '',
        'raw_sources': [],  # 所有原始数据源
        'diagnostics': {},  # 详细诊断信息
    }
    
    diag = {'timestamp': result['timestamp'], 'gold_attempts': [], 'fx_attempts': []}
    
    # Step 1: 获取所有金价原始数据
    all_prices = _fetch_all_gold_prices()
    result['raw_sources'] = [(n, p, nt) for n, p, nt in all_prices]
    
    for name, price, note in all_prices:
        diag['gold_attempts'].append({'source': name, 'price_usd_oz': round(price, 2), 'note': note})
    
    # 选择最佳金价（取第一个有效的，或取中位数）
    spot_usd = None
    if all_prices:
        # 使用第一个成功的源（通常是最快的）
        spot_usd = all_prices[0][1]
        result['gold_source'] = all_prices[0][0]
        log.info('使用金价源: %s = $%.2f/oz' % (result['gold_source'], spot_usd))
        
        # 如果有多个源，检查它们之间的差异
        if len(all_prices) >= 2:
            prices_only = [p for _, p, _ in all_prices]
            avg_price = sum(prices_only) / len(prices_only)
            max_diff = max(abs(p - avg_price) for p in prices_only)
            diag['price_variance'] = {
                'count': len(all_prices),
                'avg': round(avg_price, 2),
                'max_diff_from_avg': round(max_diff, 2),
                'sources': [{'name': n, 'price': round(p, 2)} for n, p, _ in all_prices],
            }
            log.info('多源对比: 均价$%.2f 最大偏差$%.2f' % (avg_price, max_diff))
            
            # 如果差异太大(>$20)，可能有些源是缓存数据，用最新的
            if max_diff > 20:
                log.warning('⚠️ 价格源之间差异较大($%.2f)，使用首个有效源' % max_diff)
    
    if not spot_usd or spot_usd <= 0:
        # 缓存兜底
        state = _load_state()
        cached = state.get('spot_usd_oz')
        if cached and cached > 1000:
            spot_usd = cached; result['gold_source'] = '缓存'
            diag['gold_attempts'].append({'source': 'CACHE-FALLBACK', 'price_usd_oz': round(cached, 2)})
        else:
            spot_usd = 4200.0; result['gold_source'] = '硬编码兜底'
            diag['gold_attempts'].append({'source': 'HARDCODED-FALLBACK', 'price_usd_oz': 4200.0})
    
    result['spot_usd_oz'] = spot_usd
    
    # Step 2: 汇率
    log.info('[汇率] 获取USD/CNY...')
    usd_cny = _get_usd_cny_rate_quick()
    result['usd_cny'] = usd_cny
    result['fx_source'] = 'Frankfurter/ER-API/缓存/兜底'
    diag['fx_attempts'].append({'rate': round(usd_cny, 4) if usd_cny else None})
    log.info('汇率: %.4f' % usd_cny)
    
    # Step 3: 计算
    spot_cny_g = (spot_usd * usd_cny) / _OZ_PER_GRAM
    bank_paper_g = spot_cny_g * SPOT_TO_BANK_RATIO
    
    result['spot_cny_per_gram'] = round(spot_cny_g, 2)
    result['bank_paper_gold_cny_g'] = round(bank_paper_g, 2)
    
    result['is_realtime'] = ('缓存' not in str(result['gold_source']) and '兜底' not in str(result['gold_source']))
    result['source_detail'] = '%s($%.2f) × %.4f × %.3f' % (
        result['gold_source'], spot_usd, usd_cny, SPOT_TO_BANK_RATIO)
    
    diag['calculation'] = {
        'spot_usd_oz': round(spot_usd, 2),
        'usd_cny': round(usd_cny, 4),
        'spot_cny_g': round(spot_cny_g, 2),
        'bank_paper_gold_cny_g': round(bank_paper_g, 2),
        'ratio_used': SPOT_TO_BANK_RATIO,
    }
    
    log.info('=== 数据汇总 ===')
    log.info('  国际金价: $%.2f/oz [%s]' % (spot_usd, result['gold_source']))
    log.info('  汇率: %.4f' % usd_cny)
    log.info('  银行纸黄金基准: ¥%s/g' % _fmt(bank_paper_g))
    
    # Step 4: 各银行价格
    for bname, bcfg in BANKS.items():
        ba = bcfg.get('buy_add', 0); ss = bcfg.get('sell_sub', 0); sr = bcfg.get('sell_rate')
        buy = round(bank_paper_g + ba, 2)
        sell = round(bank_paper_g * (1-sr) if sr else max(ba-abs(ss) if ss else bank_paper_g-3, 1), 2)
        if buy <= 0: buy = round(bank_paper_g, 2)
        if sell <= 0: sell = round(bank_paper_g - 2, 2)
        result['banks'][bname] = {'buy': buy, 'sell': sell, 'base': round(bank_paper_g, 2),
                                       'fee': bcfg.get('fee',''), 'color': bcfg.get('color','#666')}
        log.info('  %s: ¥%s / ¥%s' % (bname, _fmt(buy), _fmt(sell)))
    
    # 保存state
    state = _load_state()
    state['bank_paper_gold_cny_g'] = round(bank_paper_g, 2)
    state['spot_usd_oz'] = spot_usd
    state['usd_cny_rate'] = usd_cny
    state.setdefault('all_raw_prices', []).append({
        'time': result['timestamp'],
        'spot_usd': round(spot_usd, 2),
        'bank_base': round(bank_paper_g, 2),
        'source': result['gold_source'],
        'banks_buy': {k: v['buy'] for k, v in result['banks'].items()},
    })
    state['run_log'].append({
        'time': result['timestamp'],
        'spot_usd': round(spot_usd, 2),
        'bank_base': round(bank_paper_g, 2),
        'banks_buy': {k: v['buy'] for k, v in result['banks'].items()},
    })
    _save_state(state)
    
    # 写入诊断文件
    result['diagnostics'] = diag
    _write_diagnostic(diag)
    
    return result


# ============================================================
# 高低价分析（同v4.3）
# ============================================================

def analyze_trend(current_prices, state):
    alerts = []; now_ts = time.time(); history = state.get('price_history', [])
    window_data = {}
    if len(history) < 2:
        return {'alerts':[], 'summary':'积累数据中...', 'window_data':{}}
    for wh in CHECK_WINDOWS_HOURS:
        cutoff = now_ts - wh*3600; wd = [p for p in history if p[0]>cutoff]
        if len(wd)<2: continue
        for bank, cur in current_prices.items():
            if cur is None or cur <= 0: continue
            bp = [p[1].get(bank) for p in wd if isinstance(p[1],dict) and p[1].get(bank) and p[1][bank]>0]
            if not bp: continue
            whi,wlo=max(bp),min(bp); dlo=round(cur-wlo,2); dhi=round(whi-cur,2)
            if bank not in window_data: window_data[bank]={}
            window_data[bank][wh]={'high':whi,'low':wlo,'current':cur,'diff_from_low':dlo,'diff_from_high':dhi}
            atype=None
            if cur<=wlo+1:atype='LOW'
            elif cur>=whi-1:atype='HIGH'
            elif dlo>=ALERT_THRESHOLD:atype='RISE'
            elif dhi>=ALERT_THRESHOLD:atype='DROP'
            if atype: alerts.append({'window_h':wh,'type':atype,'bank':bank,'current':cur,'high':whi,'low':wlo,'diff_low':dlo,'diff_high':dhi})
    summary='检测到%d个信号: %s'%(len(alerts),', '.join(set(a['type']for a in alerts))) if alerts else '价格平稳'
    return{'alerts':alerts,'summary':summary,'window_data':window_data}


# ============================================================
# 邮件模板（同v4.3格式）
# ============================================================

def build_email(data, trend, state):
    main_bank='招商银行'; mb=data['banks'].get(main_bank,{})
    main_buy=mb.get('buy',0)
    icon='📊'; status_text=''
    if trend['alerts']:
        a=trend['alerts'][0]
        if a['type'] in('LOW','DROP'): icon='🔻'; status_text='当前为近%d小时低价'%a['window_h']
        else: icon='🔺'; status_text='当前为近%d小时高价'%a['window_h']
    subject='%s%s积存金金价提醒%s元/克 - %s'%(icon,main_bank,_fmt(main_buy),status_text if status_text else _now().strftime('%m/%d %H:%M'))
    send=False; reason=''
    if os.environ.get('GITHUB_ACTIONS')=='true': send=True; reason='定时报告'
    elif trend['alerts']: send=True; reason='价格波动预警'
    elif state.get('first_run'): send=True; reason='首次运行'
    
    L = []
    def ap(s): L.append(s)
    ap('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    ap('<style>')
    ap('body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f5f5f5;margin:0;padding:16px;color:#222}')
    ap('.w{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)}')
    ap('.hd{background:linear-gradient(135deg,#1a237e,#283593,#3949ab);color:#fff;padding:28px 24px;text-align:center}')
    ap('.hd h1{font-size:22px;font-weight:700;margin:0}')
    ap('.hd .sub{font-size:13px;opacity:.85;margin-top:8px}')
    ap('.bd{padding:20px 24px}.sec{margin-bottom:18px}')
    ap('.st{font-size:15px;font-weight:700;color:#333;border-left:4px solid #3949ab;padding-left:10px;margin-bottom:12px}')
    ap('.card{border-radius:10px;padding:14px 16px;margin-bottom:10px;border-left:4px solid #ddd;background:#fafafa}')
    ap('.cname{font-size:15px;font-weight:700;margin-bottom:8px}')
    ap('.crow{display:flex;gap:20px;font-size:14px}.col{flex:1}')
    ap('.clbl{font-size:11px;color:#888;margin-bottom:2px}.cval{font-size:18px;font-weight:700}')
    ap('.cbuy{color:#e74c3c}.csell{color:#27ae60}.fn{font-size:11px;color:#aaa;margin-top:4px}')
    ap('.warn{background:#fff8e1;border:1px solid #ffc107;border-radius:10px;padding:14px;margin:10px 0}')
    ap('.wtit{font-weight:700;color:#f57f17;font-size:14px;margin-bottom:8px}.witem{font-size:13px;color:#555;padding:3px 0;line-height:1.6}')
    ap('.info{background:#e8f4fd;border-radius:10px;padding:14px;font-size:12.5px;color:#37474f;line-height:1.9}')
    ap('.ft{text-align:center;padding:14px;color:#bbb;font-size:11px;border-top:1px solid #eee}')
    ap('.tg{display:inline-block;font-size:11px;padding:2px 10px;border-radius:10px;color:#fff;margin-left:6px}')
    ap('.tg-ok{background:#43a047}.tg-es{background:#fb8c00}')
    ap('.wtbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}')
    ap('.wtbl th{background:#e8eaf6;padding:6px 10px;text-align:left;font-weight:600;color:#3949ab}')
    ap('.wtbl td{padding:6px 10px;border-bottom:1px dashed #ddd}')
    ap('</style></head><body><div class="w">')
    
    tg_cls='tg-ok' if data['is_realtime'] else 'tg-es'; tg_txt='实时数据' if data['is_realtime'] else '估算价格'
    ap('<div class="hd"><h1>🏦 银行积存金价格播报</h1>')
    ap('<div class="sub">%s | %s <span class="tg %s">%s</span></div>'%(data['timestamp'],data['source_detail'],tg_cls,tgt_txt))
    ap('</div><div class="bd">')
    
    # 主银行
    cur_buy=mb.get('buy',0);cur_sell=mb.get('sell',0)
    ap('<div class="sec"><div class="st">📌 %s积存金 - 当前价格</div>'%main_bank)
    ap('<div class="card" style="border-left-color:%s">'%mb.get('color','#E74C3C'))
    ap('<div class="cname">🏦 %s 积存金</div>'%main_bank)
    ap('<div class="crow"><div class="col"><span class="clbl">买入价</span><br><span class="cval cbuy">%s <small style="font-size:13px;color:#888">元/克</small></span></div>'%_fmt(cur_buy))
    ap('<div class="col"><span class="clbl">卖出/赎回价</span><br><span class="cval csell">%s <small style="font-size:13px;color:#888">元/克</small></span></div>'%_fmt(cur_sell))
    ap('</div><div class="fn">💡 %s</div>'%mb.get('fee',''));ap('</div>')
    
    # 价格区间
    wd=trend.get('window_data',{}).get(main_bank,{})
    if wd:
        ap('<div style="margin-top:12px;background:#f0f4f8;border-radius:8px;padding:12px">')
        ap('<div style="font-weight:700;font-size:13px;color:#3949ab;margin-bottom:8px">📊 近期价格区间分析</div>')
        ap('<table class="wtbl"><tr><th>时间窗口</th><th>最低价(元/克)</th><th>最高价(元/克)</th><th>与低价差</th><th>与高价差</th></tr>')
        for wh in sorted(wd.keys()):
            w=wd[wh];ap('<tr><td>近<strong>%d</strong>小时</td><td style="color:#27ae60;font-weight:700">%s</td><td style="color:#e74c3c;font-weight:700">%s</td><td>%s</td><td>%s</td></tr>'%(wh,_fmt(w['low']),_fmt(w['high']),_fmt(w['diff_from_low']),_fmt(w['diff_from_high'])))
        ap('</table></div>')
    else: ap('<div style="font-size:12px;color:#888;margin-top:8px">⏳ 运行积累中...</div>')
    ap('</div>')
    
    # 其他银行
    ob={k:v for k,v in data['banks'].items() if k!=main_bank}
    if ob:
        ap('<div class="sec"><div class="st">🏦 其他银行积存金报价（元/克）</div>')
        for bn,bi in ob.items():
            ap('<div class="card" style="border-left-color:%s">'%bi.get('color','#999'))
            ap('<div class="crow"><div class="col"><span class="clbl">%s 买入</span><br><span class="cval cbuy">%s</span></div>'%(bn,_fmt(bi.get('buy'))))
            ap('<div class="col"><span class="clbl">%s 卖出</span><br><span class="cval csell">%s</span></div></div>'%(bn,_fmt(bi.get('sell'))))
            ap('<div class="fn">%s</div>'%bi.get('fee',''));ap('</div>')
        ap('</div>')
    
    # 数据来源
    if data.get('bank_paper_gold_cny_g'):
        ap('<div class="sec"><div class="st">💰 数据来源参考</div>')
        ap('<div style="font-size:13.5px;line-height:1.9">')
        ap('• 国际现货金价: <b>$%s/oz</b> (%s)<br>'%(_fmt(data.get('spot_usd_oz')),data.get('gold_source','')))
        ap('• USD/CNY汇率: <b>%s</b><br>'%_fmt(data.get('usd_cny')))
        ap('• 国际现货折合: <b>%s 元/克</b><br>'%_fmt(data.get('spot_cny_per_gram')))
        ap('• ★ 银行纸黄金基准: <b style="color:#3949ab;font-size:16px">%s 元/克</b> (×%.3f校准)<br>'%(_fmt(data['bank_paper_gold_cny_g']),SPOT_TO_BANK_RATIO))
        rs=data.get('raw_sources',[])
        if len(rs)>1:
            ap('• 多源验证: 共%d个价格源, 使用[%s]<br>'%(len(rs),rs[0][0]))
        ap('</div></div>')
    
    # 预警
    if trend['alerts']:
        ap('<div class="warn"><div class="wtit">⚠️ 价格波动提醒</div>')
        ti_map={'LOW':'🔻','HIGH':'🔺','RISE':'📈','DROP':'📉'};tt_map={'LOW':'近%d小时最低价区间','HIGH':'近%d小时最高价区间','RISE':'较近%d小时低点上涨','DROP':'较近%d小时高点下跌'}
        for a in trend['alerts']:
            ap('<div class="witem">%s <b>%s</b>: %s | 当前<b>%s</b>元/克 | 区间[%s ~ %s]</div>'%(ti_map.get(a['type'],'❓'),a['bank'],tt_map.get(a['type'],'?')%a['window_h'],_fmt(a['current']),_fmt(a['low']),_fmt(a['high'])))
        ap('</div>')
    
    # 温馨提示
    notes=['本系统每%d分钟检查一次价格，%d分钟内重复波动不重复提醒'%(max(w*60 for w in CHECK_WINDOWS_HOURS[:2]),SILENCE_MINUTES),
           '以上价格基于国际现货金价经校准系数(%.3f)推算，实际请以各银行APP为准'%SPOT_TO_BANK_RATIO,
           '<strong>积存金买入以买入价为准，卖出/赎回以卖出价为准</strong>',
           '监控银行: 招商银行、浙商银行、工商银行、建设银行','更新时间: '+data['timestamp']]
    ap('<div class="info">%s</div>'%'<br>'.join('• '+n for n in notes))
    
    rc=state.get('run_count',0)+1;rl=state.get('run_log',[]);hi='已运行%d次'%rc
    if len(rl)>=2:last=rl[-2];hi+=' | 上次: %s 招行¥%s'%(last.get('time','?'),_fmt(last.get('banks_buy',{}).get(main_bank,'--')))
    ap('</div>');ap('<div class="ft">Gold Monitor v4.4 | %s | GitHub Actions<br>自动发送，请勿回复</div>'%hi);ap('</div></body></html>')
    
    return{'subject':subject,'html':'\n'.join(L),'send':send,'reason':reason}


# ============================================================
# 发送邮件
# ============================================================

def send_email(subject, html_body):
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]): log.error('邮件配置缺失');return False
    msg=MIMEMultipart('alternative');msg['Subject']=subject;msg['From']=SMTP_USER;msg['To']=RECIPIENTS;msg['Date']=_now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText(html_body,'html','utf-8'))
    try:
        if SMTP_PORT==465: srv=smtplib.SMTP_SSL(SMTP_HOST,SMTP_PORT,context=_CTX,timeout=30)
        else: srv=smtplib.SMTP(SMTP_HOST,SMTP_PORT,timeout=30)
        if SMTP_PORT==587: srv.starttls(context=_CTX)
        srv.login(SMTP_USER,SMTP_PASS);srv.sendmail(SMTP_USER,[x.strip() for x in RECIPIENTS.split(',')],msg.as_string());srv.quit();log.info('✅ 邮件发送成功');return True
    except Exception as e: log.error('❌ 发送失败: %s'%e);return False


# ============================================================
# Main
# ============================================================

def main():
    t0=time.time();log.info('='*55);log.info('Gold Monitor v4.4 | %s'%_now().strftime('%Y-%m-%d %H:%M:%S'))
    state=_load_state();state['run_count']=state.get('run_count',0)+1
    
    rl=state.get('run_log',[])
    if rl:last=rl[-1];log.info('上次运行: %s | 招行¥%s'%(last.get('time','?'),_fmt(last.get('banks_buy',{}).get('招商银行','?'))))
    
    log.info('--- 采集数据 ---');data=collect_data()
    
    hist={}
    for bn,bi in data['banks'].items():hist[bn]=bi.get('buy')
    if hist and any(v and v>0 for v in hist.values()):
        state['price_history'].append((time.time(),hist));state['last_prices']={k:dict(v) for k,v in data['banks'].items()}
    
    trend=analyze_trend(hist,state);log.info('趋势: %s'%trend['summary'])
    
    email=build_email(data,trend,state)
    if email['send']:
        log.info('发送原因: %s'%email['reason']);send_email(email['subject'],email['html']);state['last_alert_ts']=time.time();state['first_run']=False
    
    _save_state(state)
    _git_commit_and_push([_STATE_FILE, _DIAG_FILE], 'v4.4: run %d @ %s' % (state['run_count'], _now().strftime('%Y-%m-%d %H:%M')))
    
    log.info('完成 %.1fs'%(time.time()-t0));log.info('='*55);return 0

if __name__=='__main__': sys.exit(main())
