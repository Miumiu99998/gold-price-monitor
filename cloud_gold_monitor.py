#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.0 - 银行积存金实时监控（GitHub Actions版）
==========================================================
数据源策略（多级降级）:
  Level 1: 金投网(cngold.org) 银行纸黄金实时报价
  Level 2: 东方财富/新浪 AU9999 实时行情  
  Level 3: 国际金价(CNY) × 银行系数 → 估算积存金价
  Level 4: 缓存历史数据兜底

邮件: 中文HTML格式，含4家银行报价+高低价追踪
作者: Gold Monitor System | 更新: 2026-06-16
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

# 监控配置
ALERT_THRESHOLD = 5.0        # 元/克，波动超此值触发提醒
CHECK_WINDOWS = [1, 2, 4, 6, 14, 18, 24]  # 检查时间窗口（小时）
SILENCE_MINUTES = 5          # 静默期（分钟）

# 银行积存金系数（基于纸黄金/现货金的溢价估算）
BANK_MARKUPS = {
    '招商银行': {'buy_rate': 1.005, 'sell_rate': -4.5, 'fee_desc': '纯点差~5元/克'},
    '浙商银行': {'buy_rate': 1.004, 'sell_rate': 0.005, 'fee_desc': '手续费0.4%~0.5%'},
    '工商银行': {'buy_rate': 1.000, 'sell_rate': 0.005, 'fee_desc': '买入免/赎回0.5%（新客优惠中）'},
    '建设银行': {'buy_rate': 1.004, 'sell_rate': -5.0, 'fee_desc': '点差4~6元+赎回0.5%'},
}

# 日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# SSL上下文
CTX = ssl.create_default_context()

# 数据文件路径
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gold_state_v4.json')

# 时区
CST = timezone(timedelta(hours=8))


def http_get(url, timeout=15):
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.0; +https://github.com/Miumiu99998/gold-price-monitor)',
        'Accept': 'application/json, text/html, */*; q=0.9',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    }
    req = Request(url, headers=hdrs)
    try:
        resp = urlopen(req, context=CTX, timeout=timeout)
        return resp.status_code, resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def jget(url, timeout=10):
    code, body = http_get(url, timeout)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except:
        return None


def now_cst():
    return datetime.now(CST)


def fmt_price(p):
    if p is None:
        return '--'
    try:
        return '%.2f' % float(p)
    except:
        return str(p)


def load_state():
    default = {
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
                for k, v in default.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            log.warning('加载状态文件失败: %s' % e)
    return default


def save_state(state):
    cutoff = time.time() - 48 * 3600
    state['price_history'] = [p for p in state['price_history'] if p[0] > cutoff]
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning('保存状态文件失败: %s' % e)


# ============================================================
# 数据源 Level 1: 金投网银行贵金属报价
# ============================================================
def fetch_cngold_bank():
    url = 'https://www.cngold.org/img_date/bank_gold.html'
    code, body = http_get(url, timeout=15)
    if code != 200:
        return None
    
    result = {}
    
    patterns = [
        (r'"工行纸黄金\(人民币\)"[^}]*?"latestPrice"\s*:\s*"([\d.]+)"', '工商银行', '纸黄金'),
        (r'"建行AU9995"[^}]*?"latestPrice"\s*:\s*"([\d.]+)"', '建设银行', 'AU9995'),
        (r'"建行AU9999"[^}]*?"latestPrice"\s*:\s*"([\d.]+)"', '建设银行', 'AU9999'),
        (r'"中行纸黄金\(人民币\)"[^}]*?"latestPrice"\s*:\s*"([\d.]+)"', '中国银行', '纸黄金'),
        (r'"农行纸黄金\(人民币\)"[^}]*?"latestPrice"\s*:\s*"([\d.]+)"', '农业银行', '纸黄金'),
        (r'data-price="([\d.]+)"[^>]*>工行纸黄金', '工商银行', '纸黄金'),
        (r'data-price="([\d.]+)"[^>]*>建行AU9995', '建设银行', 'AU9995'),
        (r'data-price="([\d.]+)"[^>]*>中行纸黄金', '中国银行', '纸黄金'),
        (r'data-price="([\d.]+)"[^>]*>农行纸黄金', '农业银行', '纸黄金'),
    ]
    
    for pattern, bank, product in patterns:
        m = re.search(pattern, body)
        if m:
            if bank not in result:
                result[bank] = {}
            result[bank][product] = float(m.group(1))
    
    if not result:
        lines = body.split('\n')
        current_bank = None
        for line in lines:
            if '工商银行' in line:
                current_bank = '工商银行'
            elif '建设银行' in line:
                current_bank = '建设银行'
            elif '中国银行' in line:
                current_bank = '中国银行'
            elif '农业银行' in line:
                current_bank = '农业银行'
            
            if current_bank and ('纸黄金' in line or 'AU9999' in line or 'AU9995' in line):
                prices = re.findall(r'(\d{3}\.\d{2})', line)
                if prices and current_bank not in result:
                    result[current_bank] = {'纸黄金': float(prices[0])}
    
    if result:
        log.info('cngold.org 获取到 %d 家银行价格' % len(result))
        return result
    
    return None


# ============================================================
# 数据源 Level 2: AU9999
# ============================================================
def fetch_au9999():
    sources = [
        ('东方财富', 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=113.AU9999&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56&klt=101&fqt=1&end=20500101&lmt=1'),
        ('新浪财经', 'https://finance.sina.com/service/cp/cnhjz/gold/api/openApi.php/CNHJZ_Gold.getGoldPriceBaseInfo'),
        ('金投网AU9999', 'https://www.cngold.org/img_date/au9999.html'),
    ]
    
    for name, url in sources:
        code, body = http_get(url, timeout=10)
        if code != 200:
            continue
        
        try:
            data = json.loads(body)
            if 'data' in data and isinstance(data['data'], dict):
                klines = data['data'].get('klines', [])
                if klines:
                    last = klines[-1].split(',')
                    if len(last) >= 2:
                        price = float(last[1])
                        log.info('%s AU9999=%.2f' % (name, price))
                        return price
            if 'result' in data:
                r = data['result']
                if isinstance(r, dict) and 'price' in r:
                    log.info('%s price=%.2f' % (name, r['price']))
                    return float(r['price'])
        except:
            pass
        
        prices = re.findall(r'(\d{3}\.\d{2})', body)
        valid = [float(p) for p in prices if 800 <= float(p) <= 1200]
        if valid:
            log.info('%s HTML提取 price=%.2f' % (name, valid[0]))
            return valid[0]
    
    return None


# ============================================================
# 数据源 Level 3: 国际金价 + 汇率
# ============================================================
def fetch_international_gold():
    rate_data = jget('https://api.frankfurter.app/latest?from=USD&to=CNY')
    usd_cny = None
    if rate_data and 'rates' in rate_data:
        usd_cny = rate_data['rates'].get('CNY')
    if not usd_cny:
        usd_cny = 7.25
        log.warning('使用估算汇率 %.2f' % usd_cny)
    
    spot_usd = None
    spot_source = ''
    
    kitco_code, kitco_body = http_get('https://www.kitco.com/charts.live.html', timeout=10)
    if kitco_code == 200:
        m = re.search(r'Bid\s*:\s*\$?([\d,.]+)', kitco_body)
        if m:
            spot_usd = float(m.group(1).replace(',', ''))
            spot_source = 'Kitco'
    
    if not spot_usd:
        metals = jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz')
        if metals and 'metals' in metals:
            gold = metals['metals'].get('gold', {})
            if gold:
                spot_usd = gold.get('price')
                spot_source = 'Metals.dev'
    
    if not spot_usd:
        mpa = jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=CNY&api_key=demo')
        if mpa:
            rates = mpa.get('rates', {})
            if 'XAU' in rates:
                spot_usd = 1 / rates['XAU']
                spot_source = 'Metal-Price-API'
    
    if spot_usd:
        cny_per_gram = (spot_usd * usd_cny) / 31.1035
        log.info('国际金价: $%.2f/oz x %.2f = CNY %.2f/g (%s)' % (
            spot_usd, usd_cny, cny_per_gram, spot_source))
        return cny_per_gram, spot_source
    
    return None, None


# ============================================================
# 主数据采集函数
# ============================================================
def collect_all_bank_prices():
    result = {
        'source': '',
        'is_realtime': False,
        'banks': {},
        'base_price': None,
        'timestamp': now_cst().strftime('%Y-%m-%d %H:%M:%S CST'),
    }
    
    # Level 1: cngold.org 真实银行报价
    cngold_data = fetch_cngold_bank()
    if cngold_data:
        result['source'] = '金投网(cngold.org)实时报价'
        result['is_realtime'] = True
        
        base = None
        for bank, products in cngold_data.items():
            for prod, price in products.items():
                if base is None:
                    base = price
                
                target_bank = bank
                if bank == '农业银行':
                    continue
                
                markup = BANK_MARKUPS.get(bank, BANK_MARKUPS['工商银行'])
                
                if isinstance(markup.get('sell_rate'), (int, float)) and markup['sell_rate'] < 0:
                    buy_price = round(price * markup['buy_rate'], 2)
                    sell_price = round(price + markup['sell_rate'], 2)
                else:
                    buy_price = round(price * markup['buy_rate'], 2)
                    sell_rate = abs(markup.get('sell_rate', 0))
                    sell_price = round(price * (1 - sell_rate), 2)
                
                result['banks'][target_bank] = {
                    'buy': buy_price,
                    'sell': sell_price,
                    'raw_paper_gold': price,
                    'fee_desc': markup['fee_desc'],
                }
        
        result['base_price'] = base
        if result['banks']:
            return result
    
    # Level 2: AU9999
    au9999 = fetch_au9999()
    if au9999:
        result['source'] = '上海黄金交易所AU9999（估算积存金价）'
        result['base_price'] = au9999
        
        for bank, markup in BANK_MARKUPS.items():
            if isinstance(markup.get('sell_rate'), (int, float)) and markup['sell_rate'] < 0:
                buy = round(au9999 * markup['buy_rate'], 2)
                sell = round(au9999 + markup['sell_rate'], 2)
            else:
                buy = round(au9999 * markup['buy_rate'], 2)
                sr = abs(markup.get('sell_rate', 0))
                sell = round(au9999 * (1 - sr), 2)
            
            result['banks'][bank] = {
                'buy': buy,
                'sell': sell,
                'raw_base': au9999,
                'fee_desc': markup['fee_desc'],
            }
        return result
    
    # Level 3: 国际金价
    intl_price, source = fetch_international_gold()
    if intl_price:
        result['source'] = '国际现货金价(%s) → 估算积存金价' % source
        result['base_price'] = intl_price
        
        for bank, markup in BANK_MARKUPS.items():
            adj = intl_price * 1.70
            
            if isinstance(markup.get('sell_rate'), (int, float)) and markup['sell_rate'] < 0:
                buy = round(adj * markup['buy_rate'], 2)
                sell = round(adj + markup['sell_rate'], 2)
            else:
                buy = round(adj * markup['buy_rate'], 2)
                sr = abs(markup.get('sell_rate', 0))
                sell = round(adj * (1 - sr), 2)
            
            result['banks'][bank] = {
                'buy': buy,
                'sell': sell,
                'raw_intl': intl_price,
                'fee_desc': markup['fee_desc'],
            }
        return result
    
    # Level 4: 兜底
    result['source'] = '无法获取实时数据（使用缓存）'
    state = load_state()
    if state.get('last_prices'):
        result['banks'] = state['last_prices']
        result['base_price'] = state.get('baseline', {}).get('avg')
    return result


# ============================================================
# 高低价分析
# ============================================================
def analyze_price_trend(current_prices, state):
    alerts = []
    now_ts = time.time()
    history = state.get('price_history', [])
    
    if not history:
        return {'alerts': [], 'summary': '首次运行，暂无历史数据'}
    
    for window_h in CHECK_WINDOWS:
        cutoff = now_ts - window_h * 3600
        window_data = [p for p in history if p[0] > cutoff]
        
        if not window_data:
            continue
        
        for bank, cur_price in current_prices.items():
            if cur_price is None:
                continue
            
            bank_prices = []
            for ts, prices in window_data:
                if bank in prices and prices[bank] is not None:
                    bank_prices.append(prices[bank])
            
            if not bank_prices:
                continue
            
            w_high = max(bank_prices)
            w_low = min(bank_prices)
            
            diff_high = cur_price - w_low
            diff_low = w_high - cur_price
            
            alert_type = None
            if cur_price <= w_low + 0.5:
                alert_type = 'near_low'
            elif cur_price >= w_high - 0.5:
                alert_type = 'near_high'
            elif diff_high >= ALERT_THRESHOLD:
                alert_type = 'drop'
            elif diff_low >= ALERT_THRESHOLD:
                alert_type = 'rise'
            
            if alert_type:
                alerts.append({
                    'window': window_h,
                    'type': alert_type,
                    'bank': bank,
                    'current': cur_price,
                    'high': w_high,
                    'low': w_low,
                    'diff_from_low': diff_high,
                    'diff_from_high': diff_low,
                })
    
    if alerts:
        types = set(a['type'] for a in alerts)
        banks_alerted = set(a['bank'] for a in alerts)
        summary = '检测到%d个信号: %s | 涉及银行: %s' % (
            len(alerts), ', '.join(types), ', '.join(banks_alerted))
    else:
        summary = '价格平稳，未触发预警条件'
    
    return {'alerts': alerts, 'summary': summary}


# ============================================================
# 中文邮件模板
# ============================================================
def build_email_html(data, trend_analysis, state):
    should_send = False
    send_reason = ''
    
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        should_send = True
        send_reason = '定时报告'
    elif trend_analysis['alerts']:
        should_send = True
        send_reason = '价格波动预警'
    elif state.get('first_run', True):
        should_send = True
        send_reason = '首次运行'
    
    main_bank = '招商银行'
    main_buy = data['banks'].get(main_bank, {}).get('buy', 0)
    
    title_icon = '📊'
    title_extra = ''
    
    if trend_analysis['alerts']:
        primary = trend_analysis['alerts'][0]
        if primary['type'] in ('near_low', 'drop'):
            title_icon = '🔻'
            title_extra = '当前为近%d小时低价' % primary['window']
        elif primary['type'] in ('near_high', 'rise'):
            title_icon = '🔺'
            title_extra = '当前为近%d小时高价' % primary['window']
    
    subject = '%s%s积存金金价提醒 %s元/克 - %s' % (
        title_icon,
        main_bank,
        fmt_price(main_buy),
        title_extra if title_extra else now_cst().strftime('%m/%d %H:%M')
    )
    
    # 构建HTML（用字符串拼接避免%格式化冲突）
    lines = []
    lines.append('<!DOCTYPE html><html><head><meta charset="utf-8">')
    lines.append('<style>')
    lines.append('body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f5f5f5;margin:0;padding:20px;color:#333}')
    lines.append('.container{max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1)}')
    lines.append('.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:24px;text-align:center}')
    lines.append('.header h1{margin:0;font-size:22px;font-weight:600}')
    lines.append('.header .time{opacity:0.85;font-size:13px;margin-top:6px}')
    lines.append('.content{padding:20px}')
    lines.append('.section{margin-bottom:20px}')
    lines.append('.section-title{font-size:15px;font-weight:600;color:#555;border-left:4px solid #667eea;padding-left:10px;margin-bottom:12px}')
    lines.append('.bank-card{background:#fafafa;border-radius:8px;padding:14px;margin-bottom:10px;border-left:3px solid #ddd}')
    lines.append('.bank-name{font-size:15px;font-weight:600;margin-bottom:8px}')
    lines.append('.price-row{display:flex;justify-content:space-between;margin:5px 0;font-size:14px}')
    lines.append('.price-label{color:#888}')
    lines.append('.price-value{font-weight:600;font-size:16px}')
    lines.append('.buy-color{color:#e74c3c}')
    lines.append('.sell-color{color:#27ae60}')
    lines.append('.alert-box{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:14px;margin:10px 0}')
    lines.append('.alert-title{font-weight:600;color:#856404;margin-bottom:8px}')
    lines.append('.alert-item{font-size:13px;margin:4px 0;color:#856404}')
    lines.append('.info-box{background:#e8f4f8;border-radius:8px;padding:14px;margin:10px 0;font-size:12px;color:#555;line-height:1.8}')
    lines.append('.footer{text-align:center;padding:16px;color:#aaa;font-size:11px;border-top:1px solid #eee}')
    lines.append('.realtime-tag{display:inline-block;background:#27ae60;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}')
    lines.append('.est-tag{display:inline-block;background:#f39c12;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}')
    lines.append('</style></head><body>')
    
    realtime_tag = '<span class="realtime-tag">实时</span>' if data['is_realtime'] else '<span class="est-tag">估算</span>'
    lines.append('<div class="container"><div class="header">')
    lines.append('<h1>🏦 银行积存金价格播报</h1>')
    lines.append('<div class="time">' + data['timestamp'] + ' | 数据来源: ' + data['source'] + ' ' + realtime_tag + '</div>')
    lines.append('</div><div class="content">')
    
    lines.append('<div class="section"><div class="section-title">📋 各银行积存金报价（元/克）</div>')
    
    bank_order = ['招商银行', '浙商银行', '工商银行', '建设银行']
    for bank in bank_order:
        info = data['banks'].get(bank)
        if not info:
            continue
        
        buy = info.get('buy')
        sell = info.get('sell')
        desc = info.get('fee_desc', '')
        
        border_color = '#667eea'
        lines.append('<div class="bank-card" style="border-left-color:' + border_color + '">')
        lines.append('<div class="bank-name">' + bank + '</div>')
        lines.append('<div class="price-row"><span class="price-label">买入价</span><span class="price-value buy-color">' + fmt_price(buy) + '</span></div>')
        lines.append('<div class="price-row"><span class="price-label">卖出/赎回价</span><span class="price-value sell-color">' + fmt_price(sell) + '</span></div>')
        if desc:
            lines.append('<div style="font-size:11px;color:#aaa;margin-top:4px">' + desc + '</div>')
        lines.append('</div>')
    
    lines.append('</div>')
    
    if data.get('base_price'):
        lines.append('<div class="section"><div class="section-title">💰 参考基准价</div>')
        lines.append('<div style="font-size:14px">基础金价: <b>' + fmt_price(data['base_price']) + ' 元/克</b></div>')
        lines.append('</div>')
    
    if trend_analysis['alerts']:
        lines.append('<div class="alert-box"><div class="alert-title">⚠️ 价格波动预警</div>')
        for alert in trend_analysis['alerts']:
            atype_map = {
                'near_low': '🔻 近%d小时最低价区间',
                'near_high': '🔺 近%d小时最高价区间',
                'drop': '📉 较近%d小时低点上涨%.2f元',
                'rise': '📈 较近%d小时高点下跌%.2f元',
            }
            tpl = atype_map.get(alert['type'], '未知类型')
            if 'diff_from_low' in alert:
                detail = tpl % (alert['window']) if '%' not in str(tpl) else tpl % (alert['window'], alert['diff_from_low'])
            else:
                detail = tpl % (alert['window'])
            lines.append('<div class="alert-item">' + alert['bank'] + ' · ' + detail +
                         ' | 当前' + fmt_price(alert['current']) +
                         ' 低' + fmt_price(alert['low']) +
                         ' 高' + fmt_price(alert['high']) + '</div>')
        lines.append('</div>')
    
    notes = []
    notes.append('• 本系统每' + str(max(w * 60 for w in CHECK_WINDOWS[:2])) + '分钟检查一次价格，' + str(SILENCE_MINUTES) + '分钟内重复波动不重复提醒')
    notes.append('• 以上价格为' + ('银行实时报价' if data['is_realtime'] else '基于市场数据的估算价格') + '，实际交易价格以各银行APP/网点为准')
    notes.append('• 积存金买入建议以<strong>买入价</strong>为准，卖出/赎回以<strong>卖出价</strong>为准')
    notes.append('• 监控银行: 招商银行、浙商银行、工商银行、建设银行')
    notes.append('• 数据更新时间: ' + data['timestamp'])
    
    lines.append('<div class="info-box">' + '<br>'.join(notes) + '</div>')
    
    run_count = state.get('run_count', 0) + 1
    lines.append('<div class="footer">')
    lines.append('Gold Monitor v4.0 | 第' + str(run_count) + '次运行 | Powered by GitHub Actions<br>')
    lines.append('本邮件由监控系统自动发送，请勿直接回复</div>')
    lines.append('</div></div></body></html>')
    
    html = '\n'.join(lines)
    
    return {
        'subject': subject,
        'html': html,
        'should_send': should_send,
        'send_reason': send_reason,
    }


# ============================================================
# 发送邮件
# ============================================================
def send_email(subject, html_content):
    if not SMTP_USER or not SMTP_PASS or not RECIPIENTS:
        log.error('邮件配置不完整: user=%s, recipients=%s' % (bool(SMTP_USER), bool(RECIPIENTS)))
        return False
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = RECIPIENTS
    msg['Date'] = now_cst().strftime('%a, %d %b %Y %H:%M:%S +0800')
    
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))
    
    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=CTX, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            if SMTP_PORT == 587:
                server.starttls(context=CTX)
        
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, RECIPIENTS.split(','), msg.as_string())
        server.quit()
        log.info('✅ 邮件发送成功: ' + subject)
        return True
    except Exception as e:
        log.error('❌ 邮件发送失败: ' + str(e))
        return False


# ============================================================
# 主程序
# ============================================================
def main():
    start = time.time()
    log.info('='*50)
    log.info('银行积存金监控 v4.0 启动')
    log.info('时间: ' + now_cst().strftime('%Y-%m-%d %H:%M:%S'))
    
    state = load_state()
    state['run_count'] = state.get('run_count', 0) + 1
    
    log.info('正在采集银行金价...')
    data = collect_all_bank_prices()
    
    log.info('数据来源: ' + data['source'])
    log.info('实时报价: ' + str(data['is_realtime']))
    
    for bank, info in data['banks'].items():
        log.info('  ' + bank + ': 买入=' + fmt_price(info.get('buy')) + ', 卖出=' + fmt_price(info.get('sell')))
    
    current_for_history = {}
    for bank, info in data['banks'].items():
        current_for_history[bank] = info.get('buy')
    
    if current_for_history:
        state['price_history'].append((time.time(), current_for_history))
        state['last_prices'] = dict((k, v) for k, v in data['banks'].items())
        if data.get('base_price'):
            if 'baseline' not in state:
                state['baseline'] = {}
            state['baseline']['avg'] = data['base_price']
            state['baseline']['time'] = data['timestamp']
    
    trend = analyze_price_trend(current_for_history, state)
    log.info('趋势分析: ' + trend['summary'])
    
    email_info = build_email_html(data, trend, state)
    
    if email_info['should_send']:
        log.info('发送原因: ' + email_info['send_reason'])
        success = send_email(email_info['subject'], email_info['html'])
        if success:
            state['last_alert_time'] = time.time()
            state['first_run'] = False
    else:
        log.info('无需发送邮件: 价格平稳且非首次运行')
    
    save_state(state)
    
    elapsed = time.time() - start
    log.info('运行完成, 耗时 %.1f 秒' % elapsed)
    log.info('='*50)
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
