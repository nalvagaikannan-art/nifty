# MERGE_NOTES.md — how `nifty-deploy-updated.zip` + `nifty-deploy-v2_1_.zip` were combined

இரண்டு zip-களும் ஒரே `nifty-deploy-main` codebase-ன் **வெவ்வேறு திசையில் develop
ஆன இரண்டு branches** — ஒரு common ancestor-ல் இருந்து பிரிந்து, ஒவ்வொன்றும்
தனித்தனி features சேர்த்திருந்தது:

- **`nifty-deploy-updated.zip`** ("A") → Signal-accuracy tracking (`/accuracy`
  page + engine), Zerodha Kite Connect 2nd-broker fallback, Redis-backed
  cache/rate-limiter, optional Postgres, health-metrics endpoint, and a much
  more complete `tests/` suite (proper mocks, `conftest.py`, no live network
  calls).
- **`nifty-deploy-v2_1_.zip`** ("B") → V2 Decision Board: Confluence Engine,
  Market Regime classifier, Trade Levels calculator, Risk Engine, Paper
  Trading (+ `/api/paper-trade`), and the fix for the stray `}` in
  `analysis.html` that was breaking the whole page's JS (`FIXES_APPLIED.md`'s
  headline bug).

A plain `git merge` doesn't work here since both zips fully rewrite the same
files independently (no shared commit history for `diff3` to reconcile), so
each overlapping file was diffed by hand and resolved on its own merits:

| Resolution | Files |
|---|---|
| **A taken as-is** (strict superset of B) | `config.py`, `database.py`, `ai_engine.py`, `data_fetcher.py`, `market_analyzer.py`, `cache.py`, `rate_limit.py`, `api/routes/settings.py`, `.env.example`, `requirements.txt`, `CODE_REVIEW.md`, templates `base/live/settings/terminal/dashboard.html`, `tests/test_analyzer.py`, `tests/test_data_fetcher.py` |
| **B taken as-is** (A had reverted these features, added nothing new) | `api/routes/strategy.py` (V2 engine wiring), `templates/analysis.html` (V2 dashboard + the JS-syntax-error fix) |
| **Hand-merged** | `app/main.py` — combined both routers (`accuracy` **and** `paper_trade`), both service imports, both page routes (`/accuracy` **and** the existing paper-trade API) |
| **A-only new files kept** | `api/routes/accuracy.py`, `services/signal_accuracy.py`, `services/zerodha.py`, `utils/health_metrics.py`, `templates/accuracy.html`, `pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`, `tests/test_decision_engine.py`, `tests/test_option_analyzer.py`, `tests/test_technical_indicators.py` |
| **B-only files kept** | `api/routes/paper_trade.py`, `services/confluence_engine.py`, `services/market_regime.py`, `services/paper_trading.py`, `services/risk_engine.py`, `services/trade_levels.py`, `tests/test_v2_engines.py` |

## Verified after merging
- `python -m py_compile` on every `.py` file — clean.
- Every inline `<script>` in every template parsed with Node — clean (confirms
  the stray-`}` bug is gone in the merged `analysis.html`).
- `import app.main` — succeeds, all **53** routes register (both feature sets
  wired in together).
- Full `pytest` suite: **86 / 87 passing.**

## Bugs fixed during the merge (pre-existing in both source zips, not caused by merging — confirmed by re-running the same tests against the original, un-merged `A` source)
1. **`technical_indicators.py` — `volume_spike`/`volume_ratio` leaked numpy
   scalars** (`np.mean(...)` → `numpy.bool_`/`numpy.float64` instead of plain
   Python `bool`/`float`). `result["volume_spike"] is True` fails on a
   `numpy.bool_` even though it prints as `True` (identity check, not
   equality) — wrapped both in `bool()`/`float()`, matching the pattern
   already used elsewhere in the same file.
2. **`tests/test_data_fetcher.py` had a hardcoded expiry date of
   `28-Aug-2025`**, which is now in the past — the "future expiry" filter in
   `data_fetcher.py` correctly rejected it, so the test started failing once
   the calendar caught up. Bumped the fixture to `2099` so it won't rot again.

## Left as-is (flagged, not silently changed)
- `tests/test_analyzer.py::test_full_overview_uses_real_technicals_with_enough_history`
  still fails: a synthetic 60-day uptrend (+5 pts/day, ~0.02%/day) doesn't
  clear `trend_detection()`'s bullish threshold (`ma5 > ma20 * 1.005`, i.e.
  needs >0.5% separation) — it computes `ma5/ma20 ≈ 1.0015`, so the function
  correctly returns `"sideways"` by its own rule, but the test expects
  `"bullish"`. This is a **genuine disagreement between the test's fixture
  and the trend threshold**, present identically in the original
  `nifty-deploy-updated.zip` before any merging — not something a merge
  should resolve by guessing which side is "right". Either steepen the test's
  price slope or loosen the threshold, depending on which behavior you
  actually want.

---

# Post-merge feature: expiry-aware suggestions on the Analysis page

**Ask:** picking an option expiry date should surface Call/Put suggestions
for *that* expiry, on the same page (Analysis), instead of the app always
silently using the nearest expiry with no way to change it.

## What was missing
The data layer already fully supported per-expiry chains
(`DataFetcher.get_option_chain(symbol, expiry=...)`, used by the Option
Chain page's own expiry dropdown) — but `MarketAnalyzer.get_full_market_overview()`
never accepted or forwarded an `expiry` argument, so every consumer
(`/api/analysis/ai/{symbol}`, `/api/strategy/recommend/{symbol}`) was hard-wired
to the nearest expiry. The Analysis page itself had no expiry selector at all.

## Changes
- **`app/services/market_analyzer.py`** — `get_full_market_overview(symbol, expiry=None)`
  now forwards `expiry` to the option-chain fetch. Everything downstream
  (PCR, max pain, OI support/resistance walls, the decision engine, strike
  picks, price levels, expiry-day risk) automatically reflects the requested
  expiry, since it all reads from the one fetched `chain`. The `@async_cache`
  key already includes every argument, so different expiries no longer share
  a cache entry (regression-tested — see below).
- **`app/api/routes/strategy.py`** (`/recommend/{symbol}`) and
  **`app/api/routes/analysis.py`** (`/ai/{symbol}`) — both gained an optional
  `?expiry=` query param (omit for previous nearest-expiry behaviour), and
  both now echo back `expiry` + `all_expiries` in their JSON so the frontend
  can populate a dropdown without a separate call.
- **`app/templates/analysis.html`** — new expiry `<select>` next to the
  symbol tabs (same pattern as the Options page's own selector), populated
  from `all_expiries` on load. Changing it re-fetches both `/ai` and
  `/recommend` with that expiry and re-renders the Final Signal / strikes /
  V2 dashboard in place — no navigation needed. A small "Suggestions for:
  <date>" label makes the active expiry unambiguous even when "Nearest
  Expiry" is selected. Switching symbols resets back to nearest, since each
  symbol has its own expiry calendar.

## Verified
- `python -m py_compile` on the 3 changed `.py` files — clean.
- Inline `<script>` in the edited `analysis.html` re-parsed with Node — clean.
- `import app.main` — still succeeds, still 53 routes.
- Full `pytest` suite: **88 / 89 passing** (same 1 pre-existing failure noted
  above; unrelated to this change).
- Two new regression tests in `tests/test_analyzer.py`:
  `test_expiry_param_is_forwarded_to_option_chain_fetch` (proves the
  parameter reaches `DataFetcher.get_option_chain` end-to-end from the
  analyzer) and `test_different_expiries_do_not_share_a_cache_entry` (proves
  switching expiry is a cache miss, not stale reused data) — both pass.

---

# Post-merge fix: every strike/leg suggestion now shows its exact contract expiry

**Ask:** "23,400 CE" alone doesn't say *which* contract — with weekly and
monthly options often live on the same strike simultaneously, the Analysis
page's Best Strike / Aggressive / Conservative cards and Strategy Legs panel
need to show `Strike + CE/PE + exact Expiry` together, always live from the
option chain, never hardcoded.

## Changes
- **`app/api/routes/strategy.py`** — `_pick_strikes()` now stamps every
  returned strike with `"expiry": chain_data.get("expiry", "")` — the same
  `chain_data` its strike/LTP/OI/IV numbers already came from, so it's
  guaranteed to be the exact contract's expiry, not a separate lookup that
  could drift. Also carried into the liquidity-warning text
  (`"24400 CE (28-Aug-2025): thin OI"` instead of a bare strike+type).
- **`app/services/strategy_engine.py`** — `generate_option_strategy()` reads
  `market_data["option_chain"]["expiry"]` once and threads it through every
  strategy branch (single-leg directional, weak-trend, straddle/strangle,
  iron condor) into `_leg()`, so **every** leg of **every** strategy type
  carries `"expiry"`, and each `reasoning` sentence now names the expiry too
  (e.g. *"BUY 24650 CE (28-Aug-2025 expiry) @ ₹263.4 LTP..."*).
- **`app/templates/analysis.html`** — strike-card badges now render
  `24400 CE | 28-Aug-2025` (the compact form), and Strategy Legs rows render
  `24350 — 28-Aug-2025 Expiry` next to the BUY/SELL + CE/PE label — so a leg
  row reads as `BUY CE — 24350 — 28-Aug-2025 Expiry — ₹263.35`, the complete
  contract in one line.
- Every value flows from this request's already-fetched, already-resolved
  option chain (`chain_data["expiry"]` / `market_data["option_chain"]["expiry"]`)
  — the same source `/api/strategy/recommend`'s top-level `expiry` field and
  the Analysis page's expiry selector both already use. Nothing new is
  fetched and no date is ever written into the code.

## Left out of scope (didn't have this bug)
- **Signal History** rows (`06:56:47 BUY CE Score:65 ...`) never showed a
  strike number at all, only direction — so there was no "23,400 CE"-without-
  expiry problem there to begin with. Adding strike+expiry to that historical
  log would mean logging more data going forward, not a display fix; flagging
  it here in case that's wanted separately.
- **`dashboard.html`**'s small "Strike: ATM+1" line reads
  `decision.recommended_strike`, which is a relative label ("ATM"/"ATM+1"),
  not an actual strike price — there's no specific contract there to attach
  an expiry to.
- `market-terminal.jsx` at the repo root generates its option-chain rows from
  `Math.random()`, not the live backend — it isn't wired into the app's data
  flow at all (no route serves it), so there's no live expiry for it to show.

## Verified
- `python -m py_compile` on the 2 changed `.py` files — clean.
- Inline `<script>` in the edited `analysis.html` re-parsed with Node — clean.
- Manually rendered both changed HTML snippets with sample data (Node) to
  confirm exact output: `24400 CE | 28-Aug-2025` and
  `BUY CE — 24350 — 28-Aug-2025 Expiry — ₹263.35`.
- Full `pytest` suite: **98 / 99 passing** (same 1 pre-existing failure,
  unrelated). **10 new tests**:
  - `tests/test_strategy_engine.py` (6 tests) — every strategy branch
    (single-leg, weak-trend, straddle/strangle, iron condor) attaches the
    live expiry to every leg; switching expiry changes the leg, not just a
    top-level field; a missing expiry degrades to `""` rather than crashing
    or fabricating a date.
  - `tests/test_strike_picker.py` (4 tests) — `_pick_strikes()` attaches the
    chain's expiry to every CALL and PUT strike it picks; picking from a
    different expiry's chain changes the strike's expiry even when the
    strike price itself is identical (the weekly-vs-monthly ambiguity case
    from the original report); missing expiry → `""`, never fabricated.

