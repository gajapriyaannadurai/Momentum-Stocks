# momentum-stocks

Reads the Inside CPR stock list from **cpr-bot**, applies 5-filter SMA scoring,
and publishes a curated watchlist to GitHub Pages for tradingwithgp.com.

## What it does

1. Fetches Inside CPR symbols from `cpr-bot` (runs at 4:15 PM IST, 15 min after cpr-bot)
2. Downloads daily + 1H market data via yfinance
3. Scores each stock on 5 filters (max 10 points)
4. Writes `docs/results.json` → served via GitHub Pages
5. `docs/index.html` renders the scanner page (embed via iframe on tradingwithgp.com)

## Filters

| # | Filter | Condition | Max pts |
|---|--------|-----------|---------|
| F1 | Inside CPR + Narrow | Width < 0.3% of price | 2 |
| F2 | ATR Contraction | Today ATR / 10d avg < 0.75 | 2 |
| F3 | R1 / S1 Room | Distance ≥ 1× ATR | 2 |
| F4 | Daily SMA Stack | 20/50/200 all aligned | 2 |
| F5 | 1H Bias | 1H SMAs + price vs weekly CPR | 2 |

Stocks scoring **≥ 6/10** appear in the filtered list.

## Setup

1. Create new GitHub repo: `momentum-stocks`
2. Push all files
3. Enable GitHub Pages: Settings → Pages → Source: `docs/` folder
4. Your JSON is live at: `https://gajapriyaannadurai.github.io/momentum-stocks/results.json`
5. Scanner page at: `https://gajapriyaannadurai.github.io/momentum-stocks/`

## Embed on tradingwithgp.com

Paste this iframe wherever you want the scanner to appear:

```html
<iframe
  src="https://gajapriyaannadurai.github.io/momentum-stocks/?token=TOKEN_HERE"
  width="100%" height="700"
  frameborder="0" style="border-radius:12px;">
</iframe>
```

For paid members, append `?token=XXXX` to the iframe src using the token generator.

## Token generator

```bash
python token_generator.py --days 30
```

Paste the output URL into your Graphy members-only page as a button.
Free users (no token) see top 3 stocks blurred. Paid users see all.
