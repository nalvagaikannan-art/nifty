# AI NIFTY Option Analyzer Pro

Live market analysis and AI decision support tool for NIFTY, BANKNIFTY, FINNIFTY options.

🚀 **Want to deploy this to GitHub + Render?** See [DEPLOYMENT.md](./DEPLOYMENT.md) for a full step-by-step guide.

## Installation

1. Clone the repository.
2. Create a virtual environment: `python -m venv venv`
3. Activate: `source venv/bin/activate` (Linux) or `venv\Scripts\activate` (Windows)
4. Install dependencies: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and add your AI API keys.
6. Run: `python run.py`
7. Open http://localhost:8000

## Features
- Live Spot and Option Chain from NSE
- PCR, Max Pain, OI Analysis
- Technical Indicators (S/R, RSI, MACD)
- AI-driven analysis with Gemini/OpenAI/DeepSeek
- Dark, responsive TradingView-style UI

## Disclaimer
This tool is for educational and analytical purposes only. It does not provide trading advice. Always do your own research before trading.

## ⚠️ Before Production / Real Trading Use
Data honesty model, current as of this version:
- **Angel One configured** (`ANGEL_ONE_*` env vars set) → spot, option chain, intraday OHLC,
  technicals (VWAP/EMA/RSI/MACD/ADX/Supertrend), futures premium, and Greeks all come from
  live data.
- **A specific provider unavailable** (Angel One not configured, an individual fetch fails,
  or insufficient history for real technicals) → the affected fields show
  `status: "unavailable"` / `N/A` in the UI and score as **neutral / 0 points** in the
  decision engine and Tamil explainer — never a fabricated 0% / flat / neutral reading.
  `data_completeness_pct` in the AI analysis result additionally **dampens signal confidence**
  when a meaningful chunk of the 20-condition scoring had no live data behind it, so a
  signal built on a handful of live indicators can't read as falsely high-confidence.
- Gift Nifty and global-market cues follow the same pattern: genuinely live or explicitly
  `unavailable`, never a made-up number standing in for missing data.

See [CODE_REVIEW.md](./CODE_REVIEW.md) and [FIXES_APPLIED.md](./FIXES_APPLIED.md) for the full
audit history and what's still open (market-regime-adaptive indicator weighting, exact-candle
horizon alignment for accuracy, net-of-cost accuracy is currently an estimate, not a live
brokerage calculation). Also configure `API_RATE_LIMIT_PER_MINUTE` and `CORS_ALLOWED_ORIGINS`
in `.env` before exposing this beyond localhost, and confirm the background history collector
(`HISTORY_COLLECTOR_INTERVAL_MINUTES`) and a persistent database (`DATABASE_URL` — see
`render.yaml`) are actually running/configured in your deployment before trusting the Accuracy
page's numbers, since both are required for that page to have real history to grade.
