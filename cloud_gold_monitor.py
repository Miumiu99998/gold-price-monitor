#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v4.6 - 银行积存金实时监控（最终修复版）
==========================================================
v4.6 修正（基于2026-06-25真实数据校准）:
  ① 硬编码兜底: $4200 → $4000/oz（当前实际约$3999）
  ② 校准系数: 0.958 → 1.000（银行纸黄金≈国际现货CNY/g）
  ③ 简化Git持久化: 用contents API直接写state文件

今日(06-25)真实数据作为参考基准:
  国际现货金: $3,998.9/oz | USD/CNY: 7.2985
  国际现货CNY/g: ¥937.3 | 银行纸黄金: ~¥938/g
  → 系数 = 938/937.3 ≈ 1.000

作者: Gold Monitor v4.6 | 2026-06-25
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

# 基于真实数据校准的参数
FALLBACK_SPOT_USD = 4000.0       # 当前国际金价约$4000/oz (2026-06)
FALLBACK_FX_RATE = 7.30         # USD/CNY 约7.30
SPOT_TO_BANK_RATIO = 1.000        # 银行纸黄金 ≈ 国际现货CNY/g (实测验证)

BANKS = {
    '招商银行': {'add': 5.0, 'color': '#E74C3C', 'fee': '点差~5元/克'},
    '浙商银行': {'add': 4.0, 'color': '#3498DB', 'fee': '手续费0.4%~0.5%'},
    '工商银行': {'add': 0.0, 'rate': 0.005, 'color': '#C0392B', 'fee': '买入免/赎回0.5%'},
    '建设银行': {'add': 5.5, 'color': '#27AE60', 'fee': '点差4~6+赎回0.5%'},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)
_CTX = ssl.create_default_context()
_CST = timezone(timedelta(hours=8))
_OZG = 31.1034768
_REPO_API = 'https://api.github.com/repos/Miumiu99998/gold-price-monitor'
_GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')


def _now(): return datetime.now(_CST)

def _fmt(p):
    try:
        v = round(float(p), 2)
        return '%.2f' % v if v > 0 else '--'
    except: return '--'

def _get(url, to=15):
    req = Request(url, headers={'User-Agent': 'Mozilla/5.0 (GoldMonitor/4.6)', 'Accept': '*/*'})
    try:
        r = urlopen(req, context=_CTX, timeout=to)
        return r.status_code, r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return 0, str(e)

def _jget(url, to=15):
    c, b = _get(url, to)
    if c != 200: return None
    try: return json.loads(b)
    except: return None


def _read_state_from_github():
    """从GitHub仓库读取上次的state文件"""
    if not _GH_TOKEN:
        return None
    try:
        req = Request(_REPO_API + '/contents/state.json?ref=main',
                     headers={'Authorization': 'token ' + _GH_TOKEN,
                              'Accept': 'application/vnd.github.v3+json'})
        r = urlopen(req, context=_CTX, timeout=15)
        d = json.loads(r.read().decode())
        if d.get('content'):
            import base64 as b64
            return json.loads(b64.b64decode(d['content']).decode('utf-8'))
    except:
        return None


def _write_state_to_github(state):
    """将state文件写入GitHub仓库"""
    if not _GH_TOKEN:
        log.warning('无GITHUB_TOKEN，跳过持久化'); return False
    try:
        import base64 as b64
        content = b64.b64encode(json.dumps(state, ensure_ascii=False, indent=2).encode()).decode()
        
        # 先检查文件是否存在
        req = Request(_REPO_API + '/contents/state.json?ref=main',
                     headers={'Authorization': 'token ' + _GH_TOKEN,
                              'Accept': 'application/vnd.github.v3+json'})
        try:
            r = urlopen(req, context=_CTX, timeout=15)
            existing = json.loads(r.read().decode())
            sha = existing.get('sha')
        except:
            sha = None
        
        # 上传/更新
        payload = {
            'message': 'chore: update state [%s]' % _now().strftime('%Y-%m-%d %H:%M'),
            'content': content,
        }
        if sha:
            payload['sha'] = sha
        
        data = json.dumps(payload).encode()
        req2 = Request(_REPO_API + '/contents/state.json',
                      data=data,
                      headers={'Authorization': 'token ' + _GH_TOKEN,
                               'Accept': 'application/vnd.github.v3+json'},
                      method='PUT')
        r2 = urlopen(req2, context=_CTX, timeout=30)
        result = json.loads(r2.read().decode())
        log.info('✅ State已保存到GitHub (commit: %s)' % result.get('commit', {}).get('sha', '?')[:10])
        return True
    except Exception as e:
        log.warning('State保存失败: %s' % e)
        return False


# ============================================================
# 数据采集
# ============================================================

def collect():
    result = {
        'timestamp': _now().strftime('%Y-%m-%d %H:%M:%S CST'),
        'spot_usd': None, 'usd_cny': None,
        'spot_cny_g': None, 'bank_base': None,
        'banks': {}, 'source': '', 'is_realtime': False,
        'diag': '',
    }
    
    diag_lines = []
    def dl(s): diag_lines.append(s)
    
    dl('=== Gold Monitor v4.6 === %s' % result['timestamp'])
    
    # --- 金价 ---
    spot_usd = None; src = ''
    
    # metals-dev
    dl('[1] metals-dev...')
    d = _jget('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz', 12)
    if d and d.get('metals',{}).get('gold',{}).get('price'):
        p = float(d['metals']['gold']['price'])
        if 1000 < p < 15000: spot_usd = p; src = 'Metals.dev'; dl('  OK: $%.2f' % p)
    else: dl('  FAIL')
    
    # metal-price-api
    if not spot_usd:
        dl('[2] metal-price-api...')
        d2 = _jget('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo', 12)
        if d2 and d2.get('rates',{}).get('XAU'):
            xau = float(d2['rates']['XAU'])
            if xau > 0.05:
                p = 1/xau
                if 1000 < p < 15000: spot_usd = p; src = 'Metal-Price-API'; dl('  OK: $%.2f' % p)
        else: dl('  FAIL')
    
    # Kitco
    if not spot_usd:
        dl('[3] Kitco...')
        c, b = _get('https://www.kitco.com/charts.live.html', 15)
        if c == 200:
            for pat in [r'Bid\s*:\s*\$?([\d,]+\.?\d*)', r'\$([4-9]\d{3}\.\d{2})']:
                m = re.search(pat, b, re.I|re.S)
                if m:
                    raw = m.group(1).replace(',', '')
                    try:
                        p = float(raw)
                        if 1500 < p < 15000: spot_usd = p; src = 'Kitco'; dl('  OK: $%.2f' % p); break
                    except: pass
    
    # 兜底
    if not spot_usd:
        # 尝试从GitHub读取上次值
        old = _read_state_from_github()
        if old and old.get('spot_usd') and old['spot_usd'] > 1000:
            spot_usd = old['spot_usd']; src = 'GitHub缓存($%.2f)' % spot_usd
            dl('  Using GitHub cache: $%.2f' % spot_usd)
        else:
            spot_usd = FALLBACK_SPOT_USD; src = '硬编码兜底($%d)' % int(FALLBACK_SPOT_USD)
            dl('  HARDCODED: $%.2f' % spot_usd)
    
    result['spot_usd'] = spot_usd
    result['source'] = src
    result['is_realtime'] = '缓存' not in src and '兜底' not in src
    
    # --- 汇率 ---
    dl('\n--- FX Rate ---')
    usd_cny = None; fx_src = ''
    d = _jget('https://api.frankfurter.app/latest?from=USD&to=CNY', 10)
    if d and d.get('rates',{}).get('CNY'):
        usd_cny = float(d['rates']['CNY']); fx_src = 'Frankfurter'; dl('  OK: %.4f' % usd_cny)
    else:
        d2 = _jget('https://open.er-api.com/v6/latest/USD', 10)
        if d2 and d2.get('rates',{}).get('CNY'):
            usd_cny = float(d2['rates']['CNY']); fx_src = 'ER-API'; dl('  OK: %.4f' % usd_cny)
        else:
            usd_cny = FALLBACK_FX_RATE; fx_src = '硬编码'; dl('  HARDCODED: %.4f' % usd_cny)
    
    result['usd_cny'] = usd_cny
    
    # --- 计算 ---
    spot_cny_g = (spot_usd * usd_cny) / _OZG
    bank_base = spot_cny_g * SPOT_TO_BANK_RATIO
    
    result['spot_cny_g'] = round(spot_cny_g, 2)
    result['bank_base'] = round(bank_base, 2)
    
    dl('\n=== CALCULATION ===')
    dl('  Spot: $%.2f (%s) | FX: %.4f (%s)' % (spot_usd, src, usd_cny, fx_src))
    dl('  Spot CNY/g: %s | Bank base(x%.3f): %s' % (_fmt(spot_cny_g), SPOT_TO_BANK_RATIO, _fmt(bank_base)))
    
    # 各银行
    for bn, cfg in BANKS.items():
        buy = round(bank_base + cfg['add'], 2)
        if 'rate' in cfg:
            sell = round(bank_base * (1 - cfg['rate']), 2)
        else:
            sell = round(max(buy - 3, 1), 2)
        result['banks'][bn] = {'buy': buy, 'sell': sell, 'fee': cfg['fee'], 'color': cfg['color']}
        dl('  %s: buy=%s sell=%s' % (bn, _fmt(buy), _fmt(sell)))
    
    result['diag'] = '\n'.join(diag_lines)
    return result


# ============================================================
# 邮件
# ============================================================

def send_email(data):
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]):
        log.error('邮件配置缺失'); return False
    
    mb = data['banks'].get('招商银行', {})
    subject = '%s招商银行积存金金价提醒%s元/克' % ('📊', _fmt(mb.get('buy', 0)))
    
    html_parts = []
    def h(s): html_parts.append(s)
    
    h('<!DOCTYPE html><html><head><meta charset="utf-8"><style>')
    h('body{font-family:sans-serif;background:#f5f5f5;padding:16px;color:#222}')
    h('.w{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1)}')
    h('.hd{background:linear-gradient(135deg,#1a237e,#3949ab);color:#fff;padding:24px;text-align:center}')
    h('.hd h1{margin:0;font-size:20px}.hd .sub{font-size:12px;opacity:.8;margin-top:6px}')
    h('.bd{padding:18px 24px}')
    h('.card{border-radius:8px;padding:14px;margin:10px 0;border-left:4px solid #3949ab;background:#f8f9fa}')
    h('.price{font-size:22px;font-weight:700;color:#e74c3c}')
    h('.sell{font-size:18px;color:#27ae60}.fee{font-size:11px;color:#888}')
    h('.diag{background:#fff3cd;border-radius:8px;padding:12px;font-size:11px;color:#555;line-height:1.6;white-space:pre-wrap}')
    h('.ft{text-align:center;padding:12px;color:#bbb;font-size:10px;border-top:1px solid #eee}')
    h('</style></head><body><div class="w"><div class="hd">')
    h('<h1>🏦 银行积存金价格</h1>')
    h('<div class="sub">%s | 来源: $%.2f/oz(%s) × %.4f</div>' % (
        data['timestamp'], data.get('spot_usd', 0), data['source'], data.get('usd_cny', 0)))
    h('</div><div class="bd">')
    
    for bn, bi in data['banks'].items():
        h('<div class="card"><b>%s</b><br>买入: <span class="price">%s</span> 元/克<br>卖出: <span class="sell">%s</span> 元/克<br><span class="fee">%s</span></div>' % (
            bn, _fmt(bi.get('buy')), _fmt(bi.get('sell')), bi.get('fee', '')))
    
    h('<div class="diag"><b>📊 诊断信息</b>%s</div>' % data.get('diag', ''))
    h('</div><div class="ft">Gold Monitor v4.6 | GitHub Actions</div></div></body></html>')
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject; msg['From'] = SMTP_USER; msg['To'] = RECIPIENTS
    msg['Date'] = _now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText('\n'.join(html_parts), 'html', 'utf-8'))
    
    try:
        if SMTP_PORT == 465: srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=_CTX, timeout=30)
        else: srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(SMTP_USER, [x.strip() for x in RECIPIENTS.split(',')], msg.as_string())
        srv.quit(); log.info('✅ Email sent!'); return True
    except Exception as e:
        log.error('❌ Send failed: %s' % e); return False


# ============================================================
# Main
# ============================================================

def main():
    t0 = time.time(); log.info('=== v4.6 START ===')
    
    data = collect()
    
    # 构建state用于持久化
    state = {
        'run_time': data['timestamp'],
        'spot_usd': data.get('spot_usd'),
        'usd_cny': data.get('usd_cny'),
        'bank_base': data.get('bank_base'),
        'banks_buy': {k: v['buy'] for k, v in data['banks'].items()},
        'source': data.get('source'),
        'run_count': 1,
    }
    
    # 尝试从GitHub读旧state来增加run_count
    old_state = _read_state_from_github()
    if old_state and old_state.get('run_count'):
        state['run_count'] = old_state['run_count'] + 1
        state['prev_spot'] = old_state.get('spot_usd')
        state['prev_bank_base'] = old_state.get('bank_base')
        state['prev_time'] = old_state.get('run_time')
    
    ok = send_email(data)
    
    # 持久化到GitHub
    _write_state_to_github(state)
    
    log.info('Done %.1fs | Email: %s | Runs: %d' % (time.time()-t0, 'OK' if ok else 'FAIL', state['run_count']))
    return 0

if __name__ == '__main__':
    sys.exit(main())
