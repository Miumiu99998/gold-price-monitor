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

# Futures premium discount: Yahoo GC=F is COMEX futures (~2.5% above spot)
# Calibrated: actual bank ~911 vs system ~918 -> need ~-7 CNY/g adjustment
FUTURE_DISCOUNT_RATE = 0.9925   # Convert futures to spot equivalent
FUTURE_DISCOUNT_FIXED = 2.0     # Fixed CNY/g discount

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
_GH_OUT = os.environ.get("STATE_OUTPUT_DIR", "")


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
        # Safe status code extraction - works with redirects and different response types
        try:
            code = resp.status
        except AttributeError:
            try:
                code = resp.getcode()
            except AttributeError:
                code = 200  # If we got here without exception, assume OK
        body = resp.read().decode("utf-8", errors="replace")
        return code, body
    except Exception as e:
        err_str = str(e)
        log.warning("http_get ERR [%s] %s: %s" % (type(e).__name__, url[:50], err_str[:150]))
        return 0, err_str


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
    if _GH_OUT:
        alt_path = os.path.join(_GH_OUT, "monitor_state.json")
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
    # Write to GitHub_OUTPUT (uploaded as artifact) - use unique name
    if _GH_OUT:
        try:
            os.makedirs(_GH_OUT, exist_ok=True)
            alt_path = os.path.join(_GH_OUT, "monitor_state.json")
            # Remove existing file if present
            if os.path.exists(alt_path):
                os.remove(alt_path)
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
    log.info("[FX] Trying to get exchange rate...")
    # Frankfurter
    d = jget("https://api.frankfurter.app/latest?from=USD&to=CNY", 12)
    if d and isinstance(d.get("rates"), dict) and d["rates"].get("CNY"):
        rate = float(d["rates"]["CNY"])
        log.info("[FX] Frankfurter: %.4f" % rate)
        return rate
    log.warning("[FX] Frankfurter failed")
    # ER-API
    d2 = jget("https://open.er-api.com/v6/latest/USD", 12)
    if d2 and isinstance(d2.get("rates"), dict) and d2["rates"].get("CNY"):
        rate = float(d2["rates"]["CNY"])
        log.info("[FX] ER-API: %.4f" % rate)
        return rate
    log.warning("[FX] ER-API failed")
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
    log.info("[API-1] Trying TwelveData...")
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
                    log.info("[API-1] TwelveData OK: $%.2f" % p)
        except Exception:
            pass
    dl("  FAIL" if not any(x[0] == "TwelveData" for x in results) else "")
    log.info("[API-1] TwelveData: %s" % ("OK" if any(x[0]=="TwelveData" for x in results) else "FAIL"))

    # Source 1b: Stooq.com (CSV, free, no key, cloud-friendly!)
    dl("[1b] Stooq.com...")
    log.info("[API-1b] Trying Stooq.com...")
    try:
        c_stooq, b_stooq = http_get(
            "https://stooq.com/q/l/?s=gc.f&f=sd2t2ohlc&h&e=csv&d1=20260701",
            12,
        )
        if c_stooq == 200 and b_stooq:
            lines = b_stooq.strip().split("\n")
            # Skip header line, get last data row
            if len(lines) >= 2:
                parts = lines[-1].split(",")
                # Stooq CSV: Symbol,Date,Time,Open,High,Low,Close
                if len(parts) >= 6:
                    close_val = parts[5].strip()
                    if close_val and close_val != "":
                        p = float(close_val)
                        if 100 < p < 15000:
                            results.append(("Stooq", p))
                            dl("  OK: $%.2f (from CSV)" % p)
                            log.info("[API-1b] Stooq OK: $%.2f from CSV" % p)
                        else:
                            dl("  Value out of range: %s" % close_val)
                            log.warning("[API-1b] Stooq value out of range: %s" % close_val)
                    else:
                        dl("  Empty close value")
                        log.warning("[API-1b] Stooq empty close value")
                else:
                    dl("  CSV parse fail: %d cols" % len(parts))
                    log.warning("[API-1b] Stooq CSV format error")
            else:
                dl("  Too few lines: %d" % len(lines))
                log.warning("[API-1b] Stooq too few lines")
        else:
            dl("  HTTP %d" % c_stooq)
            log.warning("[API-1b] Stooq HTTP %d" % c_stooq)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:80])
        log.warning("[API-1b] Stooq ERR: %s" % str(e)[:80])
    log.info("[API-1b] Stooq: %s" % ("OK" if any(x[0]=="Stooq" for x in results) else "FAIL"))

    # Source 1c: Eastmoney (Chinese financial site - might work since 163 SMTP works!)
    dl("[1c] Eastmoney...")
    log.info("[API-1c] Trying Eastmoney gold API...")
    try:
        # Eastmoney futures API for gold (GC = Gold Comex)
        c_em, b_em = http_get(
            "https://push2.eastmoney.com/api/qt/stock/get?secid=118.GC00Y&fields=f43,f44,f45,f46,f47,f57,f58,f170",
            12,
        )
        if c_em == 200 and b_em:
            d_em = json.loads(b_em)
            # Eastmoney returns price in CNY, need to convert or use directly
            price_val = d_em.get("data", {}).get("f43")  # latest price
            if price_val and float(price_val) > 100:
                # Eastmoney gold futures in CNY - this is already per gram-ish or needs conversion
                # f43 is usually the current price
                p_em = float(price_val)
                if 200 < p_em < 2000:
                    # This is likely CNY/g already for Chinese gold markets
                    # Store as special marker
                    results.append(("Eastmoney-CNYg", p_em))
                    dl("  OK: %.2f (CNY/g?)" % p_em)
                    log.info("[API-1c] Eastmoney OK: %.2f" % p_em)
                elif 1000 < p_em < 15000:
                    # This is USD/oz
                    results.append(("Eastmoney", p_em))
                    dl("  OK: $%.2f/oz" % p_em)
                    log.info("[API-1c] Eastmoney OK: $%.2f/oz" % p_em)
                else:
                    dl("  Value out of range: %.2f" % p_em)
                    log.warning("[API-1c] Eastmoney value out of range: %.2f" % p_em)
            else:
                dl("  No price data: %s" % str(b_em)[:100])
                log.warning("[API-1c] Eastmoney no price: %s" % b_em[:100])
        else:
            dl("  HTTP %d" % c_em)
            log.warning("[API-1c] Eastmoney HTTP %d" % c_em)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:80])
        log.warning("[API-1c] Eastmoney ERR: %s" % str(e)[:80])
    log.info("[API-1c] Eastmoney: %s" % ("OK" if any("Eastmoney" in x[0] for x in results) else "FAIL"))

    # Source 1d: Sina Finance gold
    dl("[1d] Sina Finance...")
    log.info("[API-1d] Trying Sina Finance gold...")
    try:
        c_sn, b_sn = http_get(
            "https://hq.sinajs.cn/list=GC00Y",
            10,
        )
        if c_sn == 200 and b_sn:
            # Sina format: var hq_str_GC00Y="...,...,price,..."
            m = re.search(r'GC00Y="([^"]+)"', b_sn)
            if m:
                parts = m.group(1).split(",")
                if len(parts) >= 6:
                    # Sina fields: name, open, prev_close, ...
                    try:
                        p_sn = float(parts[3])  # Usually index 3 is current or prev close
                        if 100 < p_sn < 15000:
                            results.append(("Sina", p_sn))
                            dl("  OK: %.2f" % p_sn)
                            log.info("[API-1d] Sina OK: %.2f" % p_sn)
                    except:
                        pass
            else:
                dl("  Parse failed")
                log.warning("[API-1d] Sina parse fail")
        else:
            dl("  HTTP %d" % c_sn)
            log.warning("[API-1d] Sina HTTP %d" % c_sn)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:80])
        log.warning("[API-1d] Sina ERR: %s" % str(e)[:80])

    # Source 1e: Try metals-dev with different endpoint
    dl("[1e] Metals-dev-v2...")
    log.info("[API-1e] Trying metals-dev v2...")
    try:
        d_mv2 = jget(
            "https://api.metals.dev/v1/live?api_key=demo&currency=USD&unit=toz&precise=true",
            10,
        )
        if d_mv2 and isinstance(d_mv2.get("metals"), dict):
            g_mv2 = d_mv2["metals"].get("gold", {})
            if g_mv2 and g_mv2.get("price"):
                p_mv2 = float(g_mv2["price"])
                if 1000 < p_mv2 < 15000:
                    results.append(("Metals.dev-v2", p_mv2))
                    dl("  OK: $%.2f" % p_mv2)
                    log.info("[API-1e] Metals.dev-v2 OK: $%.2f" % p_mv2)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:60])
        log.warning("[API-1e] Metals.dev-v2 ERR: %s" % str(e)[:60])

    # Source 2: Yahoo Finance
    dl("[2] Yahoo Finance...")
    log.info("[API-2] Trying Yahoo Finance...")
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

    # Source 7-10: Additional data sources
    dl("")
    dl("Total sources found: %d" % len(results))

    # Source 7: FGI (Financial Global Index) - free no-key
    dl("[7] FGI...")
    try:
        d7 = jget("https://api.fgi.gov.cn/goldPrice?_=%d" % int(time.time()), 10)
        if d7 and isinstance(d7, dict):
            for key in ["data", "price", "goldPrice", "result", "value"]:
                val = d7.get(key)
                if isinstance(val, (int, float)) and 100 < val < 20000:
                    results.append(("FGI", float(val)))
                    dl("  OK: $%.2f (key=%s)" % (float(val), key))
                    break
            else:
                dl("  Response keys: %s" % str(list(d7.keys()))[:5])
        else:
            dl("  FAIL")
    except Exception as e:
        dl("  ERR: %s" % str(e)[:60])

    # Source 8: Yahoo Finance v8 alternative
    dl("[8] Yahoo-v8...")
    try:
        c8, b8 = http_get(
            "https://query2.finance.yahoo.com/v8/finance/chart/GC=F?interval=1m&range=1d",
            12,
        )
        if c8 == 200:
            jd8 = json.loads(b8)
            meta8 = (
                jd8.get("chart", {}).get("result", [{}])[0].get("meta", {})
                if jd8.get("chart", {}).get("result") else {}
            )
            for field in ["regularMarketPrice", "previousClose"]:
                p8 = float(meta8.get(field, 0) or 0)
                if 1000 < p8 < 15000:
                    results.append(("Yahoo-v8-%s" % field[:6], p8))
                    dl("  OK: $%.2f (%s)" % (p8, field))
                    break
            else:
                dl("  No price")
        else:
            dl("  HTTP %d" % c8)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:60])

    # Source 9: metals-api.com
    dl("[9] metals-api...")
    d9 = jget("https://metals-api.com/api/latest?access_key=demo&base=USD&symbols=XAU", 10)
    if d9 and isinstance(d9, dict):
        rates = d9.get("rates", {})
        if isinstance(rates, dict) and "XAU" in rates:
            xau = float(rates["XAU"])
            if xau > 0.05:
                p9 = 1.0 / xau
                if 1000 < p9 < 15000:
                    results.append(("Metals-API2", p9))
                    dl("  OK: $%.2f" % p9)
    dl("  FAIL" if not any("Metals-API2" in x[0] for x in results) else "")

    # Source 10: goldprice.org scrape
    dl("[10] GoldPriceOrg...")
    try:
        c10, b10 = http_get("https://goldprice.org/", 12)
        if c10 == 200:
            for pat_name, pat in [
                ("gp-oz", r'(\d{1,2},?\d{3}\.\d{2})\s*(?:USD|per oz)'),
                ("gp-usd", r'\$([\d,]+\.\d{2})'),
            ]:
                m = re.search(pat, b10, re.I)
                if m:
                    raw = m.group(1).replace(",", "")
                    try:
                        p10 = float(raw)
                        if 1000 < p10 < 15000:
                            results.append(("GoldPriceOrg", p10))
                            dl("  OK: $%.2f" % p10); break
                    except: pass
        else:
            dl("  HTTP %d" % c10)
    except Exception as e:
        dl("  ERR: %s" % str(e)[:60])

    dl("")
    dl("=== Final: %d sources ===" % len(results))
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

    # Check if we have a direct CNY/g source (like Eastmoney)
    cny_g_sources = [(n, p) for n, p in prices if "CNYg" in n]
    direct_cny_g = None
    if cny_g_sources:
        direct_cny_g = cny_g_sources[0][1]
        log.info("Found direct CNY/g source: %s = %.2f" % (cny_g_sources[0][0], direct_cny_g))

    # FX rate
    fx = get_fx_rate()
    result["usd_cny"] = fx

    # Calculate bank prices
    if direct_cny_g and 200 < direct_cny_g < 2000:
        # Use direct CNY/g price!
        spot_cny_g = direct_cny_g
        bank_base = direct_cny_g
        # Reverse-calculate approximate USD/oz for display
        if fx > 0:
            approx_usd = (direct_cny_g * _OZ_PER_GRAM) / fx
            result["spot_usd"] = round(approx_usd, 2)
            log.info("Using direct CNY/g: %.2f (approx $%.2f/oz)" % (direct_cny_g, approx_usd))
        else:
            log.info("Using direct CNY/g: %.2f (no FX)" % direct_cny_g)
    else:
        # Standard: convert from USD/oz
        spot_cny_g = (spot * fx) / _OZ_PER_GRAM
        bank_base = spot_cny_g

    # Apply futures premium discount for sources that return COMEX futures (GC=F)
    # Yahoo Finance returns futures which trade ~2.5% above spot
    is_futures_src = any(k in src for k in ["Yahoo", "Stooq", "median"])
    if is_futures_src:
        old_base = bank_base
        bank_base = (bank_base * FUTURE_DISCOUNT_RATE) + FUTURE_DISCOUNT_FIXED
        dl("Futures discount: %.2f -> %.2f (rate=%.4f fixed=%.1f)" % (old_base, bank_base, FUTURE_DISCOUNT_RATE, FUTURE_DISCOUNT_FIXED))

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
    hp("</div><div class=ft>Gold Monitor v5.3 | Run #%d | GitHub Actions<br>\u81ea\u52a8\u53d1\u751f\u53d1\uff0c\u8bf7\u56de\u590d\u56de</div></div></body></html>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = RECIPIENTS
    msg["Date"] = now_cst().strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg.attach(MIMEText("\n".join(html_parts), "html", "utf-8"))

    try:
        # Try SSL first (port 465), then STARTTLS (port 587) as fallback
        srv = None
        port = int(SMTP_PORT) if SMTP_PORT else 465
        
        # Method 1: SMTP_SSL for port 465
        if port == 465:
            try:
                srv = smtplib.SMTP_SSL(SMTP_HOST, port, context=_CTX, timeout=30)
                log.info("Connected via SSL on port %d" % port)
            except Exception as e1:
                log.warning("SSL failed: %s, trying STARTTLS..." % e1)
                srv = None
        
        # Method 2: STARTTLS on port 587 (fallback or primary)
        if srv is None:
            try:
                srv = smtplib.SMTP(SMTP_HOST, 587, timeout=30)
                srv.starttls(context=_CTX)
                log.info("Connected via STARTTLS on port 587")
            except Exception as e2:
                log.warning("STARTTLS failed: %s" % e2)
                # Last resort: plain SMTP on configured port
                if srv is None:
                    try:
                        srv = smtplib.SMTP(SMTP_HOST, port, timeout=30)
                        log.info("Connected via plain SMTP on port %d" % port)
                    except Exception as e3:
                        raise Exception("All connection methods failed: SSL=%s, STARTTLS=%s, Plain=%s" % (e1, e2, e3))
        
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
