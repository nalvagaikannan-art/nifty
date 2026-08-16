# AI NIFTY Option Analyzer Pro — Code Review & Production Readiness

Full file-by-file audit. Items marked **✅ FIXED** were corrected directly in this
codebase. Items marked **🔲 OPEN** are documented here but require product
decisions, paid data sources, or infra you'll need to provide — they are not
things that can be silently patched.

---

## 🔴 Critical Bugs

| # | File | Issue | Status |
|---|------|-------|--------|
| 1 | `.env` shipped in the zip | Real config file (even with placeholder values) should never ship as `.env` — renamed to `.env.example`. `.gitignore` already excluded `.env` from git, but the zip export bypassed that. | ✅ FIXED |
| 2 | `app/services/ai_engine.py` | Used `openai.ChatCompletion.create(...)` and `openai.api_key = ...` — this is the **v0.x OpenAI SDK API**, but `requirements.txt` pins `openai==1.3.0` (v1.x), which **removed this interface entirely**. Every OpenAI/DeepSeek call would raise `AttributeError` at runtime. | ✅ FIXED — rewritten with `AsyncOpenAI` client |
| 3 | `app/main.py` | `from app.api.routes import ..., settings` silently shadowed the earlier `from app.config import settings` import. Any future code in `main.py` referencing `settings.xxx` (config) would instead hit the routes module and crash. | ✅ FIXED — aliased import |
| 4 | `app/services/data_fetcher.py` | No NSE session/cookie bootstrap. NSE's `/api/*` endpoints reject requests without cookies obtained from first loading `nseindia.com` — every single call would return 401/403 in production. | ✅ FIXED — session bootstrap + auto-refresh on 401/403 |

---

## 🟠 Logic Errors

| # | File | Issue | Status |
|---|------|-------|--------|
| 1 | `technical_indicators.py::macd()` | Signal line was computed as a 9-period EMA of **raw prices**, not of the **MACD line** itself. This produces a signal line mathematically unrelated to MACD — any crossover logic built on it (`macd > signal` = bullish) would be meaningless. | ✅ FIXED |
| 2 | `option_analyzer.py::compute_max_pain()` | Was actually computing "the strike with the highest combined OI," a completely different concept from Max Pain, and labeling it Max Pain. See **False Signal Issues** below. | ✅ FIXED — proper payout-minimization formula |
| 3 | `risk_manager.py::RiskManager` | Fully implemented but **never instantiated or called anywhere** in the original code — dead code, `risk` never appeared in any API response. | ✅ FIXED — wired into `MarketAnalyzer.get_full_market_overview()` |
| 4 | `models.py` (`MarketData`, `OptionData`, `AnalysisResult`) | Tables were created on startup (`Base.metadata.create_all`) but **no code anywhere wrote a row to them**. History, OI-change-over-time, and past-analysis lookups would always be empty. | ✅ FIXED — new `history_service.py`, wired into dashboard + analysis routes |
| 5 | `data_fetcher.py::get_option_chain()` | Calls `option-chain-indices` for **both** fetching the expiry list *and* fetching the chain for a specific expiry — same endpoint, different intent, works by accident on NSE's real API shape but is fragile if NSE changes response structure. | 🔲 OPEN — flag if NSE API contract changes |
| 6 | `market.py::get_option_chain` route | `expiry: str = None` — a required-typed parameter with a `None` default; FastAPI tolerates it but it generates an incorrect (non-nullable) OpenAPI schema. | ✅ FIXED — `Optional[str] = Query(None)` |

---

## ⚠️ False Signal Issues

These don't crash the app — they silently produce **wrong trading signals**, which is worse.

1. **Max Pain was wrong** (see Logic Errors #2). The old code returned the strike with peak
   OI, which analysts often *mistake* for max pain but is a different signal (peak OI
   strikes indicate where big positions are, not where option writers profit most). Feeding
   the wrong number into the AI prompt as "Max Pain: X" would bias every AI recommendation.
2. **`trend` was always implicitly `"sideways"`** everywhere it mattered (`RiskManager`,
   AI prompt) because no historical price series is fetched — `technical_indicators.py`'s
   `trend_detection()`, `rsi()`, and `macd()` all require a price history list that nothing
   in the codebase ever populates. **This is the single biggest gap for signal quality.**
   🔲 **OPEN — requires historical OHLC ingestion (see Production Checklist).**
3. **Support/Resistance is a naive ±2%/±5% band around spot**, not derived from actual
   pivot points, volume profile, or option OI walls. It will look plausible but is not a
   real technical level. 🔲 OPEN — cosmetic/placeholder by design, should be replaced with
   real pivot calculation from historical highs/lows once OHLC data is available.
4. **`get_market_breadth()` returns hardcoded `{"advances": 30, "declines": 20}`** — this is
   explicitly a placeholder in the original code and was never wired to a real NSE breadth
   endpoint. Any AI reasoning using "market breadth" is currently reasoning over fake data.
   🔲 OPEN — needs a real NSE breadth endpoint or third-party source.
5. **`futures_premium` is hardcoded to `0.0`** — same issue, silently wrong instead of
   fetched. 🔲 OPEN.

**Recommendation:** until items 2–5 are addressed, the AI-generated `suggestion` field
should be treated as **directional context only, not a signal to act on** — the underlying
inputs (trend, breadth, futures premium) are partly fabricated placeholders, not live data.

---

## 🚀 Performance Problems

| # | Issue | Status |
|---|-------|--------|
| 1 | `DataFetcher` created a fresh `httpx.AsyncClient` per request (via FastAPI `Depends`), with no shared connection pool and no session reuse — every request re-negotiated TLS and would have been blocked without cookies anyway. | ✅ Partially fixed — session cookie reuse now works within a client's lifetime; a fully shared/pooled client across requests is a further improvement (see checklist). |
| 2 | `dashboard_summary()` calls `get_full_market_overview()` **sequentially** for NIFTY, BANKNIFTY, FINNIFTY — 3x the latency of running them concurrently. | 🔲 OPEN — switch to `asyncio.gather()` |
| 3 | `async_cache` decorator keys on `f"{func.__name__}:{args}:{kwargs}"` — `args` includes `self`, so the cache is *technically* per-instance-safe, but the cache dict itself is a **module-level global with no eviction**, so it grows unbounded over a long-running process (slow memory leak). | 🔲 OPEN — needs TTL-based eviction or an LRU cap |
| 4 | No connection pooling / retry backoff tuning for NSE (`httpx.AsyncClient(timeout=10.0)` with no limits config) — under load this can exhaust file descriptors. | 🔲 OPEN |

---

## 🔒 Security Vulnerabilities

| # | Issue | Status |
|---|-------|--------|
| 1 | `.env` shipped with the deliverable | ✅ FIXED (see Critical Bugs #1) |
| 2 | `CORSMiddleware(allow_origins=["*"], allow_credentials=True)` — this combination is invalid per the CORS spec (browsers reject wildcard origin + credentials) and, if it ever did work, would let **any website** read authenticated responses. | ✅ FIXED — explicit origin allowlist via `CORS_ALLOWED_ORIGINS` env var, credentials disabled |
| 3 | API keys logged in plaintext risk: `logger.error(f"AI API error: {e}")` could leak key material if an SDK ever includes it in exception text. | 🔲 OPEN — add log scrubbing/redaction middleware before production |
| 4 | No rate limiting / auth on any `/api/*` route — anyone who can reach the server can trigger unlimited NSE scraping and AI provider calls (cost + ban risk). | 🔲 OPEN — add API key auth or reverse-proxy rate limiting before public exposure |
| 5 | SQLite with no encryption at rest for `analyzer.db` — fine for local/single-user use, not for multi-tenant production. | 🔲 OPEN — migrate to Postgres for real production use |

---

## 📈 AI Analysis Improvements

| # | Issue | Status |
|---|-------|--------|
| 1 | No fallback if the configured AI provider fails (wrong key, outage, rate limit) — request would just 500. | ✅ FIXED — `AIEngine` now tries every provider with a configured key, in priority order, before failing |
| 2 | Gemini model pinned to `"gemini-pro"`, an older model name that may not be enabled on newer API keys. | ✅ FIXED — tries `gemini-1.5-flash` → `gemini-1.5-pro` → `gemini-pro` in order |
| 3 | `_parse_ai_response()` failed on responses wrapped in markdown code fences (```json ... ```), which is a common LLM output format. | ✅ FIXED — fences are stripped before JSON extraction |
| 4 | The AI prompt is fed **placeholder** breadth, futures premium, and trend data (see False Signal Issues) — no amount of prompt engineering fixes bad inputs. | 🔲 OPEN — depends on the data-ingestion fixes above |
| 5 | No validation that the AI's `suggestion` field is actually one of the 6 allowed values, or that `confidence` is 0–100 — a malformed AI response is returned as-is to the frontend. | 🔲 OPEN — validate against `schemas.AIAnalysisResponse` before returning |
| 6 | No historical tracking of AI suggestion accuracy (was "BUY CALL" actually followed by a rally?) — `history_service.py` now saves every AI result, which is the prerequisite for building this, but the accuracy-scoring logic itself doesn't exist yet. | 🔲 OPEN |

---

## 📋 Production Readiness Checklist

**Before any real/paper trading use:**
- [ ] Implement historical OHLC ingestion (NSE historical API or a paid data vendor) —
      unlocks real trend/RSI/MACD/support-resistance instead of placeholders
- [ ] Replace hardcoded `get_market_breadth()` and `futures_premium` with real data sources
- [ ] Add `asyncio.gather()` for the 3-symbol dashboard summary fetch
- [ ] Add bounded/TTL eviction to the in-memory cache (or switch to Redis for multi-worker deployments)
- [ ] Add rate limiting and/or API-key auth in front of `/api/*`
- [ ] Move from SQLite to Postgres if more than one process/worker will run concurrently
- [ ] Validate AI responses against `AIAnalysisResponse` schema before returning to clients
- [ ] Add structured logging with request IDs; scrub secrets from log output
- [ ] Add integration tests that mock NSE/AI responses (current tests hit live NSE/AI — flaky and slow)
- [ ] Set up monitoring/alerting for NSE block rate (401/403 frequency) — an early signal your IP is getting rate-limited
- [ ] Decide on and document a real fallback data source if NSE blocks the server outright (there is currently no secondary market-data provider — NSE being blocked means total data outage)
- [ ] Load test the AI endpoints — each `/api/analysis/ai/{symbol}` call can now make up to 3 sequential provider calls if earlier ones fail, which adds latency

---

## Summary of Changes Made in This Pass

- `.env` → `.env.example` (no real/placeholder secrets shipped in a file literally named `.env`)
- `app/services/data_fetcher.py` — NSE session bootstrap, auto-refresh on 401/403, retry with exponential backoff on transport errors
- `app/services/ai_engine.py` — rewritten for OpenAI v1 SDK, added Gemini model fallback list, added cross-provider fallback, added markdown-fence-tolerant JSON parsing
- `app/services/option_analyzer.py` — fixed Max Pain to use the correct payout-minimization formula
- `app/services/technical_indicators.py` — fixed MACD signal line calculation
- `app/services/market_analyzer.py` — wired in `RiskManager`, added `trend` field
- `app/services/history_service.py` — **new file**, actually persists market/analysis history to the tables that already existed but were never written to
- `app/api/routes/dashboard.py`, `analysis.py`, `market.py` — wired in history saving, proper HTTP error codes, fixed `Optional` typing
- `app/main.py` — fixed `settings` import name collision, fixed CORS security config

---

## Round 2 — Answers to Specific Stability/Correctness Questions

### 1. Live NSE Data Fetching நிலையானதா? (Is it stable?)
**Was not stable — now substantially more so, but still has one open dependency.**
- **✅ FIXED — critical parsing bug:** `get_option_chain()` was reading a top-level `data`
  key that NSE's real `option-chain-indices` response does not have (the actual shape is
  `{"records": {"data": [...], "expiryDates": [...]}, "filtered": {...}}`). This meant the
  expiry list was **always empty**, so every option-chain fetch either raised
  `"No expiries available"` or — if that exception were ever swallowed — silently fed an
  empty chain into PCR/Max Pain (see Q3).
- **✅ FIXED:** added NSE session/cookie bootstrap (required before `/api/*` calls succeed),
  auto-refresh on 401/403, and retry-with-backoff on 429/5xx and transport errors.
- **✅ FIXED (perf + block-risk):** `DataFetcher` was being constructed fresh and destroyed
  on every single HTTP request (see Q4) — meaning the session cookie was re-fetched every
  request too, multiplying NSE traffic. Now a single shared instance lives for the app's
  process lifetime.
- **🔲 Still open:** NSE actively fingerprints and rate-limits scraping; no code change can
  fully guarantee it won't block a given IP/session. There is currently no secondary/fallback
  data provider — if NSE blocks the server, the app has a **total data outage** until it's
  unblocked. Treat this as a hard dependency risk, not something "fixed" by retries alone.

### 2. AI Analysis Logic தவறான Signal தருகிறதா? (Is it giving wrong signals?)
**Yes, it was — two concrete bugs plus a data-quality caveat.**
- **✅ FIXED:** Max Pain was computing "strike with highest OI" and labeling it Max Pain —
  a different, often-confused concept. The AI prompt was being fed the wrong number under
  the right name. See Q3 for the fix.
- **✅ FIXED:** MACD signal line was an EMA of raw price, not of the MACD line — histogram
  and any bullish/bearish crossover reasoning built on it was mathematically meaningless.
- **✅ FIXED:** market breadth (`advances`/`declines`) was hardcoded to `{30, 20, 0}` regardless
  of actual market conditions — every AI call reasoned over fake breadth data. Now reads
  NSE's real `advance` block.
- **🔲 Still open:** `trend` is still a hardcoded `"sideways"` because no historical OHLC
  series is fetched anywhere — `rsi()`, `trend_detection()`, and proper support/resistance
  all require it and currently can't run. **Treat the AI's `market_trend` and `suggestion`
  fields as provisional until this is implemented** — see Production Checklist.

### 3. PCR / Max Pain / OI கணக்கீடுகள் சரியா? (Are the calculations correct?)
- **PCR:** formula (`sum(PE OI) / sum(CE OI)`) was already correct — no bug found. Its
  *input data* was broken though (see Q1 parsing bug), so in practice it may have been
  computing over an empty/wrong chain. Fixed by the parsing fix.
- **Max Pain:** ✅ FIXED — was not computing max pain at all (see Q2). Now uses the standard
  formula: for each candidate strike, sum call-writer and put-writer losses across all
  strikes, and pick the strike that minimizes total payout.
- **OI Change:** still a hardcoded placeholder (`{"ce_change": 0, "pe_change": 0}`) — this
  is correct behavior *given* no historical OI snapshot exists to diff against.
  `history_service.save_option_chain_snapshot()` now exists to start building that history,
  but the diffing logic itself (compare today's snapshot to N minutes/days ago) is **not
  yet implemented** — 🔲 open item.

### 4. Memory Leak அல்லது Performance பிரச்சனை உள்ளதா?
**Yes — two real ones, both fixed.**
- **✅ FIXED — cache was completely non-functional:** `@async_cache` keyed on `str(args)`,
  which includes `self`. Since a new `DataFetcher`/`MarketAnalyzer` was created per HTTP
  request (see below), `self`'s default repr (with a memory address) differed every time —
  **the cache never hit, ever**, defeating its entire purpose and sending more requests to
  NSE than necessary.
- **✅ FIXED — unbounded cache growth:** the cache dict had no eviction at all; every unique
  call added an entry forever. Added TTL-based sweeping plus a hard size cap (500 entries).
- **✅ FIXED — wasteful per-request object churn:** `DataFetcher`/`MarketAnalyzer` were
  constructed and `.close()`d on every single request via FastAPI `Depends`. Now created
  once at app startup and shared via `app.state` (see `app/main.py` lifespan + `app/api/deps.py`).
- **✅ FIXED — sequential dashboard fetch:** `/api/dashboard/summary` fetched NIFTY,
  BANKNIFTY, FINNIFTY one after another (3x the latency of the slowest one). Now uses
  `asyncio.gather()`.

### 5. Exception Handling முழுமையா? (Is it complete?)
**Was partial — now has a consistent, layered structure.**
- **✅ FIXED:** added global FastAPI exception handlers for `MarketDataError` (→ 502),
  `AIProviderError` (→ 503), and any unhandled `Exception` (→ 500 with a generic message,
  full traceback logged server-side only — previously an unhandled exception could leak
  a raw stack trace to the client via FastAPI's default debug behavior).
- **✅ FIXED:** `/api/market/*` and `/api/analysis/ai/*` routes now explicitly catch
  `MarketDataError`/`AIProviderError` and return meaningful HTTP status codes instead of
  a generic 500.
- **✅ FIXED:** removed a bare `except:` in `get_market_breadth()` (bare except also catches
  `KeyboardInterrupt`/`asyncio.CancelledError`, which should propagate, not be swallowed).
- **🔲 Still open:** no retry/circuit-breaker at the *application* level for "AI provider
  keeps failing" beyond the per-request fallback chain — repeated failures aren't tracked
  across requests (e.g. to temporarily stop trying a provider that's clearly down).

### 6. Market Closed நேரங்களில் App சரியாக செயல்படுகிறதா?
**Was not handled at all before — now surfaced explicitly.**
- **✅ FIXED:** `get_spot()` now reads NSE's own `marketStatus` field when present, and
  falls back to an IST 09:15–15:30 Mon–Fri heuristic when it's not. Every spot response now
  includes `"market_open": true/false` and `"market_status_source"` so callers (and the
  frontend) can show a "Market Closed — showing last close" indicator instead of presenting
  a stale price as if it were live.
- **🔲 Still open:** the IST-hours fallback does **not** account for NSE holidays (no simple
  holiday-calendar endpoint is wired in) — on a market holiday during 09:15–15:30 IST, the
  fallback would incorrectly estimate the market as open if NSE's own status field is also
  unavailable for some reason. Prefer `market_status_source == "nse"` whenever present.
- **🔲 Still open:** no explicit handling of the pre-open (09:00–09:15) or post-close
  settlement windows, where NSE data can be present but not representative of continuous trading.

### 7. Frontend ↔ Backend API Integration முழுமையா?
**Verified route-by-route — all wired correctly, no mismatches found.**

| Frontend call | Backend route | Status |
|---|---|---|
| `GET /api/dashboard/summary` | `dashboard.py::dashboard_summary` | ✅ matches |
| `GET /api/market/spot/NIFTY` | `market.py::get_spot` | ✅ matches |
| `GET /api/options/pcr/NIFTY` | `options.py::get_pcr` | ✅ matches |
| `GET /api/analysis/ai/NIFTY` | `analysis.py::ai_analysis` | ✅ matches |
| `GET /api/settings/config` | `settings.py::get_config` | ✅ matches |

No broken endpoints found. Two integration gaps worth noting:
- **🔲 Open:** `websocket.js` is an empty placeholder — the dashboard actually polls every
  10 seconds via `setInterval`, not real-time push. Fine for a demo, not "live" in the
  strict sense: NSE spot cache TTL is 10s, so polling faster than that gains nothing anyway.
- **🔲 Open:** the frontend never reads the new `market_open`/`risk` fields added in this
  pass — the dashboard cards don't yet show a "Market Closed" badge or risk level, even
  though the backend now provides them. This is a small frontend follow-up, not a backend bug.

**Note on testing in this environment:** this sandbox has no network access to install
`httpx`/`fastapi`/`pydantic` or reach NSE, so these fixes were verified by (a) full-repo
`py_compile` syntax checks, (b) isolated logic tests of pure-Python functions like Max Pain
against hand-built sample data, and (c) careful manual trace of the NSE response shapes
against NSE's documented/observed API structure. Run `pytest` locally with real network
access before deploying to catch anything a static review can't.

---

## Round 3 — Remaining Open Items Addressed

Working from the "still open" list, the following were fixed with real code (not just notes):

| # | Item | What changed |
|---|------|--------------|
| 2 | Breadth data fed to AI without a reliability check | `ai_engine.py`'s prompt now appends a "⚠️ breadth data unavailable this cycle" note whenever `breadth.source != "nse"`, so the AI is told explicitly not to weight unreliable breadth heavily. Same pattern added for stale/closed-market spot prices. |
| 6 | *(Re-checked, already fixed in the prior pass — `dashboard.py` already uses `asyncio.gather()`. Also found and fixed the same sequential-fetch issue inside `MarketAnalyzer.get_full_market_overview()` itself — spot/option-chain/VIX/breadth are now fetched concurrently too, not just the 3-symbol dashboard loop.)* |
| 7/8 | No connection pool limits on the shared `httpx.AsyncClient` | Added explicit `httpx.Limits(max_connections=20, max_keepalive_connections=10)` — bounds resource usage under load instead of relying on httpx's implicit defaults. |
| 9 | API keys could leak into logs in plaintext | Added `SecretRedactionFilter` (`app/utils/logging.py`) — redacts the exact configured key values plus generic key-shaped patterns (`sk-...`, `AIza...`, `Bearer ...`) from every log record before it's written, on both the file and console handlers. |
| 10 | No rate limiting on `/api/*` | Added `app/middleware/rate_limit.py` — a sliding-window per-IP limiter (default 60 req/min, tunable via `API_RATE_LIMIT_PER_MINUTE`), returns HTTP 429 with `Retry-After` when exceeded. **Caveat: in-memory, single-process only** — see note in the file for why a multi-worker deployment needs Redis instead. |
| 12 | AI response fields not validated (out-of-range confidence, invalid suggestion strings) | `schemas.AIAnalysisResponse` now has real constraints (`Literal` for `market_trend`/`risk`/`suggestion`, `confidence` bounded 0–100). `AIEngine.analyze_market()` validates every parsed response against it — a malformed response now counts as that provider "failing" and falls through to the next provider in the chain, rather than reaching the frontend. |
| 16 | IST-hours fallback didn't know about NSE holidays | Added a verified 2026 NSE holiday list (cross-checked against NSE's published calendar) to `is_market_hours_ist()` — weekday holidays now correctly report the market as closed even during 09:15–15:30 IST. Tested against Republic Day, a normal trading day, pre-market hours, and a weekend — all correct. Note: this list needs a manual refresh every calendar year and doesn't know about ad-hoc circulars (e.g. Muhurat trading special sessions). |

## Round 4

- **Holiday calendar restructured** to `NSE_HOLIDAYS[year]` — 2027 is **not yet published by NSE** (verified via search; third-party "Indian holidays 2027" lists are generic, not NSE's trading calendar), so it's intentionally left blank with a runtime warning + docstring telling you to add it once NSE's circular drops (usually Nov/Dec 2026). Fabricating dates would be worse than an honest gap.
- **Historical OHLC ingestion implemented** (`DataFetcher.get_historical_prices()`, NSE `/historicalOR/indicesHistory`) — real `trend`, `rsi`, `macd`, and pivot-based support/resistance now compute from actual daily closes instead of the old hardcoded placeholders. **Caveat:** the response envelope couldn't be verified against a live NSE call in this sandbox (no network access), so parsing is defensive — on any shape mismatch it raises a clear error, `MarketAnalyzer` catches it and falls back to the old placeholder values instead of crashing. Verify against one real response and adjust `get_historical_prices()` if needed.
- `overview["technical_data_source"]` now tells you whether trend/RSI/MACD came from `"historical_ohlc"` (real) or `"placeholder_no_history"` (fallback) for any given request.

## Round 5

Addressed from the two "genuinely open" lists (accuracy tracking + infra):

| # | Item | What changed |
|---|------|--------------|
| §29/#13 | Nothing compared a past signal's action (CALL BUY/PUT BUY) against what price actually did | **New** `app/services/signal_accuracy.py` — grades every saved `AnalysisResult` at 5/10/15/30/60-minute horizons against `MarketData` price history already being collected. Reports overall / CALL BUY / PUT BUY accuracy, broken down **by market regime** (`volatility_regime`) and **by confidence range** (<50, 50–70, 70–85, 85+), with correct/wrong/flat/pending states — matches spec §29's field list except CALL SELL/PUT SELL (decision_engine doesn't emit sell signals yet — spec §14 requires IV/liquidity/margin data this app doesn't have, so those two show `insufficient_data` with an explicit reason rather than fabricated numbers). |
| — | `accuracy_engine.py` (per-indicator accuracy) was fully written in an earlier round but **never wired to any route** — dead code, invisible to users | New `app/api/routes/accuracy.py`: `GET /api/accuracy/indicators/{symbol}` (existing engine) + `GET /api/accuracy/signals/{symbol}` (new engine above). Registered in `main.py`. |
| #17 | Frontend never showed `market_open`/`market_status_source` even though the backend sent it | Added a `● LIVE` / `● MARKET CLOSED` badge next to the spot price on `dashboard.html` and `terminal.html`. `risk` was already rendered (dashboard.html's signal card) — verified, no change needed there. `live.html`/`option-view.html` spot rendering not yet touched — same pattern can be copied in if needed. |
| #7 | Cache + rate limiter are in-memory/per-process only | `app/utils/cache.py` and `app/middleware/rate_limit.py` now support an **optional** Redis backend — set `REDIS_URL` (and `pip install redis`) to share both across multiple workers/instances; unset (default) keeps the existing in-memory behavior unchanged. Redis errors at request time fall back to in-memory automatically rather than hard-failing. |
| #11 | SQLite hardcoded, no path to Postgres | `app/config.py` / `app/database.py` now read an optional `DATABASE_URL` (e.g. `postgresql+asyncpg://...`); unset keeps the existing SQLite file. `pip install asyncpg` needed only if you set it. |

New UI page: **`/accuracy`** — shows the signal-accuracy summary (overall/CALL BUY/PUT BUY, by-horizon table, by-regime, by-confidence-range) and the per-indicator table, both symbol/day-range selectable. Linked from the nav bar.

**Note on testing in this environment:** same constraint as prior rounds — no network access here to run against live NSE/Angel One/Redis/Postgres. All changes verified via full-repo `py_compile`, manual trace of the accuracy math against hand-built sample timelines, and HTML balance checks (div/block counts). Run against real data before relying on the accuracy numbers for anything.

## Round 6 — Monitoring + Testing (CODE_REVIEW.md #19/#20/#18)

**Monitoring (#19/#20):** `app/utils/health_metrics.py` — a rolling per-source (NSE / Angel One) call-outcome tracker (ok/blocked/transient_error/other_error over the last ~50 calls), wired into `data_fetcher.py`'s `_get()` and the Angel One wrapper methods. Exposed at `GET /api/settings/health` and a small live table on the Settings page. Logs a warning — and optionally POSTs to `ALERT_WEBHOOK_URL` if configured — when a source's block rate crosses 30% over its last 20 calls (15-minute cooldown between repeat alerts for the same source). Also finished the `market_open` badge rollout to `live.html` (`dashboard.html`/`terminal.html` got it in Round 5).

**Testing (#18):** `tests/test_data_fetcher.py` and `tests/test_analyzer.py` previously instantiated real `DataFetcher`/`MarketAnalyzer` objects and called live NSE/AI endpoints directly — slow, flaky (network-dependent), and blocked entirely in any sandboxed CI without egress. Replaced with:

| File | What it covers | Network? |
|---|---|---|
| `tests/test_technical_indicators.py` | trend/RSI/MACD/compute_all/compute_from_ohlc (Wilder ADX/ATR/Supertrend/VWAP)/support-resistance, using hand-built deterministic price series | None — pure functions |
| `tests/test_option_analyzer.py` | PCR, Max Pain (locks in the Round-1 fix), OI summary/change, volume PCR, strike candidate picking, using hand-built option-chain fixtures | None — pure functions |
| `tests/test_decision_engine.py` | bullish/bearish/sideways/market-closed scenarios (spec §43), confidence bounds, VIX-doesn't-flip-direction (spec §11), scenarios/invalidation always present (spec §18/§25), 20-reasons contract check | None — pure function over a crafted `market_data` dict |
| `tests/test_data_fetcher.py` (rewritten) | NSE response parsing for spot/option-chain/VIX/breadth/FII-DII, including regression tests for two real bugs fixed in earlier rounds (option-chain `records` shape, "India VIX" partial-match) | **Mocked** — `DataFetcher._get` is patched; Angel One is naturally unconfigured in a bare test env so its wrappers no-op automatically |
| `tests/test_analyzer.py` (rewritten) | Full `get_full_market_overview` pipeline: placeholder vs real technicals depending on history length, market-closed propagation, PCR/OI-summary correctness, graceful degradation when option-chain fetch fails | **Mocked** — `MarketAnalyzer(fetcher=...)` takes an injected `AsyncMock` fetcher (constructor already supported this); DB/global-market calls patched at the module level |
| `tests/conftest.py` (new) | Autouse fixture resetting `app.utils.cache._cache` before/after every test | — |
| `tests/test_ai_engine.py` | (unchanged — was already network-free, config-only) | None |

Also added: `requirements-dev.txt` (pytest/pytest-asyncio/pytest-mock weren't declared anywhere before this — CI or a fresh clone had no way to know what to install to run `tests/`) and `pytest.ini` (`asyncio_mode = auto`).

**Important caveat on verification:** this sandbox has no network access and only `numpy`/`pandas` pre-installed — `fastapi`, `pydantic`, `sqlalchemy`, `tenacity`, `curl_cffi`, `pytest` etc. are not available here, so `data_fetcher.py`/`market_analyzer.py`/anything importing `app.config` **cannot actually be executed** in this environment. `test_technical_indicators.py`, `test_option_analyzer.py`, and `test_decision_engine.py` (pure functions, only need numpy/pandas) *were* executed here and every assertion verified against real output. `test_data_fetcher.py` and `test_analyzer.py` were written from a careful line-by-line read of the exact functions they test (fixtures match real NSE response shapes and real method signatures) but **could not be executed in this sandbox** — run `pip install -r requirements.txt -r requirements-dev.txt && pytest` in an environment with those packages before trusting them fully.

## Round 7 — Zerodha Kite Connect provider (CODE_REVIEW.md #15)

Structural addition only — **no Zerodha API keys were available while writing this**, so nothing below has been exercised against a live Kite account. Consistent with `angel_one.py`'s own existing caution notes about unverified field names, `zerodha.py` carries the same disclaimers throughout.

| What | Where |
|---|---|
| `KiteSession` class — `is_configured`, `get_ltp`, `get_india_vix`, `get_candle_data`, `get_option_chain` (instrument-master + batched quote(), same approach `angel_one.py` uses since Kite has no single option-chain endpoint either), plus `login_url()`/`generate_session()` helpers for the one-time daily OAuth login Kite requires (no headless TOTP login like Angel One — needs an external script) | `app/services/zerodha.py` (new) |
| Fallback wiring: **Angel One → Zerodha → NSE** for `get_spot`, `get_option_chain`, `get_historical_prices`, and `get_intraday_ohlc` (multi-timeframe 5m/15m/1h) | `app/services/data_fetcher.py` |
| Fixed a related gap while wiring this in: `market_analyzer.py` and `ai_engine.py` only recognized `data_source == "angel_one_intraday"` as "real" intraday data — a Zerodha-sourced 5-min frame would have been silently ignored (treated as unavailable) even though it's just as real. Both now check `in ("angel_one_intraday", "zerodha_intraday")`. | `app/services/market_analyzer.py`, `app/services/ai_engine.py` |
| `KITE_API_KEY` / `KITE_API_SECRET` / `KITE_ACCESS_TOKEN` settings, `.env.example` entries with setup notes, `kiteconnect` as an optional requirements-dev-style dependency | `app/config.py`, `.env.example`, `requirements.txt` |
| Read-only Zerodha status card on the Settings page (no credential form — Kite's login is a browser-redirect OAuth flow, not a saved username/password, so there's nothing to type in-app) | `app/templates/settings.html`, `GET /api/settings/config` |
| `health_metrics` already generic across sources — Zerodha calls show up in the existing `/api/settings/health` table automatically, no extra wiring needed | — |

**Known gaps, called out in the module itself:** Kite's `quote()` endpoint doesn't return implied volatility or a same-day OI-change baseline the way NSE/Angel do — both are set to 0/"not available" on zerodha-sourced option chains rather than a fabricated number (spec §42). This app's own OI-change-over-time tracking (`history_service.get_oi_change_since`, used for `oi_change_tracked`) is unaffected since it diffs its own saved snapshots regardless of which provider supplied the raw chain.

**Not done in this pass (would need a live account to verify safely):** actually testing login/session against Kite, confirming the index/VIX instrument tokens are current, and building the small daily-login script the module docstring describes (`login_url()` → browser → `generate_session()` → store as `KITE_ACCESS_TOKEN`). Do all of this before trusting Zerodha as a real fallback in production — until then, it silently no-ops (`is_configured` is `False`) and every existing deployment's behavior is unchanged.

### Still genuinely open (need infra/product decisions, not just code)
- **#1/#3/#4/#5/#14** — historical OHLC ingestion is implemented (Round 4) and multi-timeframe/trend/indicators now run on real data; NSE's historical endpoint response shape is still unverified against a live call in this sandbox — verify once network access exists.
- **#15 (fallback data source)** — Zerodha Kite Connect now exists as a structural second provider (Round 7 above: Angel One → Zerodha → NSE), but is entirely unverified against a live account (no keys were available). Still no *third* provider if all of Angel One + Zerodha + NSE are down simultaneously — unlikely in practice (NSE-only IP blocks are the realistic failure mode this was built for) but worth naming as the residual gap.
- **#18 (testing)** — the three original test files now run against mocked NSE/DB data instead of live services (Round 6 above), and pure-logic modules (indicators, option math, decision engine) have real scenario coverage (bullish/bearish/sideways/expiry-adjacent/high-VIX). Still missing: tests for `strategy_engine.py` (strike/premium/SL selection), `accuracy_engine.py`/`signal_accuracy.py` themselves (would need a temp SQLite DB seeded with fake `AnalysisResult`/`MarketData` rows), the AI-provider request/response path (`ai_engine.py` beyond the config-check test), and `angel_one.py`'s auth/session flow. `requirements-dev.txt` exists now but hasn't actually been run through `pytest` anywhere (no network in this sandbox to install it) — do that before trusting the suite fully.
- **#19/#20 (monitoring, load testing)** — `app/utils/health_metrics.py` (Round 6, see above) now tracks NSE/Angel One block-rate in-process and can fire an optional webhook, which covers the "is this thing quietly getting rate-limited" question. Still no load testing of the AI endpoints, and no monitoring across a fleet (health_metrics is per-process — see its own docstring).
- **`websocket.js`** — still an empty placeholder; dashboard polls every 10s via `setInterval` rather than real server push. Given NSE spot cache TTL is 10s already, a real websocket would mainly help if a lower-latency provider (e.g. Angel One's own WS feed) were wired in directly — currently not done.
- `app/config.py` — added `CORS_ALLOWED_ORIGINS`, `DATABASE_URL`, `REDIS_URL` settings
