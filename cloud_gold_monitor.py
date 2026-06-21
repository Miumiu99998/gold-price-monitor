#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.3 - 银行积存金实时监控（GitHub Actions稳定版）
==========================================================
v4.3 修复:
  ① 价格不准 → 新增国际金价→银行纸黄金折算系数(0.958)
  ② 价格不变 → state文件通过Git commit持久化到仓库

数据源:
  ① metals-dev.com / metal-price-api.com → 国际现货金($/oz)
  ② Kitco.com HTML → 备用金价 (v3.0验证可用)
  ③ Frankfurter.app / er-api.com → USD/CNY汇率 (v3.0验证可用)
  ④ 硬编码兜底 → 绝不为0

价格推算链:
  国际现货金($/oz) × 汇率 ÷ 31.1035 = 国际现货CNY/g
  国际现货CNY/g × 0.958 = 银行纸黄金基准价(CNY/g)  ← 新增校准!
  基准价 + 各银行费率/点差 = 该行积存金买入/卖出价

State持久化:
  运行结束后自动commit state文件到Git仓库，下次运行checkout时恢复

作者: Gold Monitor v4.3 | 2026-06-21
"""

import os, sys, json, time, re, ssl, smtplib, logging, subprocess
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

# ===== 关键校准系数 =====
# 国际现货金(CNY/g) → 中国银行纸黄金(CNY/g) 的折算系数
# 实测依据(2026-06-12): 工行纸黄金938.71 vs 国际现货约$4200×7.25/31.1035=979.3
# 系数 = 938.71/979.3 ≈ 0.958
SPOT_TO_BANK_PAPER_GOLD_RATIO = 0.958

# 各银行费率配置（基于纸黄金基准价的加点/费率）
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
_CST = timezone(timedelta(hours=8))
_OZ_PER_GRAM = 31.1034768


def _now():
    return datetime.now(_CST)


def _fmt(p):
    """安全格式化价格"""
    if p is None:
        return '--'
    try:
        v = round(float(p), 2)
        if v <= 0:
            return '--'
        return '%.2f' % v
    except:
        return '--'


def _get(url, timeout=15):
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.3; +https://github.com/Miumiu99998/gold-price-monitor)',
        'Accept': 'text/html,application/json,*/*',
    }
    req = Request(url, headers=hdrs)
    try:
        r = urlopen(req, context=_CTX, timeout=timeout)
        return r.status_code, r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)


def _jget(url, timeout=15):
    c, b = _get(url, timeout)
    if c != 200:
        return None
    try:
        return json.loads(b)
    except:
        return None


def _load_state():
    d = {
        'price_history': [],
        'last_alert_ts': 0,
        'last_prices': {},
        'bank_paper_gold_cny_g': None,
        'spot_usd_oz': None,
        'usd_cny_rate': None,
        'first_run': True,
        'run_count': 0,
        'run_log': [],  # 记录每次运行的时间+价格，用于调试
    }
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for k in d:
                if k not in data:
                    data[k] = d[k]
            return data
        except Exception as e:
            log.warning('load_state err: %s' % e)
    return d


def _save_state(s):
    cut = time.time() - 72 * 3600  # 保留72小时历史
    s['price_history'] = [p for p in s['price_history'] if p[0] > cut]
    # 只保留最近20条运行日志
    s['run_log'] = s.get('run_log', [])[-20:]
    try:
        with open(_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning('save_state err: %s' % e)


def _persist_state_to_git():
    """
    将state文件commit到Git仓库，实现跨运行持久化。
    仅在GitHub Actions环境中执行。
    """
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        return False
    
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        log.info('无GITHUB_TOKEN，跳过Git持久化')
        return False
    
    try:
        repo_url = 'https://x-access-token:%s@github.com/Miumiu99998/gold-price-monitor.git' % token
        
        # 配置git
        subprocess.run(['git', 'config', 'user.name', 'Gold Monitor Bot'], 
                       capture_output=True, timeout=10)
        subprocess.run(['git', 'config', 'user.email', 'monitor@gold.local'],
                       capture_output=True, timeout=10)
        
        # add + commit + push
        subprocess.run(['git', 'add', _STATE_FILE], capture_output=True, timeout=10)
        
        result = subprocess.run(
            ['git', 'commit', '-m', 'chore: update price state [%s]' % _now().strftime('%Y-%m-%d %H:%M')],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode == 0:
            push_result = subprocess.run(
                ['git', 'push', 'origin', 'main'],
                capture_output=True, text=True, timeout=60
            )
            if push_result.returncode == 0:
                log.info('✅ State已持久化到Git仓库')
                return True
            else:
                log.warning('Git push失败: %s' % push_result.stderr[:200])
        else:
            # 可能是nothing to commit
            if 'nothing to commit' in result.stdout or 'up to date' in result.stdout.lower():
                log.info('State无变化，跳过commit')
                return True
            log.warning('Git commit失败: %s' % result.stdout[:200])
            
    except Exception as e:
        log.warning('Git持久化异常: %s' % e)
    
    return False


# ============================================================
# 数据采集
# ============================================================

def _fetch_spot_gold_usd_oz():
    """获取国际现货金价 USD/盎司"""
    
    # Source 1: metals-dev
    log.info('[金价] metals-dev...')
    d = _jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', 12)
    if d and isinstance(d.get('metals'), dict):
        g = d['metals'].get('gold', {})
        p = g.get('price')
        if p and float(p) > 500:
            log.info('[OK] metals-dev: $%.2f/oz' % float(p))
            return float(p), 'Metals.dev'
    
    # Source 2: metal-price-api
    log.info('[金价] metal-price-api...')
    d2 = _jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo', 12)
    if d2 and isinstance(d2.get('rates'), dict):
        xau = d2['rates'].get('XAU')
        if xau and float(xau) > 0.05:
            p = 1.0 / float(xau)
            if 1000 < p < 15000:
                log.info('[OK] metal-price-api: $%.2f/oz' % p)
                return p, 'Metal-Price-API'
    
    # Source 3: Kitco HTML (v3.0验证可用!)
    log.info('[金价] Kitco...')
    code, body = _get('https://www.kitco.com/charts.live.html', 15)
    if code == 200 and len(body) > 500:
        patterns = [
            r'Bid\s*:\s*\$?([\d,]+\.?\d*)',
            r'"bid":\s*"?\$?([\d,]+\.?\d*)"?',
            r'\$([4-9]\d{3}\.\d{2})',
        ]
        for pat in patterns:
            m = re.search(pat, body, re.I | re.S)
            if m:
                raw = m.group(1).replace(',', '')
                try:
                    p = float(raw)
                    if 1500 < p < 15000:
                        log.info('[OK] Kitco: $%.2f/oz' % p)
                        return p, 'Kitco'
                except:
                    continue
    
    # Source 4: 缓存
    log.warning('[金价] 在线源失败，尝试缓存...')
    state = _load_state()
    cached = state.get('spot_usd_oz')
    if cached and cached > 1000:
        log.info('[OK] 缓存: $%.2f/oz' % cached)
        return cached, '缓存'
    
    # Source 5: 兜底
    FALLBACK = 4200.0
    log.warning('[金价] ⚠️ 兜底 $%.2f/oz' % FALLBACK)
    return FALLBACK, '硬编码兜底'


def _fetch_usd_cny():
    """获取USD/CNY汇率"""
    
    log.info('[汇率] frankfurter...')
    d = _jget('https://api.frankfurter.app/latest?from=USD&to=CNY', 12)
    if d and isinstance(d.get('rates'), dict):
        rate = d['rates'].get('CNY')
        if rate and float(rate) > 5.0:
            log.info('[OK] frankfurter: %.4f' % float(rate))
            return float(rate), 'Frankfurter'
    
    log.info('[汇率] er-api...')
    d2 = _jget('https://open.er-api.com/v6/latest/USD', 12)
    if d2 and isinstance(d2.get('rates'), dict):
        rate = d2['rates'].get('CNY')
        if rate and float(rate) > 5.0:
            log.info('[OK] er-api: %.4f' % float(rate))
            return float(rate), 'ER-API'
    
    state = _load_state()
    cr = state.get('usd_cny_rate')
    if cr and cr > 5.0:
        log.info('[OK] 缓存: %.4f' % cr)
        return cr, '缓存'
    
    log.warning('[汇率] ⚠️ 兜底 7.25')
    return 7.25, '硬编码兜底'


def collect_data():
    """主采集函数。保证返回有效价格。"""
    
    result = {
        'source_detail': '',
        'is_realtime': False,
        'banks': {},
        'spot_usd_oz': None,
        'usd_cny': None,
        'spot_cny_per_gram': None,      # 国际现货CNY/g（未校准）
        'bank_paper_gold_cny_g': None,   # 银行纸黄金CNY/g（校准后）← 这是关键！
        'timestamp': _now().strftime('%Y-%m-%d %H:%M:%S CST'),
        'gold_source': '',
        'fx_source': '',
    }
    
    # Step 1: 国际金价
    spot_usd, gsrc = _fetch_spot_gold_usd_oz()
    result['spot_usd_oz'] = spot_usd
    result['gold_source'] = gsrc
    if not spot_usd or spot_usd <= 0:
        spot_usd = 4200.0
    
    # Step 2: 汇率
    usd_cny, fxsrc = _fetch_usd_cny()
    result['usd_cny'] = usd_cny
    result['fx_source'] = fxsrc
    if not usd_cny or usd_cny <= 0:
        usd_cny = 7.25
    
    # Step 3: 国际现货 CNY/g
    spot_cny_g = (spot_usd * usd_cny) / _OZ_PER_GRAM
    result['spot_cny_per_gram'] = round(spot_cny_g, 2)
    
    # Step 4: ★★★ 校准为银行纸黄金价格 ★★★
    bank_paper_g = spot_cny_g * SPOT_TO_BANK_PAPER_GOLD_RATIO
    result['bank_paper_gold_cny_g'] = round(bank_paper_g, 2)
    
    # 判断是否实时
    result['is_realtime'] = ('缓存' not in gsrc and '兜底' not in gsrc and 
                             '缓存' not in fxsrc and '兜底' not in fxsrc)
    
    result['source_detail'] = '国际金价%s × 汇率%s × 校准系数%.3f' % (
        gsrc, fxsrc, SPOT_TO_BANK_PAPER_GOLD_RATIO)
    
    log.info('=== 数据汇总 ===')
    log.info('  国际金价: $%.2f/oz [%s]' % (spot_usd, gsrc))
    log.info('  汇率: %.4f [%s]' % (usd_cny, fxsrc))
    log.info('  国际现货: ¥%s/g' % _fmt(spot_cny_g))
    log.info('  ★ 银行纸黄金基准: ¥%s/g (×%.3f)' % (_fmt(bank_paper_g), SPOT_TO_BANK_PAPER_GOLD_RATIO))
    
    # Step 5: 推算各银行积存金价
    for bname, bcfg in BANKS.items():
        ba = bcfg.get('buy_add', 0)
        ss = bcfg.get('sell_sub', 0)
        sr = bcfg.get('sell_rate')
        
        buy_price = round(bank_paper_g + ba, 2)
        
        if sr is not None:
            sell_price = round(bank_paper_g * (1 - sr), 2)
        else:
            sell_price = round(max(buy_price - abs(ss) if ss else bank_paper_g - 3, 1), 2)
        
        if buy_price <= 0:
            buy_price = round(bank_paper_g, 2)
        if sell_price <= 0:
            sell_price = round(bank_paper_g - 2, 2)
        
        result['banks'][bname] = {
            'buy': buy_price,
            'sell': sell_price,
            'paper_gold_base': round(bank_paper_g, 2),
            'fee': bcfg.get('fee', ''),
            'color': bcfg.get('color', '#666'),
        }
        log.info('  %s: 买入¥%s | 卖出¥%s' % (bname, _fmt(buy_price), _fmt(sell_price)))
    
    # 保存baseline
    state = _load_state()
    state['bank_paper_gold_cny_g'] = round(bank_paper_g, 2)
    state['spot_usd_oz'] = spot_usd
    state['usd_cny_rate'] = usd_cny
    
    # 记录运行日志
    run_entry = {
        'time': result['timestamp'],
        'spot_usd': round(spot_usd, 2),
        'bank_base': round(bank_paper_g, 2),
        'banks_buy': {k: v['buy'] for k, v in result['banks'].items()},
    }
    state.setdefault('run_log', []).append(run_entry)
    
    _save_state(state)
    
    return result


# ============================================================
# 高低价分析
# ============================================================

def analyze_trend(current_prices, state):
    alerts = []
    now_ts = time.time()
    history = state.get('price_history', [])
    
    window_data = {}
    
    if len(history) < 2:
        return {'alerts': [], 'summary': '正在积累数据...', 'window_data': {}}
    
    for wh in CHECK_WINDOWS_HOURS:
        cutoff = now_ts - wh * 3600
        wd = [p for p in history if p[0] > cutoff]
        if len(wd) < 2:
            continue
        
        for bank, cur in current_prices.items():
            if cur is None or cur <= 0:
                continue
            
            bp = [p[1].get(bank) for p in wd if isinstance(p[1], dict) and p[1].get(bank) and p[1][bank] > 0]
            if not bp:
                continue
            
            w_high = max(bp)
            w_low = min(bp)
            d_low = round(cur - w_low, 2)
            d_high = round(w_high - cur, 2)
            
            if bank not in window_data:
                window_data[bank] = {}
            window_data[bank][wh] = {
                'high': w_high, 'low': w_low, 'current': cur,
                'diff_from_low': d_low, 'diff_from_high': d_high,
            }
            
            atype = None
            if cur <= w_low + 1.0:
                atype = 'LOW'
            elif cur >= w_high - 1.0:
                atype = 'HIGH'
            elif d_low >= ALERT_THRESHOLD:
                atype = 'RISE'
            elif d_high >= ALERT_THRESHOLD:
                atype = 'DROP'
            
            if atype:
                alerts.append({
                    'window_h': wh, 'type': atype, 'bank': bank,
                    'current': cur, 'high': w_high, 'low': w_low,
                    'diff_low': d_low, 'diff_high': d_high,
                })
    
    summary = '检测到%d个信号: %s' % (len(alerts), ', '.join(set(a['type'] for a in alerts))) if alerts else '价格平稳'
    return {'alerts': alerts, 'summary': summary, 'window_data': window_data}


# ============================================================
# 邮件模板
# ============================================================

def build_email(data, trend, state):
    main_bank = '招商银行'
    mb = data['banks'].get(main_bank, {})
    main_buy = mb.get('buy', 0)
    
    icon = '📊'
    status_text = ''
    
    if trend['alerts']:
        a = trend['alerts'][0]
        if a['type'] in ('LOW', 'DROP'):
            icon = '🔻'; status_text = '当前为近%d小时低价' % a['window_h']
        else:
            icon = '🔺'; status_text = '当前为近%d小时高价' % a['window_h']
    
    subject = '%s%s积存金金价提醒%s元/克 - %s' % (
        icon, main_bank, _fmt(main_buy),
        status_text if status_text else _now().strftime('%m/%d %H:%M'))
    
    send = False; reason = ''
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        send = True; reason = '定时报告'
    elif trend['alerts']:
        send = True; reason = '价格波动预警'
    elif state.get('first_run'):
        send = True; reason = '首次运行'
    
    L = []
    def ap(s): L.append(s)
    
    ap('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    ap('<style>')
    ap('body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f5f5f5;margin:0;padding:16px;color:#222}')
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
    ap('.tg-ok{background:#43a047}.tg-es{background:#fb8c00}')
    ap('.wtbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}')
    ap('.wtbl th{background:#e8eaf6;padding:6px 10px;text-align:left;font-weight:600;color:#3949ab}')
    ap('.wtbl td{padding:6px 10px;border-bottom:1px dashed #ddd}')
    ap('</style></head><body><div class="w">')
    
    tg_cls = 'tg-ok' if data['is_realtime'] else 'tg-es'
    tg_txt = '实时数据' if data['is_realtime'] else '估算价格'
    ap('<div class="hd"><h1>🏦 银行积存金价格播报</h1>')
    ap('<div class="sub">%s | %s <span class="tg %s">%s</span></div>' % (
        data['timestamp'], data['source_detail'], tg_cls, tg_txt))
    ap('</div><div class="bd">')
    
    # === Section 1: 主银行当前价格 ===
    ap('<div class="sec"><div class="st">📌 %s积存金 - 当前价格</div>' % main_bank)
    cur_buy = mb.get('buy', 0); cur_sell = mb.get('sell', 0)
    ap('<div class="card" style="border-left-color:%s">' % mb.get('color','#E74C3C'))
    ap('<div class="cname">🏦 %s 积存金</div>' % main_bank)
    ap('<div class="crow"><div class="col"><span class="clbl">买入价</span><br><span class="cval cbuy">%s <small style="font-size:13px;color:#888">元/克</small></span></div>' % _fmt(cur_buy))
    ap('<div class="col"><span class="clbl">卖出/赎回价</span><br><span class="cval csell">%s <small style="font-size:13px;color:#888">元/克</small></span></div>' % _fmt(cur_sell))
    ap('</div><div class="fn">💡 %s</div>' % mb.get('fee',''))
    ap('</div>')
    
    # === 价格区间分析表格 ===
    wd = trend.get('window_data', {}).get(main_bank, {})
    if wd:
        ap('<div style="margin-top:12px;background:#f0f4f8;border-radius:8px;padding:12px">')
        ap('<div style="font-weight:700;font-size:13px;color:#3949ab;margin-bottom:8px">📊 近期价格区间分析</div>')
        ap('<table class="wtbl"><tr><th>时间窗口</th><th>最低价(元/克)</th><th>最高价(元/克)</th><th>与低价差</th><th>与高价差</th></tr>')
        for wh in sorted(wd.keys()):
            w = wd[wh]
            ap('<tr><td>近<strong>%d</strong>小时</td><td style="color:#27ae60;font-weight:700">%s</td><td style="color:#e74c3c;font-weight:700">%s</td><td>%s</td><td>%s</td></tr>' % (
                wh, _fmt(w['low']), _fmt(w['high']), _fmt(w['diff_from_low']), _fmt(w['diff_from_high'])))
        ap('</table></div>')
    else:
        ap('<div style="font-size:12px;color:#888;margin-top:8px">⏳ 运行积累中，将在下次报告显示高低价区间...</div>')
    ap('</div>')  # sec 1
    
    # === Section 2: 其他银行 ===
    ob = {k: v for k, v in data['banks'].items() if k != main_bank}
    if ob:
        ap('<div class="sec"><div class="st">🏦 其他银行积存金报价（元/克）</div>')
        for bn, bi in ob.items():
            ap('<div class="card" style="border-left-color:%s">' % bi.get('color','#999'))
            ap('<div class="crow"><div class="col"><span class="clbl">%s 买入</span><br><span class="cval cbuy">%s</span></div>' % (bn, _fmt(bi.get('buy'))))
            ap('<div class="col"><span class="clbl">%s 卖出</span><br><span class="cval csell">%s</span></div></div>' % (bn, _fmt(bi.get('sell'))))
            ap('<div class="fn">%s</div>' % bi.get('fee',''))
            ap('</div>')
        ap('</div>')
    
    # === Section 3: 数据来源 ===
    if data.get('bank_paper_gold_cny_g'):
        ap('<div class="sec"><div class="st">💰 数据来源参考</div>')
        ap('<div style="font-size:13.5px;line-height:1.9">')
        ap('• 国际现货金价: <b>$%s/oz</b> (%s)<br>' % (_fmt(data.get('spot_usd_oz')), data.get('gold_source','')))
        ap('• USD/CNY汇率: <b>%s</b> (%s)<br>' % (_fmt(data.get('usd_cny')), data.get('fx_source','')))
        ap('• 国际现货折合: <b>%s 元/克</b><br>' % _fmt(data.get('spot_cny_per_gram')))
        ap('• ★ 银行纸黄金基准: <b style="color:#3949ab;font-size:16px">%s 元/克</b> (×%.3f校准)<br>' % (
            _fmt(data['bank_paper_gold_cny_g']), SPOT_TO_BANK_PAPER_GOLD_RATIO))
        ap('• 各行积存金 = 纸黄金基准 + 各自费率/点差')
        ap('</div></div>')
    
    # === Section 4: 预警 ===
    if trend['alerts']:
        ap('<div class="warn"><div class="wtit">⚠️ 价格波动提醒</div>')
        ti_map = {'LOW':'🔻','HIGH':'🔺','RISE':'📈','DROP':'📉'}
        tt_map = {'LOW':'近%d小时最低价区间','HIGH':'近%d小时最高价区间',
                  'RISE':'较近%d小时低点上涨','DROP':'较近%d小时高点下跌'}
        for a in trend['alerts']:
            ap('<div class="witem">%s <b>%s</b>: %s | 当前<b>%s</b>元/克 | 区间[%s ~ %s]</div>' % (
                ti_map.get(a['type'],'❓'), a['bank'], tt_map.get(a['type'],'?') % a['window_h'],
                _fmt(a['current']), _fmt(a['low']), _fmt(a['high'])))
        ap('</div>')
    
    # === Section 5: 温馨提示 ===
    notes = [
        '本系统每%d分钟检查一次价格，%d分钟内重复波动不重复提醒' % (
            max(w*60 for w in CHECK_WINDOWS_HOURS[:2]), SILENCE_MINUTES),
        '以上价格基于国际现货金价经校准系数(%.3f)推算，实际请以各银行APP为准' % SPOT_TO_BANK_PAPER_GOLD_RATIO,
        '<strong>积存金买入以买入价为准，卖出/赎回以卖出价为准</strong>',
        '监控银行: 招商银行、浙商银行、工商银行、建设银行',
        '更新时间: ' + data['timestamp'],
    ]
    ap('<div class="info">%s</div>' % '<br>'.join('• '+n for n in notes))
    
    rc = state.get('run_count', 0) + 1
    run_hist = state.get('run_log', [])
    hist_info = '已运行%d次' % rc
    if len(run_hist) >= 2:
        last_run = run_hist[-2] if len(run_hist) >= 2 else run_hist[-1]
        hist_info += ' | 上次: %s 招行¥%s' % (last_run.get('time','?'), _fmt(last_run.get('banks_buy',{}).get(main_bank,'--')))
    
    ap('</div>')  # bd
    ap('<div class="ft">Gold Monitor v4.3 | %s | GitHub Actions<br>自动发送，请勿回复</div>' % hist_info)
    ap('</div></body></html>')
    
    return {'subject': subject, 'html': '\n'.join(L), 'send': send, 'reason': reason}


# ============================================================
# 发送邮件
# ============================================================

def send_email(subject, html_body):
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]):
        log.error('邮件配置缺失'); return False
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject; msg['From'] = SMTP_USER
    msg['To'] = RECIPIENTS; msg['Date'] = _now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))
    try:
        if SMTP_PORT == 465:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=_CTX, timeout=30)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            if SMTP_PORT == 587: srv.starttls(context=_CTX)
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(SMTP_USER, [x.strip() for x in RECIPIENTS.split(',')], msg.as_string())
        srv.quit(); log.info('✅ 邮件发送成功: %s' % subject); return True
    except Exception as e:
        log.error('❌ 发送失败: %s' % e); return False


# ============================================================
# Main
# ============================================================

def main():
    t0 = time.time()
    log.info('='*55)
    log.info('Gold Monitor v4.3 | %s' % _now().strftime('%Y-%m-%d %H:%M:%S'))
    
    state = _load_state()
    state['run_count'] = state.get('run_count', 0) + 1
    
    # 显示上次运行信息
    rl = state.get('run_log', [])
    if rl:
        last = rl[-1]
        log.info('上次运行: %s | 招行买入¥%s' % (last.get('time','?'), _fmt(last.get('banks_buy',{}).get('招商银行','?'))))
    
    log.info('--- 采集数据 ---')
    data = collect_data()
    
    # 记录历史
    hist = {}
    for bn, bi in data['banks'].items():
        hist[bn] = bi.get('buy')
    
    if hist and any(v and v > 0 for v in hist.values()):
        state['price_history'].append((time.time(), hist))
        state['last_prices'] = {k: dict(v) for k, v in data['banks'].items()}
    
    trend = analyze_trend(hist, state)
    log.info('趋势: %s' % trend['summary'])
    
    email = build_email(data, trend, state)
    if email['send']:
        log.info('发送原因: %s' % email['reason'])
        send_email(email['subject'], email['html'])
        state['last_alert_ts'] = time.time()
        state['first_run'] = False
    
    # ★ 关键：持久化state到Git仓库 ★
    _save_state(state)
    _persist_state_to_git()
    
    log.info('完成 %.1fs' % (time.time()-t0))
    log.info('='*55)
    return 0


if __name__ == '__main__':
    sys.exit(main())
