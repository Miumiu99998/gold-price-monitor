#!/usr/bin/env python3
"""v4.5 - 最简版诊断：获取数据+发邮件，所有原始值写入邮件正文"""
import os,sys,json,time,re,ssl,smtplib,logging,subprocess
from datetime import datetime,timezone,timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request,urlopen

# Config
SMTP_HOST=os.environ.get('SMTP_HOST','smtp.163.com')
SMTP_PORT=int(os.environ.get('SMTP_PORT','465'))
SMTP_USER=os.environ.get('SMTP_USER','')
SMTP_PASS=os.environ.get('SMTP_PASS','')
RECIPIENTS=os.environ.get('RECIPIENTS','')

CTX=ssl.create_default_context()
H_UA={'User-Agent':'Mozilla/5.0 (compatible; GoldMonitor/4.5)'}
CST=timezone(timedelta(hours=8))
OZG=31.1034768
STATE_F=os.path.join(os.path.dirname(os.path.abspath(__file__)),'state.json')
DIAG_F=os.path.join(os.path.dirname(os.path.abspath(__file__)),'diag.json')

def now(): return datetime.now(CST)
def fmt(p):
    try:
        v=round(float(p),2)
        return '%.2f'%v if v>0 else '--'
    except: return '--'

def get(url,to=15):
    req=Request(url,headers={**H_UA,'Accept':'*/*'})
    try:
        r=urlopen(req,context=CTX,timeout=to)
        return r.status_code,r.read().decode('utf-8',errors='replace')
    except Exception as e:
        return 0,str(e)

def jget(url,to=15):
    c,b=get(url,to)
    if c!=200: return None
    try: return json.loads(b)
    except: return None

log=logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,format='%(asctime)s [%(levelname)s] %(message)s')

# ============================================================
# Collect ALL raw data with detailed logging
# ============================================================
def collect():
    results={'timestamp':now().strftime('%Y-%m-%d %H:%M:%S CST'),'attempts':[],'spot_usd':None,'usd_cny':None,
              'base_cny_g':None,'bank_base':None,'banks':{},'diag_detail':''}
    
    lines=[]
    def l(s): lines.append(s)
    
    l('\n===== GOLD PRICE MONITOR v4.5 =====')
    l('Time: %s'%results['timestamp'])
    
    # --- Gold Price Attempts ---
    l('\n--- Gold Price Sources ---')
    
    # 1) metals-dev
    l('[1] metals-dev...')
    c,b=get('https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz',12)
    l('   HTTP %d | %d bytes'%(c,len(b)))
    if c==200:
        try:
            d=json.loads(b); p=d.get('metals',{}).get('gold',{}).get('price')
            if p: results['attempts'].append(('Metals.dev',float(p))); l('   PRICE: $%.2f'%float(p))
            else: l('   No gold key in response'); l('   BODY: %s'%b[:200])
        except: l('   Parse err: %s'%b[:150])
    else: l('   FAIL: %s'%b[:100])
    
    # 2) metal-price-api  
    l('[2] metal-price-api...')
    c,b=get('https://api.metal-price-api.com/v1/latest?base=USD&currencies=XAU&api_key=demo',12)
    if c==200:
        try:
            d=json.loads(b); xau=d.get('rates',{}).get('XAU')
            if xau and float(xau)>0.05:
                p=1/float(xau)
                if 1000<p<15000: results['attempts'].append(('Metal-Price-API',p)); l('   PRICE: $%.2f'%p)
        except: pass
        else: l('   Body: %s'%b[:150])
    else: l('   FAIL')
    
    # 3) Kitco
    l('[3] Kitco...')
    c,b=get('https://www.kitco.com/charts.live.html',15)
    l('   HTTP %d | %d bytes'%(c,len(b)))
    if c==200:
        for pn,pat in [('Bid',r'Bid\s*:\s*\$?([\d,]+\.?\d*)'),('bid-json',r'"bid":\s*"?\$?([\d,]+\.?\d*)"'),('$4XXX',r'\$([4-9]\d{3}\.\d{2})')]:
            m=re.search(pat,b,re.I|re.S)
            if m:
                raw=m.group(1).replace(',','')
                try:
                    p=float(raw)
                    if 1500<p<15000: results['attempts'].append(('Kitco-'+pn,p)); l('   PRICE: $%.2f (%s)'%(p,pn)); break
                except: pass
    
    # 4) frankfurter for FX
    l('\n--- FX Rate ---')
    d=jget('https://api.frankfurter.app/latest?from=USD&to=CNY',10)
    if d and d.get('rates',{}).get('CNY'):
        results['usd_cny']=float(d['rates']['CNY']); l('   Frankfurter: %.4f'%results['usd_cny'])
    else:
        d2=jget('https://open.er-api.com/v6/latest/USD',10)
        if d2 and d2.get('rates',{}).get('CNY'):
            results['usd_cny']=float(d2['rates']['CNY']); l('   ER-API: %.4f'%results['usd_cny'])
        else:
            results['usd_cny']=7.25; l('   HARDCODED: 7.25')
    
    # Select best spot price
    if results['attempts']:
        results['spot_usd']=results['attempts'][0][1]
        results['gold_src']=results['attempts'][0][0]
        l('\n=== USING: %s = $%.2f/oz ==='%(results['gold_src'],results['spot_usd']))
    else:
        results['spot_usd']=4200.0; results['gold_src']='HARDCODED'; l('\n=== USING HARDCODED: $4200 ===')
    
    # Calculate
    if not results['spot_usd'] or results['spot_usd']<=0: results['spot_usd']=4200.0
    if not results['usd_cny'] or results['usd_cny']<=0: results['usd_cny']=7.25
    
    spot_cny=(results['spot_usd']*results['usd_cny'])/OZG
    bank_base=spot_cny*0.958
    
    results['spot_cny_g']=round(spot_cny,2)
    results['bank_base']=round(bank_base,2)
    
    l('\n=== CALCULATION ===')
    l('  Spot: $%.2f/oz | FX: %.4f | Spot CNY/g: %s | Bank base: %s'%(
        results['spot_usd'],results['usd_cny'],fmt(results['spot_cny_g']),fmt(results['bank_base'])))
    
    # Banks
    banks_cfg={
        '招商银行':{'add':5,'color':'#E74C3C','fee':'点差~5元'},
        '浙商银行':{'add':4,'color':'#3498DB','fee':'手续费0.4%~0.5%'},
        '工商银行':{'add':0,'rate':0.005,'color':'#C0392B','fee':'买入免/赎回0.5%'},
        '建设银行':{'add':5.5,'color':'#27AE60','fee':'点差4~6+赎回0.5%'},
    }
    for bn,cfg in banks_cfg.items():
        buy=round(bank_base+cfg['add'],2)
        if 'rate' in cfg:
            sell=round(bank_base*(1-cfg.get('rate',0)),2)
        else:
            sell=round(buy-3,2)
        results['banks'][bn]={'buy':buy,'sell':sell,'fee':cfg['fee'],'color':cfg['color']}
        l('  %s: buy=%s sell=%s'%(bn,fmt(buy),fmt(sell)))
    
    results['diag_detail']='\n'.join(lines)
    return results

# ============================================================
# Email
# ============================================================
def send_email(data):
    if not all([SMTP_USER,SMTP_PASS,RECIPIENTS]):
        log.error('Missing mail config'); return False
    
    mb=data['banks'].get('招商银行',{})
    subject='%s招商银行积存金金价提醒%s元/克'%(
        '🔻' if False else '📊', fmt(mb.get('buy',0)))
    
    diag=data['diag_detail']
    
    html="""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
body{font-family:sans-serif;background:#f5f5f5;padding:16px;color:#222}
.w{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1)}
.hd{background:linear-gradient(135deg,#1a237e,#3949ab);color:#fff;padding:24px;text-align:center}
.hd h1{margin:0;font-size:20px}.hd .sub{font-size:12px;opacity:.8;margin-top:6px}
.bd{padding:18px 24px}
.card{border-radius:8px;padding:14px;margin:10px 0;border-left:4px solid #3949ab;background:#f8f9fa}
.price{font-size:22px;font-weight:700;color:#e74c3c}
.sell{font-size:18px;color:#27ae60}
.fee{font-size:11px;color:#888}
.diag{background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:12px;font-size:11px;color:#555;line-height:1.6}
.ft{text-align:center;padding:12px;color:#bbb;font-size:10px;border-top:1px solid #eee}
</style></head><body><div class="w"><div class="hd">
<h1>🏦 银行积存金价格</h1><div class="sub">%s | 来源: $%.2f/oz(%s) × %.4f × 0.958</div></div><div class="bd">"""%(
        data['timestamp'],data.get('spot_usd',0),data.get('gold_src','?'),
        data.get('usd_cny',0))
    
    for bn,bi in data['banks'].items():
        html+='<div class="card"><b>%s</b><br>买入: <span class="price">%s</span> 元/克<br>卖出: <span class="sell">%s</span> 元/克<br><span class="fee">%s</span></div>'%(bn,fmt(bi['buy']),fmt(bi['sell']),bi.get('fee',''))
    
    html+='<div class="diag"><b>📊 诊断信息（调试用）</b><pre>%s</pre></div>'%diag
    html+='<div class="ft">Gold Monitor v4.5 | GitHub Actions<br>自动生成</div></div></body></html>'
    
    msg=MIMEMultipart('alternative')
    msg['Subject']=subject;msg['From']=SMTP_USER;msg['To']=RECIPIENTS
    msg['Date']=now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg.attach(MIMEText(html,'html','utf-8'))
    
    try:
        if SMTP_PORT==465: srv=smtplib.SMTP_SSL(SMTP_HOST,SMTP_PORT,context=CTX,timeout=30)
        else: srv=smtplib.SMTP(SMTP_HOST,SMTP_PORT,timeout=30)
        srv.login(SMTP_USER,SMTP_PASS)
        srv.sendmail(SMTP_USER,[x.strip() for x in RECIPIENTS.split(',')],msg.as_string())
        srv.quit(); log.info('✅ Email sent!'); return True
    except Exception as e:
        log.error('❌ Send failed: %s'%e); return False

# ============================================================
# Main
# ============================================================
def main():
    t0=time.time();log.info('=== v4.5 START ===')
    data=collect()
    log.info('Sending email...')
    ok=send_email(data)
    
    # Save state & try git push
    state={'run_count':1,'last_time':data['timestamp'],'last_spot':data.get('spot_usd'),
           'last_bank_base':data.get('bank_base'),'last_banks':{k:v['buy'] for k,v in data['banks'].items()},
           'all_attempts':[(n,round(p,2)) for n,p in data.get('attempts',[])]}
    try:
        with open(STATE_F,'w',encoding='utf-8') as f: json.dump(state,f,ensure_ascii=False,indent=2)
    except: pass
    
    if os.environ.get('GITHUB_ACTIONS')=='true':
        token=os.environ.get('GITHUB_TOKEN','')
        if token:
            try:
                subprocess.run(['git','config','user.name','Bot'],capture_output=True,timeout=5)
                subprocess.run(['git','config','user.email','bot@local'],capture_output=True,timeout=5)
                subprocess.run(['git','add','-A'],capture_output=True,timeout=10)
                cm_msg = 'v4.5: diag @ ' + data['timestamp']
                r=subprocess.run(['git','commit','-m',cm_msg,'--allow-empty'],capture_output=True,text=True,timeout=20)
                if 'nothing to commit' not in r.stdout and 'up to date' not in r.stdout.lower():
                    subprocess.run(['git','push','origin','main'],capture_output=True,text=True,timeout=60)
                    log.info('Git push OK')
                else:
                    log.info('Git: %s'%r.stdout.strip()[:100])
            except Exception as e:
                log.warning('Git err: %s'%e)
    
    log.info('Done %.1fs | Email: %s'%(time.time()-t0,'OK' if ok else 'FAIL'))
    return 0

if __name__=='__main__': sys.exit(main())
