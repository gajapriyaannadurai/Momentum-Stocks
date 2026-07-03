#!/usr/bin/env python3
"""
Stark Momentum Scanner (SMS) — momentum-stocks repo
────────────────────────────────────────────────────
Two pools scanned daily:

  POOL A — Inside CPR  (from cpr-bot, score >= 6)
  POOL B — Narrow CPR  (all F&O stocks, width < 0.1%, score >= 7)

Both pools use the same 5 filters:
  F1  CPR type: Inside (1-2pts) or Narrow <0.1% (2pts)
  F2  ATR contraction: today ATR / 10d avg ATR < 0.75
  F3  CPR-to-R1 / S1 distance >= 1x ATR
  F4  5-min SMA compression: gap(SMA20,50,200) < 0.5% = 2pts, < 1.0% = 1pt
  F5  1H: price>=SMA20>SMA50>SMA200 + price>=wTC = 2pts (perfect)
          price>=SMA20>SMA50 + price>=wTC          = 1pt  (good)

Output: docs/results.json with inside_cpr + narrow_cpr sections
Email:  clean stock name list at 5 PM IST
"""

import json, datetime, sys, smtplib, os
import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ── Email ─────────────────────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
REPORT_RECIPIENT   = os.environ.get("REPORT_RECIPIENT", "").strip()

# ── Source for Pool A ─────────────────────────────────────────────────────────
CPR_BOT_JSON_URL = "https://gajapriyaannadurai.github.io/cpr-bot/cpr_list.json"

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_SCORE_INSIDE = 6      # Pool A minimum score
MIN_SCORE_NARROW = 7      # Pool B minimum score (stricter)
INSIDE_CPR_PCT   = 0.3    # Inside CPR: width < 0.3% counts as narrow bonus
NARROW_CPR_PCT   = 0.1    # Pool B: only stocks with CPR width < 0.1%
ATR_RATIO_TH     = 0.75   # ATR contraction threshold
SMA_COMPRESS_2PT = 0.5    # 5-min SMA gap < 0.5% → 2pts
SMA_COMPRESS_1PT = 1.0    # 5-min SMA gap < 1.0% → 1pt

# ── Full F&O universe for Pool B ─────────────────────────────────────────────
ALL_FO_STOCKS = """
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
    r1 = 2 * pp - l
    s1 = 2 * pp - h
    return {
        "upper": max(tc, bc), "lower": min(tc, bc),
        "width": abs(tc - bc), "r1": r1, "s1": s1
    }

def calc_atr(df, period=14):
    hi = df["High"].values; lo = df["Low"].values; cl = df["Close"].values
    trs = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
           for i in range(1, len(df))]
    if len(trs) < period: return None
    return float(np.mean(trs[-period:]))

def sma(series, n):
    if len(series) < n: return None
    return float(series.iloc[-n:].mean())

def weekly_cpr(df_daily):
    df = df_daily.copy()
    df.index = pd.to_datetime(df.index)
    wk = df.resample("W-FRI").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    if len(wk) < 2: return None
    r = wk.iloc[-2]
    return calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))

def get_df(bulk, ticker, tickers):
    try:
        if len(tickers) == 1:
            return bulk.dropna(how="all")
        if ticker not in bulk.columns.get_level_values(0):
            return None
        return bulk[ticker].dropna(how="all")
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# FETCH INSIDE CPR LIST FROM CPR-BOT (Pool A)
# ═══════════════════════════════════════════════════════════════════

def fetch_inside_cpr_symbols():
    try:
        with urllib.request.urlopen(CPR_BOT_JSON_URL, timeout=30) as r:
            data = json.loads(r.read().decode())
        syms = [s["sym"] for s in data.get("stocks", [])]
        if syms:
            print(f"[fetch] Pool A: {len(syms)} inside-CPR symbols from cpr-bot")
            return syms, data.get("for_date", "")
    except Exception as e:
        print(f"[fetch] cpr-bot JSON failed: {e}")
    try:
        fb = "https://raw.githubusercontent.com/gajapriyaannadurai/cpr-bot/main/docs/cpr_list.json"
        with urllib.request.urlopen(fb, timeout=30) as r:
            data = json.loads(r.read().decode())
        syms = [s["sym"] for s in data.get("stocks", [])]
        print(f"[fetch] Fallback: {len(syms)} symbols")
        return syms, data.get("for_date", "")
    except Exception as e:
        print(f"[fetch] Fallback failed: {e}")
        return [], ""


# ═══════════════════════════════════════════════════════════════════
# 5-FILTER SCORER  (shared by both pools)
# ═══════════════════════════════════════════════════════════════════

def score_stock(sym, df_d, df_1h, df_5m, pool="inside"):
    """
    pool = "inside" → F1 checks Inside CPR condition
    pool = "narrow" → F1 checks Narrow CPR < 0.1% condition
    Returns scored dict or None.
    """
    if df_d is None or len(df_d) < 30:
        return None

    today = df_d.iloc[-1]; yday = df_d.iloc[-2]
    close = float(today["Close"])

    curr_cpr = calc_cpr(float(today["High"]), float(today["Low"]), float(today["Close"]))
    prev_cpr = calc_cpr(float(yday["High"]),  float(yday["Low"]),  float(yday["Close"]))

    width_pct = (curr_cpr["width"] / close) * 100
    atr14     = calc_atr(df_d.iloc[-20:], 14)
    if atr14 is None: return None

    # ── F1: CPR type check ────────────────────────────────────────
    if pool == "inside":
        is_inside = (curr_cpr["upper"] <= prev_cpr["upper"] and
                     curr_cpr["lower"] >= prev_cpr["lower"])
        if not is_inside: return None
        is_narrow = width_pct < INSIDE_CPR_PCT
        f1 = 2 if is_narrow else 1
        f1_note = f"Inside{'+ Narrow' if is_narrow else ''} ({width_pct:.3f}%)"
    else:  # narrow pool
        if width_pct >= NARROW_CPR_PCT: return None   # must be < 0.1%
        f1 = 2
        f1_note = f"Narrow CPR ({width_pct:.3f}%)"

    # ── F2: ATR contraction ───────────────────────────────────────
    atr_list = []
    for i in range(2, min(13, len(df_d)-14)):
        a = calc_atr(df_d.iloc[-(i+14):-i], 14)
        if a: atr_list.append(a)
    if atr_list:
        avg_atr = float(np.mean(atr_list[:10]))
        ratio   = round(atr14 / avg_atr, 3) if avg_atr > 0 else 1.0
        f2      = 2 if ratio < ATR_RATIO_TH else (1 if ratio < 0.90 else 0)
        f2_note = f"ATR ratio {ratio}"
    else:
        ratio = None; f2 = 1; f2_note = "limited data"

    # ── F3: R1 / S1 room ─────────────────────────────────────────
    dist_r1 = curr_cpr["r1"] - curr_cpr["upper"]
    dist_s1 = curr_cpr["lower"] - curr_cpr["s1"]
    f3 = 2 if (dist_r1 >= atr14 or dist_s1 >= atr14) else \
         1 if (dist_r1 >= 0.5*atr14 or dist_s1 >= 0.5*atr14) else 0
    f3_note = f"R1 gap {dist_r1:.1f} | S1 gap {dist_s1:.1f}"

    # ── F4: 5-min SMA compression ─────────────────────────────────
    f4 = 0; f4_note = "no 5-min data"
    if df_5m is not None and len(df_5m) >= 200:
        m5c    = df_5m["Close"]
        m5_20  = sma(m5c, 20); m5_50 = sma(m5c, 50); m5_200 = sma(m5c, 200)
        m5_px  = float(m5c.iloc[-1])
        if m5_20 and m5_50 and m5_200:
            gap_pct = (max(m5_20,m5_50,m5_200) - min(m5_20,m5_50,m5_200)) / m5_px * 100
            gap_pct = round(gap_pct, 3)
            if gap_pct < SMA_COMPRESS_2PT:
                f4 = 2; f4_note = f"5m tight ({gap_pct:.3f}%)"
            elif gap_pct < SMA_COMPRESS_1PT:
                f4 = 1; f4_note = f"5m mild ({gap_pct:.3f}%)"
            else:
                f4 = 0; f4_note = f"5m spread ({gap_pct:.3f}%)"
        else:
            f4 = 1; f4_note = "5m partial SMA data"
    elif df_5m is not None and len(df_5m) >= 50:
        f4 = 1; f4_note = "5m limited (<200 bars)"

    # ── F5: 1H SMA + weekly TC/BC ────────────────────────────────
    htf = "UNKNOWN"; f5 = 0; f5_note = "no 1H data"

    if df_1h is not None and len(df_1h) >= 50:
        h1c   = df_1h["Close"]
        h1_20 = sma(h1c, 20); h1_50 = sma(h1c, 50)
        h1_200= sma(h1c, 200) if len(h1c) >= 200 else None
        h1_px = float(h1c.iloc[-1])
        wcpr  = weekly_cpr(df_d)
        w_tc  = wcpr["upper"] if wcpr else None
        w_bc  = wcpr["lower"] if wcpr else None

        if h1_20 and h1_50:
            abv_tc = w_tc and h1_px >= w_tc
            blw_bc = w_bc and h1_px <= w_bc
            full_bull = h1_200 and (h1_20 > h1_50 > h1_200)
            full_bear = h1_200 and (h1_20 < h1_50 < h1_200)
            part_bull = h1_20 > h1_50
            part_bear = h1_20 < h1_50

            if h1_px >= h1_20 and full_bull and abv_tc:
                f5=2; htf="BULLISH"; f5_note=f"1H perfect bull + above wTC {w_tc:.0f}"
            elif h1_px >= h1_20 and part_bull and abv_tc:
                f5=1; htf="BULLISH"; f5_note="1H good bull + above wTC (200 mixed)"
            elif h1_px <= h1_20 and full_bear and blw_bc:
                f5=2; htf="BEARISH"; f5_note=f"1H perfect bear + below wBC {w_bc:.0f}"
            elif h1_px <= h1_20 and part_bear and blw_bc:
                f5=1; htf="BEARISH"; f5_note="1H good bear + below wBC (200 mixed)"
            else:
                f5=0; htf="NEUTRAL"; f5_note="1H conditions not met"
        else:
            f5=0; htf="NEUTRAL"; f5_note="1H SMA data insufficient"

    # ── Score + setup ─────────────────────────────────────────────
    score = f1 + f2 + f3 + f4 + f5
    setup = htf if htf in ("BULLISH","BEARISH") else "NEUTRAL"

    return {
        "sym": sym, "setup": setup, "score": score,
        "width_pct": round(width_pct, 3), "atr_ratio": ratio,
        "pool": pool,
        "filters": {
            "f1": {"score":f1,"note":f1_note}, "f2": {"score":f2,"note":f2_note},
            "f3": {"score":f3,"note":f3_note}, "f4": {"score":f4,"note":f4_note},
            "f5": {"score":f5,"note":f5_note},
        }
    }


# ═══════════════════════════════════════════════════════════════════
# EMAIL
# ═══════════════════════════════════════════════════════════════════

def send_watchlist_email(i_bull, i_bear, n_bull, n_bear, for_date, generated_at):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and REPORT_RECIPIENT):
        print("[email] missing credentials — skipping"); return
    recipients = [r.strip() for r in REPORT_RECIPIENT.split(",") if r.strip()]

    def stock_rows(names, color):
        if not names:
            return "<tr><td style='color:#888;padding:4px 0'>None today</td></tr>"
        return "".join(
            f"<tr><td style='padding:4px 16px 4px 0;font-weight:600;color:{color};font-size:14px'>{s}</td></tr>"
            for s in names
        )

    def section(title, bull, bear, color_b="#2d8a4e", color_r="#b41e1e"):
        return f"""
        <div style="margin-bottom:24px">
          <div style="font-size:13px;font-weight:700;letter-spacing:1px;
                      color:#1a2847;border-bottom:2px solid #e2e6ed;
                      padding-bottom:6px;margin-bottom:12px">{title}</div>
          <table style="width:100%"><tr>
            <td style="vertical-align:top;width:50%">
              <div style="font-size:11px;color:{color_b};font-weight:700;margin-bottom:6px">
                BULLISH ({len(bull)})</div>
              <table>{stock_rows(bull, color_b)}</table>
            </td>
            <td style="vertical-align:top;width:50%">
              <div style="font-size:11px;color:{color_r};font-weight:700;margin-bottom:6px">
                BEARISH ({len(bear)})</div>
              <table>{stock_rows(bear, color_r)}</table>
            </td>
          </tr></table>
        </div>"""

    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;color:#1a2847">
      <div style="background:#1a2847;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
        <div style="font-size:11px;opacity:0.55;letter-spacing:1px">STARK SCHOOL OF FINANCE</div>
        <div style="font-size:18px;font-weight:700;margin-top:4px">Stark Momentum Scanner</div>
        <div style="font-size:12px;opacity:0.5;margin-top:2px">For {for_date} &nbsp;·&nbsp; {generated_at}</div>
      </div>
      <div style="background:#fff;border:1px solid #e2e6ed;border-top:none;
                  padding:20px 24px;border-radius:0 0 8px 8px">
        {section("INSIDE CPR SETUPS (score ≥ 6)", i_bull, i_bear)}
        {section("NARROW CPR SETUPS — &lt;0.1% (score ≥ 7)", n_bull, n_bear)}
        <div style="font-size:11px;color:#888;text-align:center;margin-top:8px">
          Happy Price Action Trading &nbsp;|&nbsp; www.tradingwithgp.com
        </div>
      </div>
    </div>"""

    total = len(i_bull)+len(i_bear)+len(n_bull)+len(n_bear)
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = GMAIL_ADDRESS
        msg["To"]      = ", ".join(recipients)
        msg["Subject"] = f"📊 SMS — {for_date} | Inside:{len(i_bull)}B/{len(i_bear)}Be  Narrow:{len(n_bull)}B/{len(n_bear)}Be  Total:{total}"
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
    print(f"[start] {generated_at}")

    # ── Pool A: Inside CPR symbols from cpr-bot ───────────────────
    inside_syms, for_date = fetch_inside_cpr_symbols()
    print(f"[pool-a] {len(inside_syms)} inside CPR symbols")

    # ── Pool B: All F&O stocks for narrow CPR scan ────────────────
    narrow_syms = [s for s in ALL_FO_STOCKS if s not in inside_syms]
    print(f"[pool-b] {len(narrow_syms)} F&O stocks to scan for narrow CPR")

    # ── Combine for bulk download ─────────────────────────────────
    all_syms    = list(dict.fromkeys(inside_syms + narrow_syms))
    all_tickers = [f"{s}.NS" for s in all_syms]

    print(f"[data] Downloading daily data ({len(all_tickers)} tickers)...")
    try:
        df_daily = yf.download(
            all_tickers, period="14mo", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=180
        )
    except Exception as e:
        print(f"[data] Daily error: {e}"); sys.exit(1)

    print("[data] Downloading 1H data...")
    try:
        df_1h = yf.download(
            all_tickers, period="60d", interval="1h", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=180
        )
    except Exception as e:
        print(f"[data] 1H error: {e}"); df_1h = None

    print("[data] Downloading 5-min data...")
    try:
        df_5m = yf.download(
            all_tickers, period="60d", interval="5m", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=180
        )
    except Exception as e:
        print(f"[data] 5m error: {e}"); df_5m = None

    def gdf(bulk, sym):
        return get_df(bulk, f"{sym}.NS", all_tickers)

    # ── Score Pool A ──────────────────────────────────────────────
    print("[score] Pool A — Inside CPR...")
    inside_results = []
    for sym in inside_syms:
        try:
            r = score_stock(sym,
                            gdf(df_daily, sym),
                            gdf(df_1h,    sym) if df_1h is not None else None,
                            gdf(df_5m,    sym) if df_5m is not None else None,
                            pool="inside")
            if r and r["score"] >= MIN_SCORE_INSIDE:
                inside_results.append(r)
        except Exception as ex:
            print(f"[score] {sym}: {ex}")
    inside_results.sort(key=lambda x: x["score"], reverse=True)

    # ── Score Pool B ──────────────────────────────────────────────
    print("[score] Pool B — Narrow CPR scan...")
    narrow_results = []
    for sym in narrow_syms:
        try:
            r = score_stock(sym,
                            gdf(df_daily, sym),
                            gdf(df_1h,    sym) if df_1h is not None else None,
                            gdf(df_5m,    sym) if df_5m is not None else None,
                            pool="narrow")
            if r and r["score"] >= MIN_SCORE_NARROW:
                narrow_results.append(r)
        except Exception as ex:
            print(f"[score] {sym}: {ex}")
    narrow_results.sort(key=lambda x: x["score"], reverse=True)

    # ── Split by setup ────────────────────────────────────────────
    def split(results):
        bull = [r for r in results if r["setup"] == "BULLISH"]
        bear = [r for r in results if r["setup"] == "BEARISH"]
        return bull, bear

    i_bull, i_bear = split(inside_results)
    n_bull, n_bear = split(narrow_results)

    print(f"[done] Inside — Bull:{len(i_bull)} Bear:{len(i_bear)}")
    print(f"[done] Narrow — Bull:{len(n_bull)} Bear:{len(n_bear)}")

    # ── Write results.json ────────────────────────────────────────
    os.makedirs("docs", exist_ok=True)
    payload = {
        "generated_at": generated_at,
        "for_date":     for_date or (ist + datetime.timedelta(days=1)).strftime("%d-%m-%Y"),
        "min_score_inside": MIN_SCORE_INSIDE,
        "min_score_narrow": MIN_SCORE_NARROW,
        "inside_cpr": {
            "bullish": [_clean(r) for r in i_bull],
            "bearish": [_clean(r) for r in i_bear],
        },
        "narrow_cpr": {
            "bullish": [_clean(r) for r in n_bull],
            "bearish": [_clean(r) for r in n_bear],
        },
    }
    with open("docs/results.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("[out] docs/results.json written")

    # ── Send email ────────────────────────────────────────────────
    send_watchlist_email(
        [r["sym"] for r in i_bull], [r["sym"] for r in i_bear],
        [r["sym"] for r in n_bull], [r["sym"] for r in n_bear],
        payload["for_date"], generated_at
    )


def _clean(r):
    return {
        "sym":       r["sym"],
        "setup":     r["setup"],
        "score":     r["score"],
        "width_pct": r["width_pct"],
        "atr_ratio": r["atr_ratio"],
        "pool":      r["pool"],
        "f1": r["filters"]["f1"]["score"], "f1_note": r["filters"]["f1"]["note"],
        "f2": r["filters"]["f2"]["score"], "f2_note": r["filters"]["f2"]["note"],
        "f3": r["filters"]["f3"]["score"], "f3_note": r["filters"]["f3"]["note"],
        "f4": r["filters"]["f4"]["score"], "f4_note": r["filters"]["f4"]["note"],
        "f5": r["filters"]["f5"]["score"], "f5_note": r["filters"]["f5"]["note"],
        "tv_link": f"https://www.tradingview.com/chart/?symbol=NSE%3A{r['sym']}"
    }


if __name__ == "__main__":
    main()
