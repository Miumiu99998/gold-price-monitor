#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_gold_monitor.py v5.0 - Bank Gold Price Monitor (GitHub Actions)
==========================================================
Changes from v4.6:
  - Fallback price: $3750/oz (matches ~Jun 2026 real price)
  - New data source: Yahoo Finance API (query1.finance.yahoo.com)
  - State persistence: GITHUB_OUTPUT + git commit dual path
  - Email includes full diagnostic info

Author: Gold Monitor v5.0 | 2026-06-28
"""

import os
import sys
import json
import time
import re
import ssl
import smtplib
import logging
import subprocess
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen

# ============================================================
# Configuration
# ============================================================

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.163.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
RECIPIENTS = os.environ.get("RECIPIENTS", "")

# Calibrated fallback values (2026-06-26)
FALLBACK_SPOT_USD = 3750.0
FALLBACK_USD_CNY = 7.30

BANKS = {
    "\u62db\u5546\u94f6\u884c": {"add": 5.0, "color": "#E74C3C", "fee": "\u70b9\u5dee~5\u5143/\u514b"},
    "\u6d59\u5546\u94f6\u884c": {"add": 4.0, "color": "#3498DB", "fee": "\u624b\u7eed\u8d390.4%~0.5%"},
    "\u5de5\u5546\u94f6\u884c": {"add": 0.0, "rate": 0.005, "color": "#C0392B", "fee": "\u4e70\u5165\u514d/\u8d4e\u56de0.5%"},
    "\u5efa\u8bbe\u94f6\u884c": {"add": 5.5, "color": "#27AE60", "fee": "\u70b9\u5dee~6+\u8d4e\u56de0.5%"},
}

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Constants
_CTX = ssl.create_default_context()
_CST = timezone(timedelta(hours=8))
_OZ_PER_GRAM = 31.1034768
_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")
_GH_OUTPUT = os.environ.get("GITHUB_OUTPUT", "")


def now_cst():
    return datetime.now(_CST)


def fmt_price(p):
    """Format price safely."""
    if p is None:
        return "--"
    try:
        v = round(float(p), 2)
        if v <= 0:
            return "--"
        return "%.2f" % v
    except Exception:
        return "--"


def http_get(url, timeout=15):
    """HTTP GET request. Returns (status_code, body)."""
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    req = Request(url, headers=hdrs)
    try:
        resp = urlopen(req, context=_CTX, timeout=timeout)
        return resp.status_code, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def jget(url, timeout=15):
    """GET and parse as JSON. Returns dict or None."""
    code, body = http_get(url, timeout)
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


# ============================================================
# State Management
# ============================================================

def load_state():
    """Load state from file or GitHub output dir."""
    default = {
        "run_count": 0,
        "last_spot": None,
        "last_bank_base": None,
        "last_time": None,
        "all_spots": [],
        "last_fx": None,
    }
    # Try local file first
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in default:
                if k not in data:
                    data[k] = default[k]
            return data
        except Exception:
            pass
    # Try GitHub output directory
    if _GH_OUTPUT:
        alt_path = os.path.join(_GH_OUTPUT, "monitor_state.json")
        if os.path.exists(alt_path):
            try:
                with open(alt_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k in default:
                    if k not in data:
                        data[k] = default[k]
                return data
            except Exception:
                pass
    return default


def save_state(state):
    """Save state to multiple locations for persistence."""
    state["saved_at"] = now_cst().strftime("%Y-%m-%d %H:%M:%S CST")
    # Keep only last 30 spot prices
    spots = state.get("all_spots", [])
    state["all_spots"] = spots[-30:]
    # Write to local file
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        log.info("State saved to local file")
    except Exception as e:
        log.warning("Save state failed: %s" % e)
    # Write to GitHub_OUTPUT (uploaded as artifact)
    if _GH_OUTPUT:
        try:
            os.makedirs(_GH_OUTPUT, exist_ok=True)
            alt_path = os.path.join(_GH_OUTPUT, "monitor_state.json")
            with open(alt_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            log.info("State also saved to GITHUB_OUTPUT")
        except Exception as e:
            log.warning("GITHUB_OUTPUT save failed: %s" % e)


def git_commit_push():
    """Try to commit and push state via git."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        log.info("No GITHUB_TOKEN, skipping git push")
        return False
    try:
        subprocess.run(["git", "config", "user.name", "Gold Bot"], capture_output=True, timeout=10)
        subprocess.run(["git", "config", "user.email", "bot@gold.local"], capture_output=True, timeout=10)
        subprocess.run(["git", "add", _STATE_FILE], capture_output=True, timeout=10)
        rc = load_state().get("run_count", 0)
        msg = "state: run %d @ %s" % (rc, now_cst().strftime("%H:%M"))
        r = subprocess.run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            out = r.stdout.strip()
            if "nothing to commit" not in out and "up to date" not in out.lower():
                pr = subprocess.run(
                    ["git", "push", "origin", "main"],
                    capture_output=True, text=True, timeout=60,
                )
                log.info("Git push done: %s" % pr.stdout.strip()[:100])
            else:
                log.info("Git: %s" % out[:100])
        else:
            log.warning("Git commit error: %s" % r.stderr.strip()[:100])
    except Exception as e:
        log.warning("Git push error: %s" % e)
    return True


# ============================================================
# FX Rate
# ============================================================

def get_fx_rate():
    """Get USD/CNY exchange rate."""
    # Frankfurter
    d = jget("https://api.frankfurter.app/latest?from=USD&to=CNY", 12)
    if d and isinstance(d.get("rates"), dict) and d["rates"].get("CNY"):
        rate = float(d["rates"]["CNY"])
        log.info("[FX] Frankfurter: %.4f" % rate)
        return rate
    # ER-API
    d2 = jget("https://open.er-api.com/v6/latest/USD", 12)
    if d2 and isinstance(d2.get("rates"), dict) and d2["rates"].get("CNY"):
        rate = float(d2["rates"]["CNY"])
        log.info("[FX] ER-API: %.4f" % rate)
        return rate
    # Cache or fallback
    old = load_state()
    cached = old.get("last_fx")
    if cached and cached > 5.0:
        log.info("[FX] Cache: %.4f" % cached)
        return cached
    log.warning("[FX] HARDCODED: %.2f" % FALLBACK_USD_CNY)
    return FALLBACK_USD_CNY


# ============================================================
# Gold Price Collection
# ============================================================

def collect_all_prices():
    """
    Try all gold price sources.
    Returns list of (source_name, price_usd_oz).
    """
    results = []
    diag_lines = []

    def dl(s):
        diag_lines.append(s)

    dl("=== Gold Price Sources ===")

    # Source 1: Twelve Data
    dl("[1] TwelveData...")
    d = jget(
        "https://api.twelvedata.com/time_series"
        "?symbol=XAU/USD&interval=1min&outputsize=1&apikey=demo",
        12,
    )
    if d:
        try:
            vals = d.get("values", [])
            if vals and len(vals) > 0:
                p = float(vals[0].get("close", 0) or vals[0].get("open", 0))
                if p > 500:
                    results.append(("TwelveData", p))
                    dl("  OK: $%.2f" % p)
        except Exception:
            pass
    dl("  FAIL" if not any(x[0] == "TwelveData" for x in results) else "")

    # Source 2: Yahoo Finance
    dl("[2] Yahoo Finance...")
    try:
        c, b = http_get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m",
            15,
        )
        if c == 200:
            jd = json.loads(b)
            meta = (
                jd.get("chart", {}).get("result", [{}])[0].get("meta", {})
                if jd.get("chart", {}).get("result")
                else {}
            )
            # Try regularMarketPrice first
            p = float(meta.get("regularMarketPrice", 0) or 0)
            if 1000 < p < 15000:
                results.append(("Yahoo", p))
                dl("  OK: $%.2f (regularMarketPrice)" % p)
            else:
                # Try previousClose
                p2 = float(meta.get("previousClose", 0) or 0)
                if 1000 < p2 < 15000:
                    results.append(("Yahoo-prev", p2))
                    dl("  OK: $%.2f (previousClose)" % p2)
                else:
                    dl("  No usable price. Keys: %s" % str(list(meta.keys()))[:5])
        else:
            dl("  HTTP %d" % c)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:80])

    # Source 3: metals-dev
    dl("[3] metals-dev...")
    d = jget(
        "https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz",
        10,
    )
    if d and isinstance(d.get("metals"), dict):
        g = d["metals"].get("gold", {})
        if g and g.get("price"):
            p = float(g["price"])
            if 1000 < p < 15000:
                results.append(("Metals.dev", p))
                dl("  OK: $%.2f" % p)
    dl("  FAIL" if not any(x[0] == "Metals.dev" for x in results) else "")

    # Source 4: metal-price-api
    dl("[4] metal-price-api...")
    d2 = jget(
        "https://api.metal-price-api.com/v1/latest"
        "?base=USD&currencies=XAU&api_key=demo",
        10,
    )
    if d2 and isinstance(d2.get("rates"), dict):
        xau = d2["rates"].get("XAU")
        if xau and float(xau) > 0.05:
            p = 1.0 / float(xau)
            if 1000 < p < 15000:
                results.append(("Metal-API", p))
                dl("  OK: $%.2f" % p)
    dl("  FAIL" if not any(x[0] == "Metal-API" for x in results) else "")

    # Source 5: Kitco HTML scrape
    dl("[5] Kitco...")
    code, body = http_get("https://www.kitco.com/charts.live.html", 15)
    if code == 200:
        patterns = [
            ("Kitco-Bid", r"Bid\s*:\s*\$?([\d,]+\.?\d*)"),
            ("Kitco-$4k", r"\$([4-9]\d{3}\.\d{2})"),
        ]
        for pname, pat in patterns:
            m = re.search(pat, body, re.I | re.S)
            if m:
                raw = m.group(1).replace(",", "")
                try:
                    p = float(raw)
                    if 1500 < p < 15000:
                        results.append((pname, p))
                        dl("  OK: $%.2f (%s)" % (p, pname))
                        break
                except Exception:
                    pass
    else:
        dl("  HTTP %d" % code)

    # Source 6: metals-dev CNY/g (reverse-calculate)
    dl("[6] metals-dev CNY/g...")
    d3 = jget(
        "https://api.metals.dev/v1/latest?api_key=demo&currency=CNY&unit=gram",
        10,
    )
    if d3 and isinstance(d3.get("metals"), dict):
        g = d3["metals"].get("gold", {})
        if g and g.get("price"):
            pg = float(g["price"])
            if pg > 200:
                fx = get_fx_rate()
                if fx:
                    pu = (pg * _OZ_PER_GRAM) / fx
                    if 1000 < pu < 15000:
                        label = "Metals-CNYg(%.0f)" % pg
                        results.append((label, pu))
                        dl("  OK: %.2f CNY/g -> $%.2f/oz" % (pg, pu))

    dl("")
    dl("Total sources found: %d" % len(results))
    for name, price in results:
        dl("  %s: $%.2f" % (name, price))

    return results, diag_lines


def select_best_price(prices_found, diag_lines):
    """Select best gold price from available sources."""
    # Use local log function within this scope
    def dl(s):
        diag_lines.append(s)
    
    spot = None
    src = ""

    if len(prices_found) >= 2:
        # Use median to avoid outliers
        sorted_p = sorted([p for _, p in prices_found])
        spot = sorted_p[len(sorted_p) // 2]
        src = "median(%d)" % len(prices_found)
        dl("")
        dl("Using MEDIAN: $%.2f from %d sources" % (spot, len(prices_found)))
    elif len(prices_found) == 1:
        spot = prices_found[0][1]
        src = prices_found[0][0]
        dl("Using SOLE source: %s = $%.2f" % (src, spot))
    else:
        # All failed - try cache then fallback
        old = load_state()
        cached_spot = old.get("last_spot")
        all_old = old.get("all_spots", [])

        if cached_spot and cached_spot > 1000:
            spot = cached_spot
            src = "cache($%.2f)" % cached_spot
            dl("Using CACHE: $%.2f" % spot)
        elif all_old and len(all_old) >= 2:
            avg = sum(all_old[-5:]) / min(len(all_old), 5)
            if avg > 1000:
                spot = avg
                src = "avg(%d)" % len(all_old)
                dl("Using AVG: $%.2f" % spot)
            else:
                spot = FALLBACK_SPOT_USD
                src = "HARDCODED($%d)" % int(FALLBACK_SPOT_USD)
                dl("Using HARDCODED: $%.2f" % spot)
        else:
            spot = FALLBACK_SPOT_USD
            src = "HARDCODED($%d)" % int(FALLBACK_SPOT_USD)
            dl("Using HARDCODED: $%.2f" % spot)

    if not spot or spot <= 0:
        spot = FALLBACK_SPOT_USD

    return round(spot, 2), src


def collect_data():
    """Main collection function. Returns full data dict."""
    result = {
        "timestamp": now_cst().strftime("%Y-%m-%d %H:%M:%S CST"),
        "spot_usd": None,
        "usd_cny": None,
        "spot_cny_g": None,
        "bank_base": None,
        "banks": {},
        "source": "",
        "is_realtime": False,
        "attempts": [],
        "diag": "",
    }

    dl_internal = []
    def dl(s):
        dl_internal.append(s)

    dl("=== v5.0 Data Collection === %s" % result["timestamp"])

    # Collect prices
    prices, ext_diag = collect_all_prices()
    result["attempts"] = [(n, round(p, 2)) for n, p in prices]

    # Select best
    spot, src = select_best_price(prices, dl_internal)
    result["spot_usd"] = spot
    result["source"] = src
    result["is_realtime"] = (
        "cache" not in src
        and "fallback" not in src.lower()
        and "hardcoded" not in src.lower()
        and "avg(" not in src
        and "history" not in src
    )

    # FX rate
    fx = get_fx_rate()
    result["usd_cny"] = fx

    # Calculate bank prices
    spot_cny_g = (spot * fx) / _OZ_PER_GRAM
    bank_base = spot_cny_g  # Bank accumulation gold ~= spot CNY/g

    result["spot_cny_g"] = round(spot_cny_g, 2)
    result["bank_base"] = round(bank_base, 2)

    dl("")
    dl("=== Calculation ===")
    dl("Spot: $%.2f (%s)" % (spot, src))
    dl("FX: %.4f" % fx)
    dl("Spot CNY/g: %s" % fmt_price(spot_cny_g))
    dl("Bank base: %s" % fmt_price(bank_base))

    # Per-bank prices
    for bname, bcfg in BANKS.items():
        buy = round(bank_base + bcfg["add"], 2)
        if "rate" in bcfg:
            sell = round(bank_base * (1 - bcfg["rate"]), 2)
        else:
            sell = round(max(buy - 3, 1), 2)
        result["banks"][bname] = {
            "buy": buy,
            "sell": sell,
            "fee": bcfg["fee"],
            "color": bcfg["color"],
        }
        dl("%s: buy=%s sell=%s" % (bname, fmt_price(buy), fmt_price(sell)))

    # Combine diagnostics
    all_diag = ext_diag + dl_internal
    result["diag"] = chr(10).join(all_diag)

    return result


# ============================================================
# Email
# ============================================================

def send_email(data):
    """Send Chinese HTML email with price info and diagnostics."""
    if not all([SMTP_USER, SMTP_PASS, RECIPIENTS]):
        log.error("Missing email config")
        return False

    mb = data["banks"].get("\u62db\u5546\u94f6\u884c", {})
    main_buy = mb.get("buy", 0)

    subject = "\U0001f4ca\u62db\u5546\u94f6\u884c\u79ef\u5b58\u91d1\u63d0%s\u5143/\u514b - %s" % (
        fmt_price(main_buy),
        data["timestamp"],
    )

    # Build HTML
    html_parts = []

    def hp(s):
        html_parts.append(s)

    hp("<!DOCTYPE html><html><head><meta charset=utf-8><style>")
    hp("body{font-family:'Microsoft YaHei',sans-serif;background:#f5f5f5;padding:16px;color:#222}")
    hp(".w{max-width:600px;margin:0 auto;background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.1)}")
    hp(".hd{background:linear-gradient(135deg,#1a237e,#3949ab);color:#fff;padding:24px;text-align:center}")
    hp(".hd h1{margin:0;font-size:20px}.hd .sub{font-size:12px;opacity:.8;margin-top:6px}")
    hp(".bd{padding:18px 24px}")
    hp(".card{border-radius:8px;padding:14px;margin:10px 0;border-left:4px solid #3949ab;background:#f8f9fa}")
    hp(".pname{font-size:15px;font-weight:700;margin-bottom:8px}")
    hp(".prow{display:flex;gap:16px;font-size:14px}")
    hp(".col{flex:1}")
    hp(".lbl{font-size:11px;color:#888;margin-bottom:2px}")
    pval_style = "font-size:20px;font-weight:700"
    hp(".buy{color:#e74c3c}.sell{color:#27ae60}")
    hp(".fee{font-size:11px;color:#aaa;margin-top:4px}")
    hp(".diag{background:#fff3cd;border-radius:8px;padding:12px;font-size:11px;color:#555;line-height:1.6;white-space:pre-wrap;word-break:break-all}")
    hp(".ft{text-align:center;padding:12px;color:#bbb;font-size:10px;border-top:1px solid #eee}")
    hp("</style></head><body><div class=w>")

    hp("<div class=hd><h1>\U0001f3e0 \u94f6\u884c\u79ef\u5b58\u4ef7\u683c</h1>")
    rt = "\u2705\u5b9e\u65f6" if data["is_realtime"] else "\u26a0\ufe0f \u4f30\u7b97"
    hp("<div class=sub>%s | %s | $%.2f/oz (%s) x %.4f</div>" % (
        data["timestamp"], rt,
        data.get("spot_usd", 0), data["source"], data.get("usd_cny", 0),
    ))
    hp("</div><div class=bd>")

    # Main bank card
    cur_buy = mb.get("buy", 0)
    cur_sell = mb.get("sell", 0)
    hp("<div class=card style=border-left-color:%s>" % mb.get("color", "#E74C3C"))
    hp("<div class=pname>\U0001f3e0 %s \u79ef\u5b58</div>" % "\u62db\u5546\u94f6\u884c")
    hp("<div class=prow><div class=col><span class=lbl>\u4e70\u5165\u4ef7</span><br><span style='%s'>%s</span> <small style=color:#888>\u5143/\u514b</small></div>" % (pval_style, fmt_price(cur_buy)))
    hp("<div class=col><span class=lbl>\u5356\u51fa/\u8d4e\u56de</span><br><span class=sell>%s</span> <small style=color:#888>\u5143/\u514b</small></div>" % fmt_price(cur_sell))
    hp("<div class=fee>%s</div>" % mb.get("fee", ""))
    hp("</div>")

    # Other banks
    other = {k: v for k, v in data["banks"].items() if k != "\u62db\u5546\u94f6\u884c"}
    if other:
        hp("<div style='margin-top:16px;font-size:13px;color:#555;border-top:1px dashed #ddd;padding-top:8px'>")
        hp("\u5176\u4ed6\u94f6\u884c\u79ef\u5b58\u62a5\u683c (\u5143/\u514b)</div>")
        for bn, bi in other.items():
            hp("<div class=card style=border-left-color:%s>" % bi.get("color", "#999"))
            hp("<div class=prow><div class=col><span class=lbl>%s \u4e70\u5165</span><br><span style='%s'>%s</span></div>" % (bn, pval_style, fmt_price(bi.get("buy"))))
            hp("<div class=col><span class=lbl>%s \u5356\u51fa</span><br><span class=sell>%s</span></div>" % (bn, fmt_price(bi.get("sell"))))
            hp("<div class=fee>%s</div>" % bi.get("fee", ""))
            hp("</div>")
        hp("</div>")

    # Diagnostics
    hp("<div class=diag><b>\U0001f4a0 \u8bca\u65ad\u4fe1\u606f\u4fe1\u606f</b>")
    hp(data.get("diag", "(no diagnostic data)"))
    hp("</div>")

    # Footer
    rc = load_state().get("run_count", 0) + 1
    hp("</div><div class=ft>Gold Monitor v5.0 | Run #%d | GitHub Actions<br>\u81ea\u52a8\u53d1\u751f\u53d1\uff0c\u8bf7\u56de\u590d\u56de</div></div></body></html>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENTS
    msg["Date"] = now_cst().strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg.attach(MIMEText("\n".join(html_parts), "html", "utf-8"))

    try:
        if SMTP_PORT == 466:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=_CTX, timeout=30)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(
            SMTP_USER, [x.strip() for x in RECIPIENTS.split(",")], msg.as_string(),
        )
        srv.quit()
        log.info("Email sent successfully")
        return True
    except Exception as e:
        log.error("Send failed: %s" % e)
        return False


# ============================================================
# Main
# ============================================================

def main():
    t0 = time.time()
    log.info("=" * 50)
    log.info("Gold Monitor v5.0 START %s" % now_cst().strftime("%Y-%m-%d %H:%M:%S"))

    # Load state
    state = load_state()
    rc = state.get("run_count", 0) + 1
    state["run_count"] = rc

    if state.get("last_time"):
        log.info(
            "Last run: %s (spot=$%s bank_base=%s)"
            % (state["last_time"], fmt_price(state.get("last_spot")), fmt_price(state.get("last_bank_base"))),
        )

    # Collect data
    log.info("--- Collecting data ---")
    data = collect_data()

    # Update state with new values
    state["last_spot"] = data.get("spot_usd")
    state["last_bank_base"] = data.get("bank_base")
    state["last_time"] = data["timestamp"]
    state["last_source"] = data.get("source")
    state["last_fx"] = data.get("usd_cny")
    state.setdefault("all_spots", []).append(data.get("spot_usd", 0))

    # Send email
    ok = send_email(data)
    if ok:
        state["last_alert"] = data["timestamp"]

    # Persist state
    save_state(state)
    git_commit_push()

    # Summary
    elapsed = time.time() - t0
    summary = (
        "Run #%d | Spot:$%.2f(%s) FX:%.4g | Base:%.4g | Buy:%.4g | Email:%s | %.1fs"
        % (
            rc,
            data.get("spot_usd", 0),
            data.get("source", "?"),
            data.get("usd_cny", 0),
            data.get("bank_base", 0),
            (data["banks"].get("\u62db\u5546\u94f6\u884c", {}) or {}).get("buy", 0),
            "OK" if ok else "FAIL",
            elapsed,
        )
    )
    log.info(summary)
    print("")
    print("=" * 50)
    print(summary)
    print("=" * 50)

    return 0


if __name__ == "__main__":
    sys.exit(main())
