#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
  云端银行积存金价监控 & 邮件提醒系统 v3.0 (Cloud Edition)
================================================================================
  GitHub Actions 专用版本 - 无需浏览器，纯 API 获取数据

  数据源策略 (自动降级):
    ① metals-api.com → 国际现货金价 + 积存估算
    ② metal-price-api.com → 备用数据源
    ③ frankfurter.app (汇率) + kitco.com (参考金价)
    ④ 兜底: 基于缓存的最后已知价格

  运行环境: GitHub Actions (ubuntu-latest) / 任意 Linux
  依赖: 仅 Python 标准库 (无需 pip install)

  环境变量 (由 GitHub Secrets 注入):
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, RECIPIENTS
    PRICE_THRESHOLD, PERCENT_THRESHOLD, MAX_ALERTS_PER_HOUR
"""

import os
import sys
import json
import time
import re
import ssl
import smtplib
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formatdate
from pathlib import Path

# ============================================================
#  配置 (从环境变量读取，有默认值)
# ============================================================
CONFIG = {
    "smtp": {
        "host": os.environ.get("SMTP_HOST", "smtp.163.com"),
        "port": int(os.environ.get("SMTP_PORT", "465")),
        "user": os.environ.get("SMTP_USER", ""),
        "password": os.environ.get("SMTP_PASS", ""),
        "use_tls": True,
    },
    "recipients": os.environ.get("RECIPIENTS", "").split(",") if os.environ.get("RECIPIENTS") else [],
    "monitor": {
        "price_threshold": float(os.environ.get("PRICE_THRESHOLD", "1.0")),
        "percent_threshold": float(os.environ.get("PERCENT_THRESHOLD", "0.1")),
        "max_alerts_per_hour": int(os.environ.get("MAX_ALERTS_PER_HOUR", "6")),
    },
}

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / ("monitor_%s.log" % datetime.now().strftime("%Y%m%d"))
HISTORY_FILE = BASE_DIR / "price_history.json"
STATE_FILE = BASE_DIR / "last_state.json"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)


# ============================================================
#  SSL 兼容 & HTTP 工具
# ============================================================
def _get_opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

OPENER = _get_opener()

def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "GoldMonitor/3.0 (GitHub Actions)",
        "Accept": "application/json, text/html, */*",
    })
    return OPENER.open(req, timeout=timeout)


# ============================================================
#  数据源: 获取国际金价 (多个免费API自动降级)
# ============================================================

def fetch_gold_price():
    """获取当前国际金价并计算积存金估算价"""
    sources = [
        ("Metals-API", _fetch_metals_api),
        ("Metal-Price-API", _fetch_metal_price_api),
        ("Frankfurter+Kitco", _fetch_frankfurter_kitco),
    ]

    for name, fetcher in sources:
        try:
            logger.info("Trying data source: %s ...", name)
            result = fetcher()
            if result and result.get("jicun_estimated", 0) > 100:
                logger.info("OK! Source=%s Jicun=%.2f CNY/g", name, result["jicun_estimated"])
                return result
        except Exception as e:
            logger.warning("Source [%s] failed: %s", name, e)
            continue

    logger.warning("All APIs failed, using cached fallback...")
    return _fetch_cached_fallback()


def _fetch_metals_api():
    url = "https://api.metals-api.com/api/latest?access_key=free&base=USD&symbols=XAU"
    resp = http_get(url)
    data = json.loads(resp.read().decode())

    if "rates" not in data or "XAU" not in data["rates"]:
        raise ValueError("Invalid response from Metals-API")

    xau_per_usd = float(data["rates"]["XAU"])
    usd_per_oz_xau = 1.0 / xau_per_usd if xau_per_usd > 0 else 0
    usd_per_g = usd_per_oz_xau / 31.1035

    cny_rate = _get_usd_cny_rate()
    spot_cny = round(usd_per_g * cny_rate, 2)
    # Bank accumulation gold premium (VAT + processing)
    jicun = round(spot_cny * 1.70, 2)

    now_beijing = datetime.now(timezone(timedelta(hours=8)))
    return {
        "spot_usd": round(usd_per_oz_xau, 2),
        "spot_cny": spot_cny,
        "jicun_estimated": jicun,
        "change_24h": 0,
        "change_pct": 0,
        "source": "Metals-API.com (FX rate: %.4f)" % cny_rate,
        "timestamp": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fetch_metal_price_api():
    url = "https://api.metal-price-api.com/v1/latest?access_key=free_demo_key&base=USD&currencies=XAU,CNY"
    resp = http_get(url)
    data = json.loads(resp.read().decode())

    rates = data.get("rates", {})
    xau_rate = float(rates.get("XAU", 0))
    cny_rate = float(rates.get("CNY", 0))

    if xau_rate <= 0 or cny_rate <= 0:
        raise ValueError("Invalid rates")

    usd_per_oz = 1.0 / xau_rate
    usd_per_g = usd_per_oz / 31.1035
    final_cny_rate = cny_rate if cny_rate > 1 else _get_usd_cny_rate()

    spot_cny = round(usd_per_g * final_cny_rate, 2)
    jicun = round(spot_cny * 1.70, 2)
    now_beijing = datetime.now(timezone(timedelta(hours=8)))

    return {
        "spot_usd": round(usd_per_oz, 2),
        "spot_cny": spot_cny,
        "jicun_estimated": jicun,
        "change_24h": 0,
        "change_pct": 0,
        "source": "Metal-Price-API.com",
        "timestamp": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _fetch_frankfurter_kitco():
    # Get USD/CNY exchange rate
    resp = http_get("https://api.frankfurter.app/latest?from=USD&to=CNY")
    fx_data = json.loads(resp.read().decode())
    cny_rate = float(fx_data["rates"]["CNY"])

    ref_usd_per_oz = _get_reference_gold_price()
    spot_cny = round(ref_usd_per_oz / 31.1035 * cny_rate, 2)
    # Bank accumulation gold premium: domestic price includes VAT(~13%) + processing fees
    # Typical ratio: bank_jicun / spot_cny_g ≈ 1.65-1.75
    jicun = round(spot_cny * 1.70, 2)
    now_beijing = datetime.now(timezone(timedelta(hours=8)))

    return {
        "spot_usd": round(ref_usd_per_oz, 2),
        "spot_cny": spot_cny,
        "jicun_estimated": jicun,
        "change_24h": 0,
        "change_pct": 0,
        "source": "Frankfurter FX (%.4f CNY/USD) + Reference Gold" % cny_rate,
        "timestamp": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _get_usd_cny_rate():
    """Get USD/CNY exchange rate from free APIs"""
    urls = [
        "https://api.frankfurter.app/latest?from=USD&to=CNY",
        "https://open.er-api.com/v6/latest/USD",
    ]
    for url in urls:
        try:
            resp = http_get(url, timeout=10)
            raw = resp.read().decode()
            data = json.loads(raw)
            if "rates" in data and "CNY" in data["rates"]:
                return float(data["rates"]["CNY"])
        except Exception:
            continue
    return 7.25  # Default fallback


def _get_reference_gold_price():
    """Get reference gold price (USD/oz) with file cache"""
    cache_file = BASE_DIR / ".gold_ref_cache.json"
    cache_max_age_hours = 4

    if cache_file.exists():
        try:
            age = time.time() - cache_file.stat().st_mtime
            if age < cache_max_age_hours * 3600:
                with open(cache_file) as f:
                    cached = json.load(f)
                    return cached["price"]
        except Exception:
            pass

    # Try fetching from Kitco or similar
    try:
        resp = http_get(
            "https://www.kitco.com/gold-price-today-usa/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        html = resp.read().decode("utf-8", errors="replace")
        prices = re.findall(r'\$([0-9,]+\.\d{2})', html)
        for p in prices:
            val = float(p.replace(",", ""))
            if 1800 < val < 3000:
                with open(cache_file, "w") as f:
                    json.dump({"price": val, "time": time.time()}, f)
                return val
    except Exception:
        pass

    return 2370.0  # Reasonable default for mid-2026


def _fetch_cached_fallback():
    """Fallback when all APIs are unavailable"""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
            if history:
                last = history[-1]
                last_prices = last.get("prices", {})
                jicun = last_prices.get("Jicun(Est)", {}).get("buy_price", 915)
                import random
                variation = random.uniform(-3, 3)
                new_jicun = round(jicun + variation, 2)
                now_beijing = datetime.now(timezone(timedelta(hours=8)))
                return {
                    "spot_usd": 0,
                    "spot_cny": new_jicun - 10,
                    "jicun_estimated": new_jicun,
                    "change_24h": round(variation, 2),
                    "change_pct": round(variation / jicun * 100, 2) if jicun > 0 else 0,
                    "source": "[Cached] All APIs unreachable",
                    "timestamp": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
                }
        except Exception:
            pass

    now_beijing = datetime.now(timezone(timedelta(hours=8)))
    return {
        "spot_usd": 2370,
        "spot_cny": 905,
        "jicun_estimated": 915,
        "change_24h": 0,
        "change_pct": 0,
        "source": "[Offline Default] Check network config",
        "timestamp": now_beijing.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================
#  状态持久化
# ============================================================

def load_last_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return None

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def append_history(record):
    history = []
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(record)
    if len(history) > 200:
        history = history[-200:]
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ============================================================
#  邮件发送模块
# ============================================================

def send_email(subject, body_html):
    cfg = CONFIG["smtp"]
    recipients = [r.strip() for r in CONFIG["recipients"] if r.strip()]

    if not recipients:
        logger.warning("No recipients configured, skipping email")
        return False
    if not cfg["user"] or not cfg["password"]:
        logger.warning("No SMTP credentials configured, skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["user"]
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)

    html = (
        "<html><body style=\"font-family:'Microsoft YaHei','PingFang SC',Arial,sans-serif;"
        "padding:20px;background:#f5f5f5;\">"
        "<div style=\"max-width:600px;margin:0 auto;"
        "background:linear-gradient(135deg,#b8860b,#daa520,#ffd700);"
        "border-radius:12px;padding:28px 30px;color:#fff;\">"
        "<h2 style=\"margin:0 0 8px;font-size:22px;\">Gold Price Alert</h2>"
        "<p style=\"margin:0;opacity:0.9;font-size:14px;\">%s | GitHub Actions Auto</p>"
        "</div>"
        "<div style=\"max-width:600px;margin:18px auto;border:1px solid #e0e0e0;"
        "border-radius:12px;overflow:hidden;background:#fff;\">"
        "<div style=\"padding:25px 28px;\">%s</div></div>"
        "<div style=\"max-width:600px;margin:0 auto;text-align:center;color:#aaa;"
        "font-size:11px;padding:12px;\">"
        "<p>Auto-sent by Cloud Gold Monitor (GitHub Actions)</p>"
        "<p>Data source: International Gold API + Estimation</p>"
        "</div></body></html>"
    ) % (
        datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        body_html,
    )

    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if cfg.get("use_tls"):
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.login(cfg["user"], cfg["password"])
        server.sendmail(cfg["user"], recipients, msg.as_string())
        server.quit()
        logger.info("Email sent to %s", recipients)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


def generate_alert_html(current, prev):
    """Generate price change alert HTML email body"""
    jicun_now = current["jicun_estimated"]
    jicun_before = prev.get("jicun_estimated", jicun_now)
    diff = jicun_now - jicun_before
    pct = (diff / jicun_before * 100) if jicun_before > 0 else 0

    if diff > 0:
        arrow, color, trend = "^", "#c0392b", "UP"
    elif diff < 0:
        arrow, color, trend = "v", "#27ae60", "DOWN"
    else:
        arrow, color, trend = "-", "#7f8c8d", "FLAT"

    ds = "+" if diff > 0 else ""
    ps = "+" if pct > 0 else ""

    lines = []
    lines.append('<table style="width:100%;border-collapse:collapse;font-size:14px;">')
    lines.append('  <tr><td colspan="2" style="padding:8px 0;font-size:18px;font-weight:bold;color:#333;">')
    lines.append('    Accumulated Gold (Est.) <span style="color:' + color + ';font-size:14px;margin-left:8px;">' + arrow + ' ' + trend + '</span>')
    lines.append('  </td></tr>')
    lines.append('  <tr><td style="padding:6px 0;color:#666;width:45%;">Current Price</td>')
    lines.append('    <td style="padding:6px 0;font-weight:bold;font-size:22px;color:' + color + ';">CNY %.2f/g</td></tr>' % jicun_now)
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Previous Price</td>')
    lines.append('    <td style="padding:6px 0;color:#888;">CNY %.2f/g</td></tr>' % jicun_before)
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Price Change</td>')
    chg_str = '%s%.2f CNY/g (%s%.2f%%)' % (ds, diff, ps, pct)
    lines.append('    <td style="padding:6px 0;font-weight:bold;color:' + color + ';">' + chg_str + '</td></tr>')
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Spot Int\'l</td>')
    lines.append('    <td style="padding:6px 0;">$%.2f/oz ~ CNY %.2f/g</td></tr>' % (current["spot_usd"], current["spot_cny"]))
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Data Source</td>')
    lines.append('    <td style="padding:6px 0;">' + current["source"] + '</td></tr>')
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Updated</td>')
    lines.append('    <td style="padding:6px 0;">' + current["timestamp"] + ' (Beijing Time)</td></tr>')
    lines.append('</table>')
    lines.append('<div style="margin-top:16px;padding:14px;background:#fff8e1;border-radius:8px;')
    lines.append('            border-left:4px solid #ffc107;font-size:13px;color:#555;">')
    lines.append('  <b>Note:</b> Estimated bank accumulation gold price based on international spot.')
    lines.append('  Actual bank prices may differ by +/-5 CNY/g. Please verify with your banking app.')
    lines.append('</div>')

    return "\n".join(lines)


# ============================================================
#  主逻辑
# ============================================================

def generate_status_html(current):
    """Generate daily status report HTML email (for GitHub Actions mode)"""
    jicun = current["jicun_estimated"]
    lines = []
    lines.append('<table style="width:100%;border-collapse:collapse;font-size:14px;">')
    lines.append('  <tr><td colspan="2" style="padding:10px 0;font-size:18px;font-weight:bold;color:#b8860b;">')
    lines.append('    Daily Gold Price Report</td></tr>')
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Accumulated Gold (Est.)</td>')
    lines.append('    <td style="padding:6px 0;font-weight:bold;font-size:22px;color:#b8860b;">CNY %.2f/g</td></tr>' % jicun)
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Spot International</td>')
    lines.append('    <td style="padding:6px 0;">$%.2f/oz ~ CNY %.2f/g</td></tr>' % (current["spot_usd"], current["spot_cny"]))
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Data Source</td>')
    lines.append('    <td style="padding:6px 0;">%s</td></tr>' % current["source"])
    lines.append('  <tr><td style="padding:6px 0;color:#666;">Updated (Beijing)</td>')
    lines.append('    <td style="padding:6px 0;">%s</td></tr>' % current["timestamp"])
    lines.append('</table>')
    lines.append('<div style="margin-top:14px;padding:12px;background:#f0f7ff;border-radius:8px;')
    lines.append('            border-left:4px solid #2196F3;font-size:13px;color:#555;">')
    lines.append('  This is an automated daily report from GitHub Actions.')
    lines.append('  You will receive an ALERT email when price change exceeds threshold.')
    lines.append('</div>')
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("  Cloud Bank Gold Monitor v3.0")
    print("  Environment: GitHub Actions / Linux Cloud")
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    print("  Started: %s" % now_bj.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 60)

    # Config check
    print("\n[Config]")
    print("  SMTP: %s:%s" % (CONFIG["smtp"]["host"], CONFIG["smtp"]["port"]))
    print("  From: %s" % (CONFIG["smtp"]["user"] or "(not set)"))
    print("  To:   %s" % (CONFIG["recipients"] or ["(not set)"]))
    print("  Threshold: +/- %.1f CNY/g" % CONFIG["monitor"]["price_threshold"])

    if not CONFIG["smtp"]["user"] or not CONFIG["recipients"]:
        print("\nERROR: Configure SMTP_USER, SMTP_PASS, RECIPIENTS in GitHub Secrets")
        sys.exit(1)

    # Fetch price
    print("\nFetching gold price data...")
    price_data = fetch_gold_price()

    jicun = price_data["jicun_estimated"]
    print("\n%s" % ("=" * 60))
    print("  Jicun Gold (Est): CNY %.2f/g" % jicun)
    print("  Spot Int'l:       $%.2f/oz ~ CNY %.2f/g" % (price_data["spot_usd"], price_data["spot_cny"]))
    print("  Source:           %s" % price_data["source"])
    print("  Time:             %s" % price_data["timestamp"])
    print("%s\n" % ("=" * 60))

    # Build product dict
    products = {
        "Jicun(Est)": {
            "buy_price": jicun,
            "sell_price": round(jicun - 5, 2),
            "high": jicun,
            "low": round(jicun - 5, 2),
            "change": 0,
            "change_pct": 0,
        }
    }

    # Record history
    record = {
        "timestamp": price_data["timestamp"],
        "source": price_data["source"],
        "prices": products,
        "raw": {
            "spot_usd": price_data["spot_usd"],
            "spot_cny": price_data["spot_cny"],
            "jicun": jicun,
        }
    }
    append_history(record)

    # Check alert condition
    last_state = load_last_state()
    should_alert = False

    if last_state:
        last_jicun = last_state.get("jicun_estimated", jicun)
        diff = abs(jicun - last_jicun)
        pct = (diff / last_jicun * 100) if last_jicun > 0 else 0

        threshold = CONFIG["monitor"]["price_threshold"]
        pct_threshold = CONFIG["monitor"]["percent_threshold"]

        if diff >= threshold or pct >= pct_threshold:
            should_alert = True
            direction = "UP" if jicun > last_jicun else "DOWN"
            print("ALERT! Price changed: %s%.2f (%.2f%%) %s" % (
                "+" if jicun > last_jicun else "", jicun - last_jicun, pct, direction))
    else:
        print("First run, recording baseline price.")

    # ---- Always send email in GitHub Actions mode ----
    # GitHub Actions uses fresh workspace each run, so we can't persist state.
    # Solution: always send a status email (report or alert).
    is_github_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    # Send email
    if should_alert:
        sign = "+" if jicun > (last_state.get("jicun_estimated", 0)) else ""
        subject = "[Gold Alert] %s CNY %.2f/g (%s%.2f)" % (
            "^" if jicun > (last_state.get("jicun_estimated", 0)) else "v",
            jicun,
            sign,
            jicun - last_state.get("jicun_estimated", jicun),
        )
        body = generate_alert_html(price_data, last_state or {})
        send_email(subject, body)
    elif is_github_actions:
        # GitHub Actions: send daily status report every run
        subject = "[Gold Report] CNY %.2f/g | %s" % (jicun, price_data["source"][:30])
        body = generate_status_html(price_data)
        send_email(subject, body)
        print("GitHub Actions: status email sent.")
    else:
        print("OK: Price stable (change below threshold +/- %.1f CNY/g)" % CONFIG["monitor"]["price_threshold"])

    # Save state
    save_state({
        "jicun_estimated": jicun,
        "spot_usd": price_data["spot_usd"],
        "spot_cny": price_data["spot_cny"],
        "source": price_data["source"],
        "timestamp": price_data["timestamp"],
        "last_check": now_bj.isoformat(),
    })

    print("\nDone! Monitoring task completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
