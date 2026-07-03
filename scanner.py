#!/usr/bin/env python3
"""
Stark Momentum Scanner (SMS)
────────────────────────────
Three scanners in one file:
  DAILY   — runs Mon-Fri 5 PM IST
  WEEKLY  — runs Friday 5 PM IST
  MONTHLY — runs last Friday of month 5 PM IST

Each scanner has two pools:
  Pool A — Inside CPR  (from cpr-bot JSON, score >= 6/7/7)
  Pool B — Narrow CPR  (fresh scan all F&O, width <0.1%, score >= 7/8/8)

Filter framework (same logic, scaled timeframes):
  F1  CPR type: Inside or Narrow <0.1%
  F2  ATR contraction ratio < 0.75
  F3  CPR to R1/S1 distance >= 1x ATR
  F4  Lower TF SMA compression (Daily=5m, Weekly=1H, Monthly=Daily)
  F5  Higher TF SMA stack + price vs reference CPR
      (Daily=1H+weeklyTC, Weekly=Daily+monthlyTC, Monthly=Weekly+quarterlyTC)
"""

import json, datetime, sys, smtplib, os
import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Email ─────────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
REPORT_RECIPIENT   = os.environ.get("REPORT_RECIPIENT", "").strip()

# ── cpr-bot JSON URLs ─────────────────────────────────────────────
BASE_URL   = "https://raw.githubusercontent.com/gajapriyaannadurai/cpr-bot/main/docs"
DAILY_URL  = f"{BASE_URL}/cpr_list.json"
WEEKLY_URL = f"{BASE_URL}/weekly_cpr_list.json"
MONTHLY_URL= f"{BASE_URL}/monthly_cpr_list.json"

# ── Score thresholds ──────────────────────────────────────────────
THRESHOLDS = {
    "daily":   {"inside": 6, "narrow": 7},
    "weekly":  {"inside": 6, "narrow": 7},   # loosened from 7/8
    "monthly": {"inside": 6, "narrow": 7},   # loosened from 7/8
}

# ── CPR width thresholds ──────────────────────────────────────────
NARROW_PCT     = 0.1    # Pool B: must be < 0.1%
INSIDE_NARROW  = 0.3    # Pool A: bonus narrow if < 0.3%
ATR_RATIO_TH   = 0.75

# ── SMA compression thresholds per timeframe ──────────────────────
# F4: gap between max and min of SMA20/50/200 as % of price
COMPRESS = {
    "daily":   (0.5, 1.0),   # 5-min  (tight=2pts, mild=1pt)
    "weekly":  (1.5, 3.0),   # 1H     — loosened from 1.0/2.0
    "monthly": (2.0, 4.0),   # Daily  — loosened from 1.5/3.0
}

# ── Full F&O universe for Pool B fresh scan ───────────────────────
ALL_FO = """
RELIANCE TCS HDFCBANK BHARTIARTL ICICIBANK INFY SBIN HINDUNILVR ITC LT KOTAKBANK
LICI BAJFINANCE HCLTECH MARUTI SUNPHARMA AXISBANK ADANIENT ONGC NTPC TATAMOTORS
DMART ULTRACEMCO TITAN ASIANPAINT WIPRO BAJAJFINSV NESTLEIND M&M COALINDIA POWERGRID
ADANIPORTS HAL JSWSTEEL TATASTEEL BAJAJ-AUTO TRENT IOC ADANIPOWER ADANIGREEN HINDALCO
SIEMENS PIDILITIND VBL DLF GRASIM TECHM BEL HDFCLIFE BRITANNIA CIPLA APOLLOHOSP
SBILIFE IRFC INDIGO EICHERMOT DRREDDY ABB DIVISLAB INDUSINDBK SHREECEM ZOMATO
TATACONSUME BPCL HEROMOTOCO LTIM CHOLAFIN ICICIPRULI ICICIGI HAVELLS UPL JIOFIN
GAIL TATAPOWER GODREJCP DABUR PFC RECLTD AMBUJACEM ADANIENSOL VEDL TVSMOTOR
SHRIRAMFIN BAJAJHLDNG IRCTC CGPOWER NAUKRI POLYCAB PNB BANKBARODA TIINDIA SRF
INDUSTOWER LODHA TORNTPHARM BERGEPAINT MARICO SBICARD BOSCHLTD ATGL UNITDSPR
MUTHOOTFIN ABCAPITAL BHEL CONCOR LICHSGFIN TATACOMM PETRONET MFSL MPHASIS
COLPAL HINDPETRO BHARATFORG MAXHEALTH OBEROIRLTY ZYDUSLIFE INDHOTEL BIOCON
BANKINDIA LUPIN HINDZINC ALKEM AUBANK PERSISTENT NMDC PAGEIND IDFCFIRSTB
JSWENERGY ABFRL JINDALSTEL CUMMINSIND IGL OFSS ASHOKLEY BALKRISIND POLICYBZR
SAIL OIL AUROPHARMA INDIANB UBL CANBK COFORGE TORNTPOWER MRF CROMPTON
ACC GUJGASLTD LTTS NHPC NLCINDIA IDEA APLAPOLLO ESCORTS RAMCOCEM SUNDARMFIN
MOTHERSON HUDCO YESBANK PAYTM IRB LAURUSLABS AARTIIND MANAPPURAM
ASTRAL PIIND NAM-INDIA SUPREMEIND CUB DELHIVERY DIXON PRESTIGE MAZDOCK
NYKAA TATAELXSI FEDERALBNK ENDURANCE EXIDEIND CANFIN LINDEINDIA RVNL
KPITTECH POLYMED IPCALAB SUNTV GLENMARK CAMS MAHABANK VOLTAS BSE GLAND
JBCHEPHARM CDSL JSL HONAUT APOLLOTYRE EMAMILTD UNIONBANK
TATAINVEST POONAWALLA CESC RBLBANK BANDHANBNK GODREJPROP NATIONALUM
HINDCOPPER SBFC HFCL KPRMILL FIVESTAR THERMAX KAJARIACER MEDPLUS
""".split()


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def calc_cpr(h, l, c):
    pp = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * pp - bc
    return {
        "upper": max(tc, bc), "lower": min(tc, bc),
        "width": abs(tc - bc),
        "r1": 2*pp - l, "s1": 2*pp - h
    }

def calc_atr(df, period=14):
    hi = df["High"].values; lo = df["Low"].values; cl = df["Close"].values
    trs = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
           for i in range(1, len(df))]
    return float(np.mean(trs[-period:])) if len(trs) >= period else None

def sma(series, n):
    if len(series) < n: return None
    return float(series.iloc[-n:].mean())

def sma_compress_score(df, tight_pct, mild_pct, min_bars=50):
    """Score SMA20/50/200 compression on any timeframe dataframe."""
    if df is None or len(df) < min_bars:
        return 0, "insufficient data"
    cl = df["Close"]
    s20 = sma(cl, 20); s50 = sma(cl, 50); s200 = sma(cl, 200) if len(cl) >= 200 else None
    px  = float(cl.iloc[-1])
    if not (s20 and s50):
        return 0, "SMA data missing"
    vals = [v for v in [s20, s50, s200] if v]
    gap  = round((max(vals) - min(vals)) / px * 100, 3)
    if gap < tight_pct:
        return 2, f"tight {gap:.3f}%"
    elif gap < mild_pct:
        return 1, f"mild {gap:.3f}%"
    return 0, f"spread {gap:.3f}%"

def get_monthly_cpr(df_daily):
    """Last completed month's CPR from daily data."""
    df = df_daily.copy()
    df.index = pd.to_datetime(df.index)
    mn = df.resample("ME").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    if len(mn) < 2: return None
    r = mn.iloc[-2]
    return calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))

def get_weekly_cpr_from_daily(df_daily):
    """Last completed week's CPR from daily data."""
    df = df_daily.copy()
    df.index = pd.to_datetime(df.index)
    wk = df.resample("W-FRI").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    if len(wk) < 2: return None
    r = wk.iloc[-2]
    return calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))

def get_quarterly_cpr(df_daily):
    """Last completed quarter's CPR from daily data."""
    df = df_daily.copy()
    df.index = pd.to_datetime(df.index)
    qt = df.resample("QE").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    if len(qt) < 2: return None
    r = qt.iloc[-2]
    return calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))

def fetch_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[fetch] {url} — {e}")
        return None

def get_df(bulk, sym, tickers):
    ts = f"{sym}.NS"
    try:
        if len(tickers) == 1:
            return bulk.dropna(how="all")
        if ts not in bulk.columns.get_level_values(0):
            return None
        return bulk[ts].dropna(how="all")
    except Exception:
        return None

def is_last_friday_of_month():
    today = datetime.date.today()
    if today.weekday() != 4:  # not Friday
        return False
    next_friday = today + datetime.timedelta(days=7)
    return next_friday.month != today.month


# ═══════════════════════════════════════════════════════════════════
# 5-FILTER SCORER — generic, works for all timeframes
# ═══════════════════════════════════════════════════════════════════

def score_stock(sym, tf, pool,
                df_daily, df_1h, df_5m, df_weekly,
                ref_cpr_upper=None, ref_cpr_lower=None):
    """
    tf   : "daily" | "weekly" | "monthly"
    pool : "inside" | "narrow"
    ref_cpr_upper/lower : pre-calculated CPR upper/lower for this stock
                          (from cpr-bot JSON for Pool A)
    """
    if df_daily is None or len(df_daily) < 30:
        return None

    today = df_daily.iloc[-1]
    yday  = df_daily.iloc[-2]
    close = float(today["Close"])

    # Use pre-calculated CPR if provided (Pool A), else calculate fresh
    if ref_cpr_upper is not None:
        curr_upper = ref_cpr_upper
        curr_lower = ref_cpr_lower
        curr_width = abs(curr_upper - curr_lower)
        # R1/S1 from today's OHLC
        pp = (float(today["High"]) + float(today["Low"]) + float(today["Close"])) / 3
        curr_r1 = 2*pp - float(today["Low"])
        curr_s1 = 2*pp - float(today["High"])
    else:
        # Fresh calculation for Pool B narrow scan
        if tf == "daily":
            c = calc_cpr(float(today["High"]), float(today["Low"]), float(today["Close"]))
        elif tf == "weekly":
            df_daily.index = pd.to_datetime(df_daily.index)
            wk = df_daily.resample("W-FRI").agg({"High":"max","Low":"min","Close":"last"}).dropna()
            if len(wk) < 1: return None
            r = wk.iloc[-1]
            c = calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))
        else:  # monthly
            df_daily.index = pd.to_datetime(df_daily.index)
            mn = df_daily.resample("ME").agg({"High":"max","Low":"min","Close":"last"}).dropna()
            if len(mn) < 1: return None
            r = mn.iloc[-1]
            c = calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))
        curr_upper = c["upper"]; curr_lower = c["lower"]
        curr_width = c["width"]; curr_r1 = c["r1"]; curr_s1 = c["s1"]

    width_pct = (curr_width / close) * 100

    # ATR — use appropriate timeframe bars
    if tf == "daily":
        atr_df = df_daily
    elif tf == "weekly":
        df_daily.index = pd.to_datetime(df_daily.index)
        atr_df = df_daily.resample("W-FRI").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    else:
        df_daily.index = pd.to_datetime(df_daily.index)
        atr_df = df_daily.resample("ME").agg({"High":"max","Low":"min","Close":"last"}).dropna()

    atr14 = calc_atr(atr_df.iloc[-20:] if len(atr_df) >= 20 else atr_df, 14)
    if atr14 is None: return None

    # ── F1: CPR type ─────────────────────────────────────────────
    if pool == "inside":
        is_narrow_bonus = width_pct < INSIDE_NARROW
        f1 = 2 if is_narrow_bonus else 1
        f1_note = f"Inside{'+ Narrow' if is_narrow_bonus else ''} ({width_pct:.3f}%)"
    else:
        if width_pct >= NARROW_PCT: return None
        f1 = 2; f1_note = f"Narrow CPR ({width_pct:.3f}%)"

    # ── F2: ATR contraction ───────────────────────────────────────
    atr_list = []
    for i in range(2, min(13, len(atr_df)-14)):
        a = calc_atr(atr_df.iloc[-(i+14):-i], 14)
        if a: atr_list.append(a)
    if atr_list:
        avg = float(np.mean(atr_list[:10]))
        ratio = round(atr14/avg, 3) if avg > 0 else 1.0
        f2 = 2 if ratio < ATR_RATIO_TH else (1 if ratio < 0.90 else 0)
        f2_note = f"ATR ratio {ratio}"
    else:
        ratio = None; f2 = 1; f2_note = "limited ATR data"

    # ── F3: R1/S1 room ───────────────────────────────────────────
    dist_r1 = curr_r1 - curr_upper
    dist_s1 = curr_lower - curr_s1
    f3 = 2 if (dist_r1 >= atr14 or dist_s1 >= atr14) else \
         1 if (dist_r1 >= 0.5*atr14 or dist_s1 >= 0.5*atr14) else 0
    f3_note = f"R1 gap {dist_r1:.1f} | S1 gap {dist_s1:.1f}"

    # ── F4: Lower TF SMA compression ─────────────────────────────
    tight, mild = COMPRESS[tf]
    if tf == "daily":
        f4, f4_note = sma_compress_score(df_5m,    tight, mild, min_bars=200)
        f4_note = f"5m {f4_note}"
    elif tf == "weekly":
        f4, f4_note = sma_compress_score(df_1h,    tight, mild, min_bars=50)
        f4_note = f"1H {f4_note}"
    else:  # monthly
        f4, f4_note = sma_compress_score(df_daily, tight, mild, min_bars=50)
        f4_note = f"Daily {f4_note}"

    # ── F5: Higher TF SMA stack + price vs reference CPR ─────────
    htf = "NEUTRAL"; f5 = 0; f5_note = "no HTF data"

    if tf == "daily":
        htf_df  = df_1h
        ref_cpr_func = get_weekly_cpr_from_daily
        min_bars_htf = 50
        tf_label = "1H"
        use_midpoint = False   # daily: strict price vs weekly TC/BC
    elif tf == "weekly":
        htf_df  = df_daily
        ref_cpr_func = get_monthly_cpr
        min_bars_htf = 50
        tf_label = "Daily"
        use_midpoint = True    # weekly: price vs monthly CPR midpoint
    else:  # monthly
        htf_df  = df_weekly
        ref_cpr_func = get_quarterly_cpr
        min_bars_htf = 20
        tf_label = "Weekly"
        use_midpoint = True    # monthly: price vs quarterly CPR midpoint

    if htf_df is not None and len(htf_df) >= min_bars_htf:
        hc    = htf_df["Close"]
        h20   = sma(hc, 20); h50 = sma(hc, 50)
        h200  = sma(hc, 200) if len(hc) >= 200 else None
        h_px  = float(hc.iloc[-1])

        ref   = ref_cpr_func(df_daily)
        r_tc  = ref["upper"] if ref else None
        r_bc  = ref["lower"] if ref else None
        r_mid = ((r_tc + r_bc) / 2) if (r_tc and r_bc) else None

        if h20 and h50:
            # For midpoint mode: bullish = above midpoint, bearish = below
            if use_midpoint and r_mid:
                abv_tc = h_px >= r_mid
                blw_bc = h_px < r_mid
            else:
                abv_tc = r_tc and h_px >= r_tc
                blw_bc = r_bc and h_px <= r_bc

            ref_val_str = f"mid {r_mid:.0f}" if use_midpoint and r_mid else \
                          (f"TC {r_tc:.0f}" if r_tc else "N/A")

            full_bull = h200 and (h20 > h50 > h200)
            full_bear = h200 and (h20 < h50 < h200)
            part_bull = h20 > h50
            part_bear = h20 < h50

            if h_px >= h20 and full_bull and abv_tc:
                f5=2; htf="BULLISH"
                f5_note=f"{tf_label} perfect bull + above {ref_val_str}"
            elif h_px >= h20 and part_bull and abv_tc:
                f5=1; htf="BULLISH"
                f5_note=f"{tf_label} good bull + above {ref_val_str}"
            elif h_px <= h20 and full_bear and blw_bc:
                f5=2; htf="BEARISH"
                f5_note=f"{tf_label} perfect bear + below {ref_val_str}"
            elif h_px <= h20 and part_bear and blw_bc:
                f5=1; htf="BEARISH"
                f5_note=f"{tf_label} good bear + below {ref_val_str}"
            else:
                f5=0; htf="NEUTRAL"
                f5_note=f"{tf_label} px={h_px:.1f} vs {ref_val_str} | 20SMA={h20:.1f} 50SMA={h50:.1f}"

    score = f1 + f2 + f3 + f4 + f5
    setup = htf if htf in ("BULLISH","BEARISH") else "NEUTRAL"

    return {
        "sym": sym, "setup": setup, "score": score,
        "width_pct": round(width_pct, 3), "atr_ratio": ratio, "pool": pool,
        "filters": {
            "f1":{"score":f1,"note":f1_note}, "f2":{"score":f2,"note":f2_note},
            "f3":{"score":f3,"note":f3_note}, "f4":{"score":f4,"note":f4_note},
            "f5":{"score":f5,"note":f5_note},
        }
    }


def _clean(r):
    return {
        "sym": r["sym"], "setup": r["setup"], "score": r["score"],
        "width_pct": r["width_pct"], "atr_ratio": r["atr_ratio"], "pool": r["pool"],
        "f1":r["filters"]["f1"]["score"],"f1_note":r["filters"]["f1"]["note"],
        "f2":r["filters"]["f2"]["score"],"f2_note":r["filters"]["f2"]["note"],
        "f3":r["filters"]["f3"]["score"],"f3_note":r["filters"]["f3"]["note"],
        "f4":r["filters"]["f4"]["score"],"f4_note":r["filters"]["f4"]["note"],
        "f5":r["filters"]["f5"]["score"],"f5_note":r["filters"]["f5"]["note"],
        "tv_link": f"https://www.tradingview.com/chart/?symbol=NSE%3A{r['sym']}"
    }


# ═══════════════════════════════════════════════════════════════════
# RUN ONE TIMEFRAME
# ═══════════════════════════════════════════════════════════════════

def run_timeframe(tf, cpr_json, all_dfs, tickers):
    """Score Pool A (inside CPR) and Pool B (narrow fresh scan) for one timeframe."""
    thresh = THRESHOLDS[tf]
    df_daily = all_dfs["daily"]; df_1h = all_dfs["1h"]
    df_5m    = all_dfs["5m"];    df_weekly = all_dfs["weekly"]

    # ── Pool A: Inside CPR from cpr-bot ──────────────────────────
    inside_results = []
    inside_debug   = []   # store all scores for debug
    if cpr_json and cpr_json.get("stocks"):
        pool_a_syms = [(s["sym"], s.get("cpr_upper"), s.get("cpr_lower"))
                       for s in cpr_json["stocks"]]
        print(f"[{tf}] Pool A: {len(pool_a_syms)} inside CPR stocks")
        for sym, cup, clo in pool_a_syms:
            try:
                r = score_stock(sym, tf, "inside",
                                get_df(df_daily, sym, tickers),
                                get_df(df_1h,    sym, tickers),
                                get_df(df_5m,    sym, tickers),
                                get_df(df_weekly,sym, tickers) if df_weekly is not None else None,
                                ref_cpr_upper=cup, ref_cpr_lower=clo)
                if r:
                    inside_debug.append(r)
                    if r["score"] >= thresh["inside"]:
                        inside_results.append(r)
            except Exception as ex:
                print(f"[{tf}] {sym}: {ex}")

        # Debug: print top 5 scores regardless of threshold
        if not inside_results:
            inside_debug.sort(key=lambda x: x["score"], reverse=True)
            print(f"[{tf}] DEBUG — top scores from Pool A (none passed threshold {thresh['inside']}):")
            for r in inside_debug[:5]:
                print(f"  {r['sym']} score={r['score']} setup={r['setup']} "
                      f"f1={r['filters']['f1']['score']} f2={r['filters']['f2']['score']} "
                      f"f3={r['filters']['f3']['score']} f4={r['filters']['f4']['score']} "
                      f"f5={r['filters']['f5']['score']}")
                print(f"    f4: {r['filters']['f4']['note']}")
                print(f"    f5: {r['filters']['f5']['note']}")
    inside_results.sort(key=lambda x: x["score"], reverse=True)

    # ── Pool B: Narrow CPR fresh scan ────────────────────────────
    inside_syms = {s["sym"] for s in (cpr_json.get("stocks") or [])}
    narrow_syms = [s for s in ALL_FO if s not in inside_syms]
    narrow_results = []
    print(f"[{tf}] Pool B: scanning {len(narrow_syms)} stocks for narrow CPR <0.1%")
    for sym in narrow_syms:
        try:
            r = score_stock(sym, tf, "narrow",
                            get_df(df_daily, sym, tickers),
                            get_df(df_1h,    sym, tickers),
                            get_df(df_5m,    sym, tickers),
                            get_df(df_weekly,sym, tickers) if df_weekly is not None else None)
            if r and r["score"] >= thresh["narrow"]:
                narrow_results.append(r)
        except Exception as ex:
            print(f"[{tf}] {sym}: {ex}")
    narrow_results.sort(key=lambda x: x["score"], reverse=True)

    def split(lst):
        return ([r for r in lst if r["setup"]=="BULLISH"],
                [r for r in lst if r["setup"]=="BEARISH"])

    i_bull, i_bear = split(inside_results)
    n_bull, n_bear = split(narrow_results)
    print(f"[{tf}] Inside Bull:{len(i_bull)} Bear:{len(i_bear)} | Narrow Bull:{len(n_bull)} Bear:{len(n_bear)}")

    return {
        "inside_cpr": {"bullish":[_clean(r) for r in i_bull], "bearish":[_clean(r) for r in i_bear]},
        "narrow_cpr": {"bullish":[_clean(r) for r in n_bull], "bearish":[_clean(r) for r in n_bear]},
        "i_bull": i_bull, "i_bear": i_bear, "n_bull": n_bull, "n_bear": n_bear,
    }


# ═══════════════════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════════════════

def send_email(results_map, for_labels, generated_at):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and REPORT_RECIPIENT):
        print("[email] missing credentials"); return
    recipients = [r.strip() for r in REPORT_RECIPIENT.split(",") if r.strip()]

    def rows(names, color):
        if not names:
            return "<tr><td style='color:#888;padding:3px 0'>None</td></tr>"
        return "".join(f"<tr><td style='color:{color};font-weight:700;padding:3px 0;font-size:13px'>{s}</td></tr>"
                       for s in names)

    def tf_section(label, res, for_lbl):
        ib = [r["sym"] for r in res["i_bull"]]; ibr = [r["sym"] for r in res["i_bear"]]
        nb = [r["sym"] for r in res["n_bull"]]; nbr = [r["sym"] for r in res["n_bear"]]
        return f"""
        <div style="margin-bottom:20px">
          <div style="background:#1a2847;color:#fff;padding:8px 14px;border-radius:6px;
                      font-weight:700;font-size:13px;letter-spacing:0.5px">
            {label} &nbsp;<span style="opacity:0.55;font-weight:400;font-size:11px">{for_lbl}</span>
          </div>
          <div style="padding:10px 0">
            <table style="width:100%"><tr>
              <td style="vertical-align:top;width:50%;padding-right:12px">
                <div style="font-size:10px;color:#2d8a4e;font-weight:700;margin-bottom:4px">
                  INSIDE CPR BULLISH ({len(ib)})</div>
                <table>{rows(ib,'#2d8a4e')}</table>
                <div style="font-size:10px;color:#b41e1e;font-weight:700;margin:10px 0 4px">
                  INSIDE CPR BEARISH ({len(ibr)})</div>
                <table>{rows(ibr,'#b41e1e')}</table>
              </td>
              <td style="vertical-align:top;width:50%;border-left:1px solid #e2e6ed;padding-left:12px">
                <div style="font-size:10px;color:#1a7a4a;font-weight:700;margin-bottom:4px">
                  NARROW &lt;0.1% BULLISH ({len(nb)})</div>
                <table>{rows(nb,'#1a7a4a')}</table>
                <div style="font-size:10px;color:#8b0000;font-weight:700;margin:10px 0 4px">
                  NARROW &lt;0.1% BEARISH ({len(nbr)})</div>
                <table>{rows(nbr,'#8b0000')}</table>
              </td>
            </tr></table>
          </div>
        </div>"""

    tfs_run = list(results_map.keys())
    subject_parts = []
    for tf in tfs_run:
        r = results_map[tf]
        total = len(r["i_bull"])+len(r["i_bear"])+len(r["n_bull"])+len(r["n_bear"])
        subject_parts.append(f"{tf.capitalize()}:{total}")

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;color:#1a2847">
      <div style="background:#1a2847;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
        <div style="font-size:11px;opacity:0.55;letter-spacing:1px">STARK SCHOOL OF FINANCE</div>
        <div style="font-size:18px;font-weight:700;margin-top:4px">Stark Momentum Scanner</div>
        <div style="font-size:12px;opacity:0.5;margin-top:2px">{generated_at}</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e6ed;border-top:none;
                  padding:20px 24px;border-radius:0 0 8px 8px">
        {"".join(tf_section(tf.upper(), results_map[tf], for_labels.get(tf,"")) for tf in tfs_run)}
        <div style="font-size:11px;color:#888;text-align:center;margin-top:8px">
          Happy Price Action Trading · www.tradingwithgp.com
        </div>
      </div>
    </div>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = ", ".join(recipients)
        msg["Subject"] = f"📊 SMS — {' | '.join(subject_parts)} — {generated_at[:10]}"
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls(); s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, recipients, msg.as_string())
        print(f"[email] Sent to {recipients}")
    except Exception as e:
        print(f"[email] Error: {e}")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    generated_at = ist.strftime("%Y-%m-%d %H:%M IST")
    today_dow    = ist.weekday()   # 0=Mon 4=Fri
    print(f"[start] {generated_at}")

    # ── Decide which timeframes to run ───────────────────────────
    run_daily   = True
    run_weekly  = (today_dow == 4)                         # Friday
    run_monthly = (today_dow == 4 and is_last_friday_of_month())

    print(f"[run] Daily:{run_daily} Weekly:{run_weekly} Monthly:{run_monthly}")

    # ── Fetch CPR lists ───────────────────────────────────────────
    daily_json   = fetch_json(DAILY_URL)
    weekly_json  = fetch_json(WEEKLY_URL)  if run_weekly  else None
    monthly_json = fetch_json(MONTHLY_URL) if run_monthly else None

    if run_monthly and not monthly_json:
        print("[monthly] monthly_cpr_list.json not available — skipping monthly")
        run_monthly = False

    # ── Collect all symbols for bulk download ────────────────────
    def syms_from(j):
        return [s["sym"] for s in (j or {}).get("stocks", [])] if j else []

    all_syms = list(dict.fromkeys(
        syms_from(daily_json) + syms_from(weekly_json) +
        syms_from(monthly_json) + ALL_FO
    ))
    tickers = [f"{s}.NS" for s in all_syms]
    print(f"[data] {len(tickers)} total tickers to download")

    def dl(period, interval=None, timeout=180):
        kwargs = dict(group_by="ticker", auto_adjust=False,
                      progress=False, threads=True, timeout=timeout)
        if interval: kwargs["interval"] = interval
        try:
            return yf.download(tickers, period=period, **kwargs)
        except Exception as e:
            print(f"[data] {interval or 'daily'} error: {e}"); return None

    print("[data] Downloading daily (14mo)...")
    df_d = dl("14mo")
    print("[data] Downloading 1H (60d)...")
    df_h = dl("60d",  "1h")
    print("[data] Downloading 5m (60d)...")
    df_5 = dl("60d",  "5m")
    df_w = None
    if run_monthly:
        print("[data] Downloading weekly (5y)...")
        df_w = dl("5y", "1wk")

    if df_d is None or df_d.empty:
        print("[abort] daily data failed"); sys.exit(1)

    all_dfs = {"daily": df_d, "1h": df_h, "5m": df_5, "weekly": df_w}

    # ── Run scanners ──────────────────────────────────────────────
    results_map = {}; for_labels = {}

    if run_daily:
        results_map["daily"] = run_timeframe("daily", daily_json, all_dfs, all_syms)
        for_labels["daily"]  = daily_json.get("for_date","") if daily_json else ""

    if run_weekly:
        results_map["weekly"] = run_timeframe("weekly", weekly_json, all_dfs, all_syms)
        for_labels["weekly"]  = weekly_json.get("for_week","") if weekly_json else ""

    if run_monthly:
        results_map["monthly"] = run_timeframe("monthly", monthly_json, all_dfs, all_syms)
        for_labels["monthly"]  = monthly_json.get("for_month","") if monthly_json else ""

    # ── Write results.json ────────────────────────────────────────
    os.makedirs("docs", exist_ok=True)
    payload = {"generated_at": generated_at, "for_labels": for_labels}
    for tf, res in results_map.items():
        payload[tf] = {
            "for":        for_labels.get(tf,""),
            "inside_cpr": res["inside_cpr"],
            "narrow_cpr": res["narrow_cpr"],
            "min_score_inside": THRESHOLDS[tf]["inside"],
            "min_score_narrow": THRESHOLDS[tf]["narrow"],
        }

    with open("docs/results.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("[out] docs/results.json written")

    # ── Send email ────────────────────────────────────────────────
    send_email(results_map, for_labels, generated_at)


if __name__ == "__main__":
    main()
