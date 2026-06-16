#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.1 - 银行积存金实时监控（GitHub Actions版）
==========================================================
核心策略:
  在GitHub Actions(美国服务器)环境下，使用国际免费API获取真实
  国际现货金价和汇率，然后按各银行费率规则推算积存金买入/卖出价。
  
  数据源（按优先级）:
    1. metals-dev.com  → 国际现货金价(USD/oz)
    2. frankfurter.app → USD/CNY汇率
    3. metal-price-api.com → 备用金价
    4. kitco.com       → 备用金价(HTML解析)
    
  银行积存金价推算公式:
    基础价(CNY/g) = 国际金价(USD/oz) × 汇率 ÷ 31.1035
    积存金买入价 = 基础价 × 银行买入系数 + 银行点差
    积存金卖出价 = 基础价 × 银行卖出系数 - 银行点差
    
邮件: 中文HTML，严格按用户要求格式
作者: Gold Monitor v4.1 | 2026-06-16
"""

import os, sys, json, time, re, ssl, smtplib, logging
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ============================================================
# 配置区
# ============================================================
SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.163.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASS = os.environ.get('SMTP_PASS', '')
RECIPIENTS = os.environ.get('RECIPIENTS', '')

ALERT_THRESHOLD = 5.0
CHECK_WINDOWS = [1, 2, 4, 6, 14, 18, 24]
SILENCE_MINUTES = 5

# 各银行费率配置（基于纸黄金/现货金的实际费率规则）
# buy_coefficient: 买入价相对于基础价的系数（1.0=基础价本身）
# sell_point_diff: 卖出价与买入价的固定点差（元/克），负数表示卖出低于买入
# fee_note: 费率说明文字
BANKS = {
    '招商银行': {
        'buy_coef': 1.0055,
        'sell_point_diff': -5.0,
        'fee_note': '纯点差模式，买卖约~5元/克',
        'color': '#E74C3C',
    },
    '浙商银行': {
        'buy_coef': 1.0045,
        'sell_point_diff': -4.0,
        'fee_note': '手续费0.4%~0.5%，赎回0.5%',
        'color': '#3498DB',
    },
    '工商银行': {
        'buy_coef': 1.0000,
        'sell_rate': 0.005,
        'fee_note': '当前买入免手续费，赎回0.5%（新客优惠）',
        'color': '#C0392B',
    },
    '建设银行': {
        'buy_coef': 1.0045,
        'sell_point_diff': -5.5,
        'fee_note': '点差4~6元/克 + 赎回手续费0.5%',
        'color': '#27AE60',
    },
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
CTX = ssl.create_default_context()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gold_state_v4.json')
CST = timezone(timedelta(hours=8))


def http_get(url, timeout=15):
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.1; +https://github.com/Miumiu99998/gold-price-monitor)',
        'Accept': 'application/json, text/html, */*; q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    req = Request(url, headers=hdrs)
    try:
        resp = urlopen(req, context=CTX, timeout=timeout)
        return resp.status_code, resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def jget(url, timeout=15):
    c, b = http_get(url, timeout)
    if c != 200:
        return None
    try:
        return json.loads(b)
    except:
        return None


def now_cst():
    return datetime.now(CST)


def fmt(p):
    if p is None:
        return '--'
    try:
        return '%.2f' % round(float(p), 2)
    except:
        return str(p)


def load_state():
    d = {
        'price_history': [],
        'last_alert_time': 0,
        'last_prices': {},
        'baseline': {},
        'first_run': True,
        'run_count': 0,
    }
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k, v in d.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception as e:
            log.warning('load_state err: %s' % e)
    return d


def save_state(s):
    cut = time.time() - 48 * 3600
    s['price_history'] = [p for p in s['price_history'] if p[0] > cut]
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning('save_state err: %s' % e)


# ============================================================
# 数据采集核心
# ============================================================

def fetch_spot_gold_usd_per_oz():
    """
    获取国际现货金价 (USD/盎司)
    返回: (price, source_name) 或 (None, None)
    """
    
    # Source 1: metals-dev.com (free tier, no key needed for basic)
    log.info('[数据源] 尝试 metals-dev.com ...')
    d = jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', 10)
    if d and 'metals' in d:
        g = d['metals'].get('gold')
        if g and g.get('price'):
            p = float(g['price'])
            log.info('[OK] metals-dev: $%.2f/oz' % p)
            return p, 'Metals.dev'
    
    # Source 2: metal-price-api.com
    log.info('[数据源] 尝试 metal-price-api.com ...')
    d2 = jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo', 10)
    if d2 and 'rates' in d2:
        rates = d2['rates']
        if 'XAU' in rates:
            # XAU rate is oz of gold per 1 USD, so inverse
            p = 1.0 / float(rates['XAU'])
            log.info('[OK] metal-price-api: $%.2f/oz' % p)
            return p, 'Metal-Price-API'
    
    # Source 3: Kitco HTML scraping
    log.info('[数据源] 尝试 Kitco ...')
    c, b = http_get('https://www.kitco.com/charts.live.html', 10)
    if c == 200:
        # Try multiple patterns for bid price
        patterns = [
            r'Bid\s*:\s*\$?([\d,]+\.?\d*)',
            r'"bid":\s*"?\$?([\d,]+\.?\d*)"?',
            r'gold-spot-price[^>]*>([\d,]+\.?\d*)',
            r'spot-price[^>]*>\$?([\d,]+\.?\d*)',
        ]
        for pat in patterns:
            m = re.search(pat, b, re.I)
            if m:
                p_str = m.group(1).replace(',', '')
                try:
                    p = float(p_str)
                    if 1000 < p < 15000:  # sanity check: gold should be $1000-$15000/oz
                        log.info('[OK] Kitco: $%.2f/oz' % p)
                        return p, 'Kitco'
                except:
                    pass
        
        # Fallback: find any dollar amount that looks like gold price
        m2 = re.search(r'\$([4-9]\d{3}\.\d{2})', b)
        if m2:
            p = float(m2.group(1))
            log.info('[OK] Kitco(fallback): $%.2f/oz' % p)
            return p, 'Kitco'
    
    # Source 4: Hardcoded fallback (shouldn't reach here normally)
    log.warning('[FAIL] 所有金价数据源均失败')
    return None, None


def fetch_usd_cny_rate():
    """获取USD/CNY汇率"""
    log.info('[汇率] 尝试 frankfurter.app ...')
    d = jget('https://api.frankfurter.app/latest?from=USD&to=CNY', 10)
    if d and 'rates' in d:
        rate = d['rates'].get('CNY')
        if rate:
            log.info('[OK] 汇率: USD/CNY = %.4f' % rate)
            return rate
    
    # Backup: open.er-api.com
    log.info('[汇率] 尝试 open.er-api.com ...')
    d2 = jget('https://open.er-api.com/v6/latest/USD', 10)
    if d2 and 'rates' in d2:
        rate = d2['rates'].get('CNY')
        if rate:
            log.info('[OK] 汇率(er-api): %.4f' % rate)
            return rate
    
    log.warning('[FAIL] 汇率获取失败，使用估算值 7.25')
    return 7.25


def collect_bank_prices():
    """
    主采集函数：获取国际金价+汇率 → 推算各银行积存金价
    返回: dict with banks data
    """
    result = {
        'source': '',
        'is_realtime': False,
        'banks': {},
        'spot_usd': None,
        'usd_cny': None,
        'base_cny_g': None,
        'timestamp': now_cst().strftime('%Y-%m-%d %H:%M:%S CST'),
        'diagnostics': [],
    }
    
    def diag(msg):
        result['diagnostics'].append(msg)
        log.info(msg)
    
    # Step 1: Get spot gold price
    spot_usd, spot_source = fetch_spot_gold_usd_per_oz()
    result['spot_usd'] = spot_usd
    
    if not spot_usd:
        diag('❌ 无法获取国际金价')
        # Try to use cached baseline
        state = load_state()
        bl = state.get('baseline', {})
        if bl.get('spot_usd') and bl.get('usd_cny') and bl.get('base_cny_g'):
            diag('⚠️ 使用缓存基准价')
            spot_usd = bl['spot_usd']
            usd_cny = bl['usd_cny']
            base_cny_g = bl['base_cny_g']
            result['source'] = '缓存数据 (%s)' % bl.get('source', 'unknown')
        else:
            result['source'] = '所有数据源不可用'
            return result
    else:
        diag('✅ 国际金价: $%.2f/oz [%s]' % (spot_usd, spot_source))
    
    # Step 2: Get exchange rate
    usd_cny = fetch_usd_cny_rate() if 'usd_cny' not in dir() else usd_cny
    result['usd_cny'] = usd_cny
    
    # Step 3: Convert to CNY per gram
    # Formula: (USD/oz) × (CNY/USD) ÷ (31.1035 g/oz) = CNY/g
    OZ_TO_GRAM = 31.1035
    base_cny_g = (spot_usd * usd_cny) / OZ_TO_GRAM if 'base_cny_g' not in dir() else base_cny_g
    result['base_cny_g'] = round(base_cny_g, 2)
    
    diag('✅ 基础金价: ¥%.2f/g (=$%.2f/oz × %.4f)' % (base_cny_g, spot_usd, usd_cny))
    
    # Step 4: Calculate each bank's accumulation gold price
    result['source'] = '国际现货金价(%s) × 银行费率推算' % spot_source
    result['is_realtime'] = True  # Based on real market data
    
    for bank_name, cfg in BANKS.items():
        bc = cfg['buy_coef']
        
        # Buy price
        buy = round(base_cny_g * bc, 2)
        
        # Sell price depends on model
        spd = cfg.get('sell_point_diff')
        sr = cfg.get('sell_rate')
        
        if spd is not None:
            # Point-diff model: sell = buy + diff (diff is usually negative)
            sell = round(buy + spd, 2)
        elif sr:
            # Fee-rate model: sell = base × (1 - fee_rate)
            sell = round(base_cny_g * (1 - sr), 2)
        else:
            sell = round(base_cny_g * 0.995, 2)
        
        result['banks'][bank_name] = {
            'buy': buy,
            'sell': sell,
            'base': round(base_cny_g, 2),
            'fee_note': cfg['fee_note'],
            'color': cfg['color'],
        }
        
        diag('  💰 %s: 买入¥%s | 卖出¥%s' % (bank_name, fmt(buy), fmt(sell)))
    
    # Save baseline for future fallback
    state = load_state()
    state['baseline'] = {
        'spot_usd': spot_usd,
        'usd_cny': usd_cny,
        'base_cny_g': round(base_cny_g, 2),
        'source': spot_source,
        'time': result['timestamp'],
    }
    save_state(state)
    
    return result


# ============================================================
# 高低价分析
# ============================================================

def analyze_trend(prices_dict, state):
    alerts = []
    now_ts = time.time()
    history = state.get('price_history', [])
    
    if len(history) < 2:
        return {'alerts': [], 'summary': '数据积累中，需要更多运行次数'}
    
    for wh in CHECK_WINDOWS:
        cutoff = now_ts - wh * 3600
        wd = [p for p in history if p[0] > cutoff]
        if not wd:
            continue
        
        for bank, cur in prices_dict.items():
            if cur is None:
                continue
            bp = [p[1].get(bank) for p in wd if isinstance(p[1], dict) and p[1].get(bank)]
            if not bp:
                continue
            
            w_high = max(bp)
            w_low = min(bp)
            
            if cur <= w_low + 1.0:
                alerts.append({'w': wh, 't': 'LOW', 'b': bank, 'c': cur, 'h': w_high, 'l': w_low})
            elif cur >= w_high - 1.0:
                alerts.append({'w': wh, 't': 'HIGH', 'b': bank, 'c': cur, 'h': w_high, 'l': w_low})
            elif (cur - w_low) >= ALERT_THRESHOLD:
                alerts.append({'w': wh, 't': 'RISE', 'b': bank, 'c': cur, 'h': w_high, 'l': w_low})
            elif (w_high - cur) >= ALERT_THRESHOLD:
                alerts.append({'w': wh, 't': 'DROP', 'b': bank, 'c': cur, 'h': w_high, 'l': w_low})
    
    if alerts:
        summary = '检测到%d个价格信号' % len(alerts)
    else:
        summary = '价格平稳'
    
    return {'alerts': alerts, 'summary': summary}


# ============================================================
# 邮件模板（严格按用户要求格式）
# ============================================================

def build_email(data, trend, state):
    """构建中文HTML邮件"""
    
    # Determine main bank and its buy price for title
    main_bank = '招商银行'
    mb = data['banks'].get(main_bank, {})
    main_buy = mb.get('buy', 0)
    
    # Title icon and suffix based on alerts
    icon = '📊'
    suffix = ''
    
    if trend['alerts']:
        a = trend['alerts'][0]
        if a['t'] in ('LOW', 'DROP'):
            icon = '🔻'
            suffix = '当前为近%d小时低价' % a['w']
        elif a['t'] in ('HIGH', 'RISE'):
            icon = '🔺'
            suffix = '当前为近%d小时高价' % a['w']
    
    subject = '%s%s积存金金价提醒 %s元/克 - %s' % (
        icon, main_bank, fmt(main_buy),
        suffix if suffix else now_cst().strftime('%m/%d %H:%M')
    )
    
    # Should send?
    should_send = False
    reason = ''
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        should_send = True
        reason = '定时报告'
    elif trend['alerts']:
        should_send = True
        reason = '价格波动预警'
    elif state.get('first_run'):
        should_send = True
        reason = '首次运行'
    
    # Build HTML
    L = []  # lines
    def A(s): L.append(s)
    
    A('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    A('<style>')
    A('*{box-sizing:border-box;margin:0;padding:0}')
    A('body{font-family:-apple-system,"Microsoft YaHei","PingFang SC",sans-serif;background:#f0f2f5;color:#1a1a1a;line-height:1.6;padding:16px}')
    A('.wrap{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)}')
    A('.hd{background:linear-gradient(135deg,#1a237e 0%,#3949ab 50%,#5c6bc0 100%);color:#fff;padding:28px 24px;text-align:center}')
    A('.hd h1{font-size:22px;font-weight:700;letter-spacing:1px}')
    A('.hd .sub{font-size:13px;opacity:.8;margin-top:8px}')
    A('.bd{padding:20px 24px}')
    A('.sec{margin-bottom:18px}')
    A('.sec-tit{font-size:15px;font-weight:700;color:#333;display:flex;align-items:center;gap:6px;margin-bottom:10px;padding-bottom:6px;border-bottom:2px solid #eee}')
    A('.bank-row{display:flex;align-items:center;padding:12px 14px;margin-bottom:8px;border-radius:10px;background:#fafbfc;border-left:4px solid #ccc}')
    A('.bank-name{flex:0 0 90px;font-weight:700;font-size:14px}')
    A('.bank-prices{flex:1;display:flex;gap:16px;font-size:14px}')
    A('.bp{display:flex;flex-direction:column}')
    A('.bp-l{font-size:11px;color:#888}')
    A('.bp-v{font-size:17px;font-weight:700}')
    A('.buy-c{color:#e74c3c}')
    A('.sell-c{color:#27ae60}')
    A('.fee-txt{font-size:11px;color:#aaa;margin-top:2px}')
    A('.alert-card{background:#fff8e1;border:1px solid #ffc107;border-radius:10px;padding:14px;margin:10px 0}')
    A('.alert-tit{font-weight:700;color:#f57f17;font-size:14px;margin-bottom:8px}')
    A('.alert-item{font-size:13px;color:#666;padding:3px 0}')
    A('.info-card{background:#e3f2fd;border-radius:10px;padding:14px;font-size:12px;color:#455a64;line-height:1.9}')
    A('.ft{text-align:center;padding:14px;color:#bbb;font-size:11px;border-top:1px solid #eee}')
    A('.tag{display:inline-block;font-size:11px;padding:2px 10px;border-radius:10px;color:#fff;margin-left:6px}')
    A('.tag-ok{background:#43a047}')
    A('.tag-es{background:#fb8c00}')
    A('</style></head><body>')
    
    A('<div class="wrap">')
    
    # Header
    tag_cls = 'tag-ok' if data['is_realtime'] else 'tag-es'
    tag_txt = '实时数据' if data['is_realtime'] else '估算价格'
    A('<div class="hd">')
    A('<h1>🏦 银行积存金价格播报</h1>')
    A('<div class="sub">%s | 来源: %s <span class="tag %s">%s</span></div>' % (
        data['timestamp'], data['source'], tag_cls, tag_txt))
    A('</div>')
    
    A('<div class="bd">')
    
    # Section 1: Bank Prices
    A('<div class="sec"><div class="sec-tit">📋 各银行积存金报价（元/克）</div>')
    
    for bname, bcfg in BANKS.items():
        info = data['banks'].get(bname, {})
        buy_v = info.get('buy')
        sell_v = info.get('sell')
        fnote = info.get('fee_note', '')
        clr = info.get('color', '#666')
        
        A('<div class="bank-row" style="border-left-color:%s">' % clr)
        A('<div class="bank-name">%s</div>' % bname)
        A('<div class="bank-prices">')
        A('<div class="bp"><span class="bp-l">买入价</span><span class="bp-v buy-c">%s</span></div>' % fmt(buy_v))
        A('<div class="bp"><span class="bp-l">卖出/赎回价</span><span class="bp-v sell-c">%s</span></div>' % fmt(sell_v))
        A('</div>')
        A('</div>')
        if fnote:
            A('<div style="font-size:11px;color:#aaa;padding:0 14px 8px;margin-top:-6px">💡 %s: %s</div>' % (bname, fnote))
    
    A('</div>')  # sec
    
    # Section 2: Reference Price
    if data.get('base_cny_g'):
        A('<div class="sec"><div class="sec-tit">💰 参考基准</div>')
        A('<div style="font-size:14px;padding:4px 0">')
        A('• 国际现货金价: <b>$%s/oz</b><br>' % fmt(data.get('spot_usd')))
        A('• 美元/人民币汇率: <b>%s</b><br>' % fmt(data.get('usd_cny')))
        A('• 折合人民币基础金价: <b style="color:#3949ab;font-size:16px">%s 元/克</b>' % fmt(data['base_cny_g']))
        A('</div></div>')
    
    # Section 3: Alerts
    if trend['alerts']:
        A('<div class="alert-card"><div class="alert-tit">⚠️ 价格波动提醒</div>')
        for a in trend['alerts']:
            type_icons = {'LOW':'🔻','HIGH':'🔺','RISE':'📈','DROP':'📉'}
            type_texts = {
                'LOW':'近%d小时最低价区间',
                'HIGH':'近%d小时最高价区间',
                'RISE':'较近%d小时低点上涨%.2f元',
                'DROP':'较近%d小时高点下跌%.2f元',
            }
            ti = type_icons.get(a['t'], '❓')
            tt = type_texts.get(a['t'], '?')
            if '%' in str(tt) and 'diff' not in str(a):
                txt = tt % a['w']
            elif '%' in str(tt):
                txt = tt % (a['w'], abs(a['c'] - a['l']))
            else:
                txt = tt % a['w']
            
            A('<div class="alert-item">%s <b>%s</b>: %s | 当前%s | 区间[%s ~ %s]</div>' % (
                ti, a['b'], txt, fmt(a['c']), fmt(a['l']), fmt(a['h'])))
        A('</div>')
    
    # Section 4: Info notes
    notes = []
    notes.append('本系统每%d分钟检查一次价格，%d分钟内重复波动不重复提醒' % (
        max(w * 60 for w in CHECK_WINDOWS[:2]), SILENCE_MINUTES))
    notes.append('以上价格基于国际现货金价按各银行费率规则推算，实际交易价格以各银行APP/网点为准')
    notes.append('积存金<strong>买入</strong>请以买入价为准，<strong>卖出/赎回</strong>请以卖出价为准')
    notes.append('监控银行: 招商银行、浙商银行、工商银行、建设银行')
    notes.append('数据更新: %s' % data['timestamp'])
    
    A('<div class="info-card">%s</div>' % '<br>'.join('• ' + n for n in notes))
    
    # Footer
    rc = state.get('run_count', 0) + 1
    A('</div>')  # bd
    A('<div class="ft">Gold Monitor v4.1 | 第%d次运行 | GitHub Actions<br>自动发送，请勿回复</div>' % rc)
    A('</div>')  # wrap
    A('</body></html>')
    
    html = '\n'.join(L)
    
    return {
        'subject': subject,
        'html': html,
        'send': should_send,
        'reason': reason,
    }


# ============================================================
# 发送邮件
# ============================================================

def send_mail(subject, html_body):
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]):
        log.error('邮件配置不完整')
        return False
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = RECIPIENTS
    msg['Date'] = now_cst().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    
    try:
        if SMTP_PORT == 465:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=CTX, timeout=30)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            if SMTP_PORT == 587:
                srv.starttls(context=CTX)
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(SMTP_USER, [r.strip() for r in RECIPIENTS.split(',')], msg.as_string())
        srv.quit()
        log.info('✅ 邮件发送成功: %s' % subject)
        return True
    except Exception as e:
        log.error('❌ 发送失败: %s' % e)
        return False


# ============================================================
# Main
# ============================================================

def main():
    t0 = time.time()
    log.info('='*55)
    log.info('Gold Monitor v4.1 启动 | %s' % now_cst().strftime('%Y-%m-%d %H:%M:%S'))
    
    state = load_state()
    state['run_count'] = state.get('run_count', 0) + 1
    
    # Collect data
    log.info('--- 开始采集数据 ---')
    data = collect_bank_prices()
    
    log.info('来源: %s' % data['source'])
    log.info('实时: %s' % data['is_realtime'])
    
    # Record history
    hist_prices = {}
    for bn, bi in data['banks'].items():
        hist_prices[bn] = bi.get('buy')
        log.info('  %s => 买入=%s 卖出=%s' % (bn, fmt(bi.get('buy')), fmt(bi.get('sell'))))
    
    if hist_prices and any(v for v in hist_prices.values()):
        state['price_history'].append((time.time(), hist_prices))
        state['last_prices'] = {k: dict(v) for k, v in data['banks'].items()}
    
    # Analyze
    trend = analyze_trend(hist_prices, state)
    log.info('趋势: %s' % trend['summary'])
    
    # Build & send email
    email = build_email(data, trend, state)
    
    if email['send']:
        log.info('发送原因: %s' % email['reason'])
        ok = send_mail(email['subject'], email['html'])
        if ok:
            state['last_alert_time'] = time.time()
            state['first_run'] = False
    else:
        log.info('无需发送邮件')
    
    save_state(state)
    log.info('完成, 耗时 %.1fs' % (time.time() - t0))
    log.info('='*55)
    return 0


if __name__ == '__main__':
    sys.exit(main())
