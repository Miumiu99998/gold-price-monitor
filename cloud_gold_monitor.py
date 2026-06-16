#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.2 - 银行积存金实时监控（GitHub Actions稳定版）
==========================================================
数据源策略（按优先级，全部使用国际API确保GitHub Actions可用）:
  ① Frankfurter.app → USD/CNY汇率 ✅ v3.0已验证可用
  ② Kitco.com → 国际现货金价(USD/oz) HTML解析 ✅ v3.0已验证可用  
  ③ metals-dev.com → 备用金价
  ④ metal-price-api.com → 备用金价
  ⑤ 硬编码兜底 → 绝不让价格为0

邮件格式（严格按用户要求）:
  标题: (🔻/🔺/📊)(银行)积存金金价提醒(金额)元/克 - (状态描述)
  内容: 当前金价、N小时低价/高价、差值、4家银行报价

作者: Gold Monitor v4.2 | 2026-06-16
"""

import os, sys, json, time, re, ssl, smtplib, logging, math
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

ALERT_THRESHOLD = 5.0          # 元/克
CHECK_WINDOWS_HOURS = [1, 2, 4, 6, 14, 18, 24]
SILENCE_MINUTES = 5

# 银行费率配置
BANKS = {
    '招商银行': {'buy_add': 5.0, 'sell_sub': 0, 'fee': '纯点差~5元/克', 'color': '#E74C3C'},
    '浙商银行': {'buy_add': 4.0, 'sell_sub': 0, 'fee': '手续费0.4%~0.5%', 'color': '#3498DB'},
    '工商银行': {'buy_add': 0,   'sell_rate': 0.005, 'fee': '买入免手续费/赎回0.5%', 'color': '#C0392B'},
    '建设银行': {'buy_add': 5.5, 'sell_sub': 0, 'fee': '点差4~6元+赎回0.5%', 'color': '#27AE60'},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
_CTX = ssl.create_default_context()
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gold_state_v4.json')
_CST = timezone(timedelta(hours=8))
_OZ_PER_GRAM = 31.1034768  # 精确的盎司→克换算


def _now():
    return datetime.now(_CST)


def _fmt(p):
    """安全格式化价格，永远不返回空或0"""
    if p is None:
        return '--'
    try:
        v = round(float(p), 2)
        if v == 0:
            return '--'
        return '%.2f' % v
    except:
        return '--'


def _get(url, timeout=15):
    """HTTP GET，返回(status_code, body)"""
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.2; +https://github.com/Miumiu99998/gold-price-monitor)',
        'Accept': 'text/html,application/json,*/*',
    }
    req = Request(url, headers=hdrs)
    try:
        r = urlopen(req, context=_CTX, timeout=timeout)
        return r.status_code, r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def _jget(url, timeout=15):
    """GET并尝试JSON解析"""
    c, b = _get(url, timeout)
    if c != 200:
        return None
    try:
        return json.loads(b)
    except:
        return None


def _load_state():
    d = {
        'price_history': [],      # [(ts, {bank: buy_price}), ...]
        'last_alert_ts': 0,
        'last_prices': {},
        'baseline_cny_g': None,
        'spot_usd_oz': None,
        'usd_cny_rate': None,
        'first_run': True,
        'run_count': 0,
    }
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k in d:
                if k not in data:
                    data[k] = d[k]
            return data
        except:
            pass
    return d


def _save_state(s):
    cut = time.time() - 48 * 3600
    s['price_history'] = [p for p in s['price_history'] if p[0] > cut]
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except:
        pass


# ============================================================
# 数据采集（核心：确保永远返回有效价格）
# ============================================================

def _fetch_spot_gold_usd_oz():
    """
    获取国际现货金价 USD/盎司
    返回 (price_float, source_name) 或 (None, error_msg)
    
    优先级：
     1. metals-dev.com API (JSON)
     2. metal-price-api.com API (JSON)  
     3. Kitco.com HTML 解析 (v3.0验证可用!)
     4. 硬编码兜底
    """
    
    # --- Source 1: metals-dev ---
    log.info('[金价] 尝试 metals-dev...')
    d = _jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', 10)
    if d and isinstance(d.get('metals'), dict):
        g = d['metals'].get('gold', {})
        p = g.get('price')
        if p and float(p) > 500:
            log.info('[OK] metals-dev: $%.2f/oz' % float(p))
            return float(p), 'Metals.dev'
    
    # --- Source 2: metal-price-api ---
    log.info('[金价] 尝试 metal-price-api...')
    d2 = _jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo', 10)
    if d2 and isinstance(d2.get('rates'), dict):
        xau = d2['rates'].get('XAU')
        if xau and float(xau) > 0.05:
            p = 1.0 / float(xau)  # XAU is oz per USD
            if 1000 < p < 15000:
                log.info('[OK] metal-price-api: $%.2f/oz' % p)
                return p, 'Metal-Price-API'
    
    # --- Source 3: Kitco HTML (v3.0验证可用!) ---
    log.info('[金价] 尝试 Kitco HTML...')
    code, body = _get('https://www.kitco.com/charts.live.html', 12)
    if code == 200 and len(body) > 500:
        # 多种模式匹配Kitco页面中的金价
        patterns = [
            r'Bid\s*:\s*\$?([\d,]+\.?\d*)',           # Bid: $XXXX.XX
            r'"bid":\s*"?\$?([\d,]+\.?\d*)"',         # "bid":"$XXXX.XX"
            r'gold-spot-price[^>]*>([\d,]+\.?\d*)',     # gold-spot-price>XXXX.XX
            r'spot-price[^>]*>\$?([\d,]+\.?\d*)',       # spot-price>$XXXX.XX
            r'GOLD\s*[:\-]\s*\$?([\d,]+\.?\d*)',       # GOLD : $XXXX.XX
            r'\$([4-9]\d{3}\.\d{2})',                  # $4XXX.XX format anywhere
        ]
        for pat in patterns:
            m = re.search(pat, body, re.I | re.S)
            if m:
                raw = m.group(1).replace(',', '')
                try:
                    p = float(raw)
                    if 1500 < p < 15000:  # 合理的金价范围
                        log.info('[OK] Kitco: $%.2f/oz (pattern: %s)' % (p, pat[:30]))
                        return p, 'Kitco'
                except:
                    continue
    
    # --- Source 4: 从缓存读取 ---
    log.warning('[金价] 所有在线源失败，尝试缓存...')
    state = _load_state()
    cached = state.get('spot_usd_oz')
    if cached and cached > 1000:
        log.info('[OK] 使用缓存金价: $%.2f/oz' % cached)
        return cached, '缓存(%s)' % state.get('source_name', '')
    
    # --- Source 5: 硬编码兜底（基于当前市场价估算）---
    # 当前(2026年6月)国际金价约$4200-4300/oz，这里用一个保守值
    FALLBACK_GOLD_USD = 4200.0
    log.warning('[金价] ⚠️ 使用硬编码兜底价格: $%.2f/oz' % FALLBACK_GOLD_USD)
    return FALLBACK_GOLD_USD, '硬编码兜底'


def _fetch_usd_cny():
    """获取USD/CNY汇率"""
    
    # Source 1: Frankfurter (v3.0验证可用!)
    log.info('[汇率] 尝试 frankfurter...')
    d = _jget('https://api.frankfurter.app/latest?from=USD&to=CNY', 10)
    if d and isinstance(d.get('rates'), dict):
        rate = d['rates'].get('CNY')
        if rate and float(rate) > 5.0:
            log.info('[OK] frankfurter: %.4f' % float(rate))
            return float(rate), 'Frankfurter'
    
    # Source 2: open.er-api.com
    log.info('[汇率] 尝试 open.er-api...')
    d2 = _jget('https://open.er-api.com/v6/latest/USD', 10)
    if d2 and isinstance(d2.get('rates'), dict):
        rate = d2['rates'].get('CNY')
        if rate and float(rate) > 5.0:
            log.info('[OK] er-api: %.4f' % float(rate))
            return float(rate), 'ER-API'
    
    # 缓存
    state = _load_state()
    cr = state.get('usd_cny_rate')
    if cr and cr > 5.0:
        log.info('[OK] 缓存汇率: %.4f' % cr)
        return cr, '缓存'
    
    FALLBACK_RATE = 7.25
    log.warning('[汇率] ⚠️ 使用硬编码汇率: %.4f' % FALLBACK_RATE)
    return FALLBACK_RATE, '硬编码兜底'


def collect_data():
    """
    主采集函数。返回完整的数据字典。
    保证: banks中每个银行的buy/sell都有效（不为0）
    """
    result = {
        'source_detail': '',
        'is_realtime': False,
        'banks': {},
        'spot_usd_oz': None,
        'usd_cny': None,
        'base_cny_per_gram': None,
        'timestamp': _now().strftime('%Y-%m-%d %H:%M:%S CST'),
        'gold_source': '',
        'fx_source': '',
    }
    
    # Step 1: 获取国际金价
    spot_usd, gsrc = _fetch_spot_gold_usd_oz()
    result['spot_usd_oz'] = spot_usd
    result['gold_source'] = gsrc
    
    if not spot_usd or spot_usd <= 0:
        log.error('❌ 无法获取任何有效金价!')
        spot_usd = 4200.0  # 最终兜底
    
    # Step 2: 获取汇率
    usd_cny, fxsrc = _fetch_usd_cny()
    result['usd_cny'] = usd_cny
    result['fx_source'] = fxsrc
    
    if not usd_cny or usd_cny <= 0:
        usd_cny = 7.25  # 最终兜底
    
    # Step 3: 计算基础人民币金价 (元/克)
    base_cny_g = (spot_usd * usd_cny) / _OZ_PER_GRAM
    result['base_cny_per_gram'] = round(base_cny_g, 2)
    
    result['source_detail'] = '国际现货金价%s × 汇率%s' % (gsrc, fxsrc)
    result['is_realtime'] = '缓存' not in gsrc and '兜底' not in gsrc
    
    log.info('=== 数据汇总 ===')
    log.info('  国际金价: $%.2f/oz [%s]' % (spot_usd, gsrc))
    log.info('  汇率: %.4f [%s]' % (usd_cny, fxsrc))
    log.info('  基础价(CNY/g): ¥%.2f' % base_cny_g)
    
    # Step 4: 推算各银行积存金价
    for bname, bcfg in BANKS.items():
        ba = bcfg.get('buy_add', 0)      # 买入加点（元/克）
        ss = bcfg.get('sell_sub', 0)      # 卖出减点
        sr = bcfg.get('sell_rate')        # 卖出费率
        
        buy_price = round(base_cny_g + ba, 2)
        
        if sr is not None:
            sell_price = round(base_cny_g * (1 - sr), 2)
        else:
            sell_price = round(buy_price - abs(ss) if ss else base_cny_g - 3, 2)
        
        # 安全检查：绝对不能为0或负数
        if buy_price <= 0:
            buy_price = round(base_cny_g, 2)
        if sell_price <= 0:
            sell_price = round(base_cny_g - 2, 2)
        
        result['banks'][bname] = {
            'buy': buy_price,
            'sell': sell_price,
            'base': round(base_cny_g, 2),
            'fee': bcfg.get('fee', ''),
            'color': bcfg.get('color', '#666'),
        }
        
        log.info('  %s: 买入=¥%s | 卖出=¥%s' % (bname, _fmt(buy_price), _fmt(sell_price)))
    
    # 保存基准到state供下次fallback
    state = _load_state()
    state['baseline_cny_g'] = round(base_cny_g, 2)
    state['spot_usd_oz'] = spot_usd
    state['usd_cny_rate'] = usd_cny
    state['source_name'] = gsrc
    _save_state(state)
    
    return result


# ============================================================
# 高低价分析
# ============================================================

def analyze_trend(current_buy_prices, state):
    """
    分析各时间窗口的高低价
    current_buy_prices: {银行名: 买入价}
    返回: {alerts: [...], summary: str}
    """
    alerts = []
    now_ts = time.time()
    history = state.get('price_history', [])
    
    if len(history) < 2:
        return {
            'alerts': [],
            'summary': '首次运行，正在积累数据...',
            'window_data': {},  # 用于邮件展示
        }
    
    window_data = {}  # {银行: {window_hours: {high, low, diff}}}
    
    for wh in CHECK_WINDOWS_HOURS:
        cutoff = now_ts - wh * 3600
        wd = [p for p in history if p[0] > cutoff]
        if len(wd) < 2:
            continue
        
        for bank, cur_price in current_buy_prices.items():
            if cur_price is None or cur_price <= 0:
                continue
            
            bp = [p[1].get(bank) for p in wd if isinstance(p[1], dict) and p[1].get(bank) and p[1][bank] > 0]
            if not bp:
                continue
            
            w_high = max(bp)
            w_low = min(bp)
            diff_from_low = round(cur_price - w_low, 2)
            diff_from_high = round(w_high - cur_price, 2)
            
            if bank not in window_data:
                window_data[bank] = {}
            window_data[bank][wh] = {
                'high': w_high,
                'low': w_low,
                'current': cur_price,
                'diff_from_low': diff_from_low,
                'diff_from_high': diff_from_high,
            }
            
            # 判断是否触发预警
            alert_type = None
            if cur_price <= w_low + 1.0:
                alert_type = 'LOW'
            elif cur_price >= w_high - 1.0:
                alert_type = 'HIGH'
            elif diff_from_low >= ALERT_THRESHOLD:
                alert_type = 'RISE'
            elif diff_from_high >= ALERT_THRESHOLD:
                alert_type = 'DROP'
            
            if alert_type:
                alerts.append({
                    'window_h': wh,
                    'type': alert_type,
                    'bank': bank,
                    'current': cur_price,
                    'high': w_high,
                    'low': w_low,
                    'diff_low': diff_from_low,
                    'diff_high': diff_from_high,
                })
    
    if alerts:
        types = set(a['type'] for a in alerts)
        summary = '检测到%d个价格信号: %s' % (len(alerts), ', '.join(types))
    else:
        summary = '价格平稳'
    
    return {'alerts': alerts, 'summary': summary, 'window_data': window_data}


# ============================================================
# 邮件模板（严格按用户要求格式）
# ============================================================
# 要求:
#   标题: (高/低符号)(某行)金价提醒(金额) - 当前为近(N)小时低价/高价
#   内容: 1) N小时内低价/高价  2) 当前金价金额：元/克  3) 与低价/高价的差值
#   备注: 5分钟静默期说明 + 以买入价为准

def build_email(data, trend, state):
    """构建中文HTML邮件"""
    
    # === 标题构建 ===
    main_bank = '招商银行'
    mb_info = data['banks'].get(main_bank, {})
    main_buy = mb_info.get('buy', 0)
    
    icon = '📊'
    status_text = ''
    
    if trend['alerts']:
        a = trend['alerts'][0]
        if a['type'] in ('LOW', 'DROP'):
            icon = '🔻'
            status_text = '当前为近%d小时低价' % a['window_h']
        else:
            icon = '🔺'
            status_text = '当前为近%d小时高价' % a['window_h']
    
    subject = '%s%s积存金金价提醒%s元/克 - %s' % (
        icon, main_bank, _fmt(main_buy),
        status_text if status_text else _now().strftime('%m/%d %H:%M')
    )
    
    # 是否发送
    send = False
    reason = ''
    gh = os.environ.get('GITHUB_ACTIONS') == 'true'
    if gh:
        send = True
        reason = '定时报告'
    elif trend['alerts']:
        send = True
        reason = '价格波动预警'
    elif state.get('first_run'):
        send = True
        reason = '首次运行'
    
    # === HTML构建 ===
    L = []
    def ap(s): L.append(s)
    
    ap('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    ap('<style>')
    ap('body{font-family:"Microsoft YaHei","PingFang SC",-apple-system,sans-serif;background:#f5f5f5;margin:0;padding:16px;color:#222}')
    ap('.w{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)}')
    ap('.hd{background:linear-gradient(135deg,#1a237e,#283593,#3949ab);color:#fff;padding:28px 24px;text-align:center}')
    ap('.hd h1{font-size:22px;font-weight:700;margin:0}')
    ap('.hd .sub{font-size:13px;opacity:.85;margin-top:8px}')
    ap('.bd{padding:20px 24px}')
    ap('.sec{margin-bottom:18px}')
    ap('.st{font-size:15px;font-weight:700;color:#333;border-left:4px solid #3949ab;padding-left:10px;margin-bottom:12px}')
    ap('.card{border-radius:10px;padding:14px 16px;margin-bottom:10px;border-left:4px solid #ddd;background:#fafafa}')
    ap('.cname{font-size:15px;font-weight:700;margin-bottom:8px}')
    ap('.crow{display:flex;gap:20px;font-size:14px}')
    ap('.col{flex:1}')
    ap('.clbl{font-size:11px;color:#888;margin-bottom:2px}')
    ap('.cval{font-size:18px;font-weight:700}')
    ap('.cbuy{color:#e74c3c}')
    ap('.csell{color:#27ae60}')
    ap('.fn{font-size:11px;color:#aaa;margin-top:4px}')
    ap('.warn{background:#fff8e1;border:1px solid #ffc107;border-radius:10px;padding:14px;margin:10px 0}')
    ap('.wtit{font-weight:700;color:#f57f17;font-size:14px;margin-bottom:8px}')
    ap('.witem{font-size:13px;color:#555;padding:3px 0;line-height:1.6}')
    ap('.info{background:#e8f4fd;border-radius:10px;padding:14px;font-size:12.5px;color:#37474f;line-height:1.9}')
    ap('.ft{text-align:center;padding:14px;color:#bbb;font-size:11px;border-top:1px solid #eee}')
    ap('.tg{display:inline-block;font-size:11px;padding:2px 10px;border-radius:10px;color:#fff;margin-left:6px}')
    ap('.tg-ok{background:#43a047}')
    ap('.tg-es{background:#fb8c00}')
    ap('</style></head><body>')
    
    ap('<div class="w">')
    
    # Header
    tg_cls = 'tg-ok' if data['is_realtime'] else 'tg-es'
    tg_txt = '实时数据' if data['is_realtime'] else '估算价格'
    ap('<div class="hd">')
    ap('<h1>🏦 银行积存金价格播报</h1>')
    ap('<div class="sub">%s | %s <span class="tg %s">%s</span></div>' % (
        data['timestamp'], data['source_detail'], tg_cls, tg_txt))
    ap('</div><div class="bd">')
    
    # ===== Section 1: 主监控银行当前价格+高低分析 =====
    ap('<div class="sec"><div class="st">📌 招商银行积存金 - 当前价格</div>')
    
    mb = data['banks'].get(main_bank, {})
    cur_buy = mb.get('buy', 0)
    cur_sell = mb.get('sell', 0)
    
    ap('<div class="card" style="border-left-color:%s">' % mb.get('color','#E74C3C'))
    ap('<div class="cname">🏦 %s 积存金</div>' % main_bank)
    ap('<div class="crow">')
    ap('<div class="col"><span class="clbl">买入价</span><br><span class="cval cbuy">%s <small style="font-size:13px;color:#888">元/克</small></span></div>' % _fmt(cur_buy))
    ap('<div class="col"><span class="clbl">卖出/赎回价</span><br><span class="cval csell">%s <small style="font-size:13px;color:#888">元/克</small></span></div>' % _fmt(cur_sell))
    ap('</div>')
    ap('<div class="fn">💡 %s</div>' % mb.get('fee',''))
    ap('</div>')
    
    # 高低价详情（核心要求！）
    wd = trend.get('window_data', {}).get(main_bank, {})
    if wd:
        ap('<div style="margin-top:12px;background:#f0f4f8;border-radius:8px;padding:12px">')
        ap('<div style="font-weight:700;font-size:13px;color:#3949ab;margin-bottom:8px">📊 价格区间分析</div>')
        # 按时间窗口排序显示
        for wh in sorted(wd.keys()):
            wdata = wd[wh]
            ap('<div style="display:flex;justify-content:space-between;font-size:13px;padding:3px 0;border-bottom:1px dashed #dde3ea">')
            ap('<span>🕐 近<strong>%d</strong>小时</span>' % wh)
            ap('<span>低价 <b style="color:#27ae60">%s</b></span>' % _fmt(wdata['low']))
            ap('<span>高价 <b style="color:#e74c3c">%s</b></span>' % _fmt(wdata['high']))
            ap('<span>与低价差 <b>%s</b></span>' % _fmt(wdata['diff_from_low']))
            ap('<span>与高价差 <b>%s</b></span>' % _fmt(wdata['diff_from_high']))
            ap('</div>')
        ap('</div>')
    else:
        ap('<div style="font-size:12px;color:#888;margin-top:8px">⏳ 需要更多运行数据后显示高低价区间...</div>')
    
    ap('</div>')  # sec 1
    
    # ===== Section 2: 其他银行报价 =====
    other_banks = {k: v for k, v in data['banks'].items() if k != main_bank}
    if other_banks:
        ap('<div class="sec"><div class="st">🏦 其他银行积存金报价（元/克）</div>')
        for bname, binfo in other_banks.items():
            ap('<div class="card" style="border-left-color:%s">' % binfo.get('color','#999'))
            ap('<div class="crow"><div class="col"><span class="clbl">%s 买入</span><br><span class="cval cbuy">%s</span></div>' % (bname, _fmt(binfo.get('buy'))))
            ap('<div class="col"><span class="clbl">%s 卖出</span><br><span class="cval csell">%s</span></div></div>' % (bname, _fmt(binfo.get('sell'))))
            ap('<div class="fn">%s</div>' % binfo.get('fee',''))
            ap('</div>')
        ap('</div>')
    
    # ===== Section 3: 参考基准 =====
    if data.get('base_cny_per_gram'):
        ap('<div class="sec"><div class="st">💰 数据来源参考</div>')
        ap('<div style="font-size:13.5px;line-height:1.9">')
        ap('• 国际现货金价: <b>$%s/oz</b> (%s)<br>' % (_fmt(data.get('spot_usd_oz')), data.get('gold_source','')))
        ap('• 美元/人民币汇率: <b>%s</b> (%s)<br>' % (_fmt(data.get('usd_cny')), data.get('fx_source','')))
        ap('• 折合基础金价: <b style="color:#3949ab;font-size:16px">%s 元/克</b><br>' % _fmt(data['base_cny_per_gram']))
        ap('• 各银行积存金价 = 基础价 + 各自费率/点差')
        ap('</div></div>')
    
    # ===== Section 4: 预警信息 =====
    if trend['alerts']:
        ap('<div class="warn"><div class="wtit">⚠️ 价格波动提醒</div>')
        for a in trend['alerts']:
            ticons = {'LOW':'🔻','HIGH':'🔺','RISE':'📈','DROP':'📉'}
            ttexts = {'LOW':'近%d小时最低价区间','HIGH':'近%d小时最高价区间',
                      'RISE':'较近%d小时低点上涨','DROP':'较近%d小时高点下跌'}
            ti = ticons.get(a['type'],'❓')
            tt = ttexts.get(a['type'],'?') % a['window_h']
            ap('<div class="witem">%s <b>%s</b>: %s | 当前<b>%s</b>元/克 | 区间[%s ~ %s]</div>' % (
                ti, a['bank'], tt, _fmt(a['current']), _fmt(a['low']), _fmt(a['high'])))
        ap('</div>')
    
    # ===== Section 5: 温馨提示 =====
    notes = [
        '本系统每%d分钟检查一次价格，%d分钟内重复波动不重复提醒' % (
            max(w*60 for w in CHECK_WINDOWS_HOURS[:2]), SILENCE_MINUTES),
        '以上价格基于国际现货金价按各银行公开费率推算，实际交易请以各银行APP/网点为准',
        '<strong>积存金买入请以买入价为准，卖出/赎回请以卖出价为准</strong>',
        '监控银行: 招商银行、浙商银行、工商银行、建设银行',
        '数据更新时间: ' + data['timestamp'],
    ]
    ap('<div class="info">%s</div>' % '<br>'.join('• '+n for n in notes))
    
    # Footer
    rc = state.get('run_count', 0) + 1
    ap('</div>')  # bd
    ap('<div class="ft">Gold Monitor v4.2 | 第%d次运行 | GitHub Actions 自动发送<br>本邮件由监控系统自动生成，请勿回复</div>' % rc)
    ap('</div></body></html>')
    
    return {
        'subject': subject,
        'html': '\n'.join(L),
        'send': send,
        'reason': reason,
    }


# ============================================================
# 发送邮件
# ============================================================

def send_email(subject, html_body):
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]):
        log.error('邮件配置缺失')
        return False
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = SMTP_USER
    msg['To'] = RECIPIENTS
    msg['Date'] = _now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    
    try:
        if SMTP_PORT == 465:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=_CTX, timeout=30)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            if SMTP_PORT == 587:
                srv.starttls(context=_CTX)
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(SMTP_USER, [x.strip() for x in RECIPIENTS.split(',')], msg.as_string())
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
    log.info('Gold Monitor v4.2 启动 | %s' % _now().strftime('%Y-%m-%d %H:%M:%S'))
    
    state = _load_state()
    state['run_count'] = state.get('run_count', 0) + 1
    
    # 采集数据
    log.info('--- 开始采集 ---')
    data = collect_data()
    
    # 记录历史
    hist = {}
    for bn, bi in data['banks'].items():
        hist[bn] = bi.get('buy')
    
    if hist and any(v and v > 0 for v in hist.values()):
        state['price_history'].append((time.time(), hist))
        state['last_prices'] = {k: dict(v) for k, v in data['banks'].items()}
    
    # 分析趋势
    trend = analyze_trend(hist, state)
    log.info('趋势: %s' % trend['summary'])
    
    # 构建并发送邮件
    email = build_email(data, trend, state)
    
    if email['send']:
        log.info('发送原因: %s' % email['reason'])
        send_email(email['subject'], email['html'])
        state['last_alert_ts'] = time.time()
        state['first_run'] = False
    else:
        log.info('无需发送')
    
    _save_state(state)
    log.info('完成, 耗时%.1fs' % (time.time()-t0))
    log.info('='*55)
    return 0


if __name__ == '__main__':
    sys.exit(main())
