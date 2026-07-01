#!/usr/bin/env python3
"""
momentum-stocks / scanner.py
────────────────────────────
Step 1 — Read Inside CPR stock list from cpr-bot exports
Step 2 — Apply 5-filter SMA scoring on each stock
Step 3 — Output filtered stock list as docs/results.json

No entry / SL / targets. Just the curated stock list.

Filters (each scored 0-2, max 10):
  F1  Inside CPR confirmed (from source list) + Narrow CPR width < 0.3%
  F2  ATR contraction: today ATR / 10d avg ATR < 0.75
  F3  CPR-to-R1 or CPR-to-S1 distance >= 1x ATR (room to move)
  F4  Daily SMA alignment: 20/50/200 all stacked bullish or bearish
  F5  1H SMA alignment + price above/below weekly CPR

Stocks scoring >= 6 appear in results.json.
Setup is labelled BULLISH or BEARISH based on F4 + F5 agreement.
"""

import json, datetime, sys
import yfinance as yf
import pandas as pd
import numpy as np
import urllib.request

# ── Source: cpr-bot publishes docs/cpr_list.json via GitHub Pages ────────────
# momentum-stocks reads that URL to get today's Inside CPR symbol list.
CPR_BOT_JSON_URL = (
    "https://gajapriyaannadurai.github.io/cpr-bot/cpr_list.json"
)

MIN_SCORE    = 6      # minimum score to appear in filtered list
NARROW_PCT   = 0.3    # CPR width % threshold for "Narrow CPR"
ATR_RATIO_TH = 0.75   # ATR contraction threshold


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def calc_cpr(h, l, c):
    pp    = (h + l + c) / 3
    bc    = (h + l) / 2
    tc    = 2 * pp - bc
    r1    = 2 * pp - l
    s1    = 2 * pp - h
    return {
        "upper": max(tc, bc),
        "lower": min(tc, bc),
        "width": abs(tc - bc),
        "r1": r1, "s1": s1
    }


def calc_atr(df, period=14):
    hi = df["High"].values
    lo = df["Low"].values
    cl = df["Close"].values
    trs = [max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
           for i in range(1, len(df))]
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))


def sma(series, n):
    if len(series) < n:
        return None
    return float(series.iloc[-n:].mean())


def weekly_cpr(df_daily):
    df = df_daily.copy()
    df.index = pd.to_datetime(df.index)
    wk = df.resample("W-FRI").agg({"High":"max","Low":"min","Close":"last"}).dropna()
    if len(wk) < 2:
        return None
    r = wk.iloc[-2]
    return calc_cpr(float(r["High"]), float(r["Low"]), float(r["Close"]))


# ═══════════════════════════════════════════════════════════════════
# FETCH INSIDE CPR LIST FROM CPR-BOT
# ═══════════════════════════════════════════════════════════════════

def fetch_inside_cpr_symbols():
    """Pull the Inside CPR symbol list from cpr-bot's published JSON."""
    try:
        with urllib.request.urlopen(CPR_BOT_JSON_URL, timeout=30) as r:
            data = json.loads(r.read().decode())
        # cpr-bot JSON has a "stocks" list — extract all symbols
        syms = []
        for key in ("bullish", "bearish", "neutral"):
            for s in data.get(key, []):
                sym = s.get("sym") or s.get("symbol")
                if sym and sym not in syms:
                    syms.append(sym)
        if syms:
            print(f"[fetch] Got {len(syms)} inside-CPR symbols from cpr-bot JSON")
            return syms, data.get("for_date", "")
    except Exception as e:
        print(f"[fetch] cpr-bot JSON failed: {e}")

    # Fallback: try raw GitHub URL directly
    try:
        js_url = "https://raw.githubusercontent.com/gajapriyaannadurai/cpr-bot/main/docs/cpr_list.json"
        with urllib.request.urlopen(js_url, timeout=30) as r:
            data = json.loads(r.read().decode())
        syms = [s["sym"] for s in data.get("stocks", [])]
        print(f"[fetch] Fallback JSON: {len(syms)} symbols")
        return syms, data.get("for_date", "")
    except Exception as e:
        print(f"[fetch] Fallback JS failed: {e}")
        return [], ""


# ═══════════════════════════════════════════════════════════════════
# 5-FILTER SCORER
# ═══════════════════════════════════════════════════════════════════

def score_stock(sym, df_d, df_1h):
    """
    Returns dict with score, setup (BULLISH/BEARISH/NEUTRAL), filter notes.
    Returns None if data is insufficient.
    """
    if df_d is None or len(df_d) < 30:
        return None

    today = df_d.iloc[-1]
    yday  = df_d.iloc[-2]
    close = float(today["Close"])

    curr_cpr = calc_cpr(float(today["High"]), float(today["Low"]), float(today["Close"]))
    prev_cpr = calc_cpr(float(yday["High"]),  float(yday["Low"]),  float(yday["Close"]))

    width_pct = (curr_cpr["width"] / close) * 100
    atr14     = calc_atr(df_d.iloc[-20:], 14)
    if atr14 is None:
        return None

    # ── F1: Inside CPR + Narrow ───────────────────────────────────
    is_inside = (curr_cpr["upper"] <= prev_cpr["upper"] and
                 curr_cpr["lower"] >= prev_cpr["lower"])
    is_narrow = width_pct < NARROW_PCT

    if not is_inside:
        return None          # not Inside CPR — skip entirely

    f1 = 2 if is_narrow else 1
    f1_note = f"{'Inside+Narrow' if is_narrow else 'Inside'} ({width_pct:.3f}%)"

    # ── F2: ATR Contraction ───────────────────────────────────────
    atr_list = []
    for i in range(2, min(13, len(df_d)-14)):
        a = calc_atr(df_d.iloc[-(i+14):-i], 14)
        if a:
            atr_list.append(a)
    if atr_list:
        avg_atr = float(np.mean(atr_list[:10]))
        ratio   = round(atr14 / avg_atr, 3) if avg_atr > 0 else 1.0
        f2      = 2 if ratio < ATR_RATIO_TH else (1 if ratio < 0.90 else 0)
        f2_note = f"ATR ratio {ratio}"
    else:
        ratio = None
        f2    = 1
        f2_note = "limited data"

    # ── F3: Distance to R1 / S1 ──────────────────────────────────
    dist_r1 = curr_cpr["r1"] - curr_cpr["upper"]
    dist_s1 = curr_cpr["lower"] - curr_cpr["s1"]
    f3      = 2 if (dist_r1 >= atr14 or dist_s1 >= atr14) else \
              1 if (dist_r1 >= 0.5*atr14 or dist_s1 >= 0.5*atr14) else 0
    f3_note = f"R1 gap {dist_r1:.1f} | S1 gap {dist_s1:.1f}"

    # ── F4: Daily SMA alignment ───────────────────────────────────
    cl  = df_d["Close"]
    s20 = sma(cl, 20);  s50 = sma(cl, 50);  s200 = sma(cl, 200)

    if s20 and s50 and s200:
        if s20 > s50 > s200:
            f4 = 2;  trend = "BULLISH";  f4_note = f"20>{int(s20)} 50>{int(s50)} 200>{int(s200)}"
        elif s20 < s50 < s200:
            f4 = 2;  trend = "BEARISH";  f4_note = f"20<{int(s20)} 50<{int(s50)} 200<{int(s200)}"
        else:
            f4 = 0;  trend = "NEUTRAL";  f4_note = "SMAs mixed"
    elif s20 and s50:
        trend   = "BULLISH" if s20 > s50 else "BEARISH"
        f4      = 1
        f4_note = f"20{'>'if s20>s50 else '<'}50 (no 200 data)"
    else:
        f4 = 0;  trend = "NEUTRAL";  f4_note = "insufficient SMA data"

    # ── F5: 1H SMA + weekly CPR ───────────────────────────────────
    htf = "UNKNOWN"
    f5  = 0
    f5_note = "no 1H data"

    if df_1h is not None and len(df_1h) >= 50:
        h1c   = df_1h["Close"]
        h1_20 = sma(h1c, 20);  h1_50 = sma(h1c, 50)
        h1_px = float(h1c.iloc[-1])
        wcpr  = weekly_cpr(df_d)
        wmid  = (wcpr["upper"] + wcpr["lower"]) / 2 if wcpr else None

        h1_bull = h1_20 and h1_50 and (h1_20 > h1_50)
        h1_bear = h1_20 and h1_50 and (h1_20 < h1_50)
        abv_w   = wmid and h1_px > wmid
        blw_w   = wmid and h1_px < wmid

        if h1_bull and abv_w:
            f5=2; htf="BULLISH"; f5_note="1H SMAs bull + above weekly CPR"
        elif h1_bear and blw_w:
            f5=2; htf="BEARISH"; f5_note="1H SMAs bear + below weekly CPR"
        elif h1_bull or abv_w:
            f5=1; htf="WEAK_BULL"; f5_note="1H partial bullish"
        elif h1_bear or blw_w:
            f5=1; htf="WEAK_BEAR"; f5_note="1H partial bearish"
        else:
            f5=0; htf="NEUTRAL"; f5_note="1H neutral"

    # ── Total score + setup ───────────────────────────────────────
    score = f1 + f2 + f3 + f4 + f5

    if trend == "BULLISH" and htf in ("BULLISH","WEAK_BULL","UNKNOWN"):
        setup = "BULLISH"
    elif trend == "BEARISH" and htf in ("BEARISH","WEAK_BEAR","UNKNOWN"):
        setup = "BEARISH"
    elif trend in ("BULLISH","BEARISH"):
        setup = trend
    else:
        setup = "NEUTRAL"

    return {
        "sym":       sym,
        "setup":     setup,
        "score":     score,
        "width_pct": round(width_pct, 3),
        "atr_ratio": ratio,
        "filters": {
            "f1": {"score": f1, "note": f1_note},
            "f2": {"score": f2, "note": f2_note},
            "f3": {"score": f3, "note": f3_note},
            "f4": {"score": f4, "note": f4_note},
            "f5": {"score": f5, "note": f5_note},
        }
    }


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    generated_at = ist.strftime("%Y-%m-%d %H:%M IST")
    print(f"[start] {generated_at}")

    # Step 1 — get Inside CPR symbols from cpr-bot
    symbols, for_date = fetch_inside_cpr_symbols()
    if not symbols:
        print("[abort] No inside CPR symbols found")
        _write_empty(generated_at, for_date)
        sys.exit(0)

    # Step 2 — download market data
    tickers = [f"{s}.NS" for s in symbols]
    print(f"[data] Downloading daily data for {len(tickers)} symbols...")
    try:
        df_daily = yf.download(
            tickers, period="14mo", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=120
        )
    except Exception as e:
        print(f"[data] Daily download error: {e}"); sys.exit(1)

    print(f"[data] Downloading 1H data...")
    try:
        df_1h = yf.download(
            tickers, period="60d", interval="1h", group_by="ticker",
            auto_adjust=False, progress=False, threads=True, timeout=120
        )
    except Exception as e:
        print(f"[data] 1H download error: {e}"); df_1h = None

    def get_df(bulk, ticker):
        try:
            if len(tickers) == 1:
                return bulk.dropna(how="all")
            if ticker not in bulk.columns.get_level_values(0):
                return None
            return bulk[ticker].dropna(how="all")
        except Exception:
            return None

    # Step 3 — score each symbol
    print("[score] Applying 5-filter scoring...")
    results = []
    for sym in symbols:
        ts  = f"{sym}.NS"
        d_d = get_df(df_daily, ts)
        d_h = get_df(df_1h, ts) if df_1h is not None else None
        try:
            r = score_stock(sym, d_d, d_h)
            if r and r["score"] >= MIN_SCORE:
                results.append(r)
        except Exception as ex:
            print(f"[score] {sym}: {ex}")

    results.sort(key=lambda x: x["score"], reverse=True)

    bullish = [r for r in results if r["setup"] == "BULLISH"]
    bearish = [r for r in results if r["setup"] == "BEARISH"]
    neutral = [r for r in results if r["setup"] == "NEUTRAL"]

    print(f"[done] Bullish: {len(bullish)} | Bearish: {len(bearish)} | Neutral: {len(neutral)}")

    # Step 4 — write results.json
    payload = {
        "generated_at":   generated_at,
        "for_date":       for_date,
        "source":         "cpr-bot inside CPR list",
        "min_score":      MIN_SCORE,
        "total_filtered": len(results),
        "bullish": [_clean(r) for r in bullish],
        "bearish": [_clean(r) for r in bearish],
        "neutral": [_clean(r) for r in neutral],
    }

    import os
    os.makedirs("docs", exist_ok=True)
    with open("docs/results.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("[out] docs/results.json written")


def _clean(r):
    return {
        "sym":       r["sym"],
        "setup":     r["setup"],
        "score":     r["score"],
        "width_pct": r["width_pct"],
        "atr_ratio": r["atr_ratio"],
        "f1": r["filters"]["f1"]["score"], "f1_note": r["filters"]["f1"]["note"],
        "f2": r["filters"]["f2"]["score"], "f2_note": r["filters"]["f2"]["note"],
        "f3": r["filters"]["f3"]["score"], "f3_note": r["filters"]["f3"]["note"],
        "f4": r["filters"]["f4"]["score"], "f4_note": r["filters"]["f4"]["note"],
        "f5": r["filters"]["f5"]["score"], "f5_note": r["filters"]["f5"]["note"],
        "tv_link": f"https://www.tradingview.com/chart/?symbol=NSE%3A{r['sym']}"
    }


def _write_empty(generated_at, for_date):
    import os
    os.makedirs("docs", exist_ok=True)
    payload = {
        "generated_at": generated_at, "for_date": for_date,
        "total_filtered": 0, "bullish": [], "bearish": [], "neutral": []
    }
    with open("docs/results.json", "w") as f:
        json.dump(payload, f, indent=2)


if __name__ == "__main__":
    main()
