# இந்த ZIP-ல் real-ஆக code-level-ல் செய்யப்பட்ட Fixes

இது ஏற்கனவே இருந்த `CODE_REVIEW.md`-ல் உள்ள 26 குறைகளை நானே தனியாக code-ஐ
படித்து verify பண்ணி, முடிந்தவரை **real code fixes** ஆக செய்தது (prompt
மட்டும் இல்லை). **Existing project architecture அழிக்கப்படவில்லை** — ஒவ்வொரு
fix-உம் existing files-ல் surgical edits ஆகவே செய்யப்பட்டது.

---

## 🔴 மிக முக்கியமான கண்டுபிடிப்பு (review-ல் இல்லாத ஒன்று)

**`app/templates/analysis.html`-ல் stray `}` — entire JS script-ஐ break பண்ணிக்கொண்டிருந்தது.**

`renderStrikes()` function-க்குள் ஒரு extra, unmatched `}` இருந்தது (Market
state color logic-க்குப் பிறகு). இது `<script>` block முழுவதையும் parse
ஆகாமல் ஆக்கியது — browser-ல் இது ஒரு **syntax error**, அதனால் script
முழுவதும் run ஆகாது. இதனால் தான் `loadAll()` call ஆகாமல், Analysis page
**எப்போதும் "Loading…"-ல் நின்றுகொண்டே இருக்கும்** — duplicate API calls
issue (review point #1) பங்களித்தாலும், இது தான் root cause. `node
--check`-ஆல் verify பண்ணி, fix பண்ணிய பிறகு எல்லா templates-உம் clean-ஆ
parse ஆகின்றன.

---

## ✅ Real code fixes (verify பண்ணி, synthetic data-உடன் test பண்ணப்பட்டவை)

| # | Review Point | File(s) | என்ன செய்யப்பட்டது |
|---|---|---|---|
| 1 | Duplicate API calls | `market_analyzer.py`, `ai_engine.py` | `get_full_market_overview()` மற்றும் `analyze_market()`-க்கு short-TTL (`@async_cache`) சேர்த்தேன். `/api/analysis/ai` + `/api/strategy/recommend` இப்போ ஒரே market snapshot-ஐயும் ஒரே AI call-ஐயும் share பண்ணும். (Unit-tested — different instances even hit the same cache.) |
| 2,3 | 5m/15m/1h real candles இல்லை | `data_fetcher.py`, `technical_indicators.py`, `market_analyzer.py` | Angel One-ல் இருந்து real intraday OHLCV (`get_multi_timeframe_ohlc`) fetch பண்ணி, `compute_from_ohlc()`-ல் Wilder ADX/ATR/Supertrend + real VWAP calculate பண்றேன். Angel One configure ஆகாதபோது `"N/A"` காட்டும் — flat/neutral ஆக காட்டாது. |
| 4 | Futures Premium hardcoded 0.0 | `angel_one.py` (`get_futures_ltp`), `data_fetcher.py` (`get_futures_premium`) | Real futures LTP fetch பண்ணி `premium = futures_ltp - spot`. Angel One இல்லாதபோது status `UNAVAILABLE`. |
| 5 | OI Change real-time இல்லை | `history_service.py` (`get_previous_option_snapshot`), `option_analyzer.py` (`classify_oi_buildup`) | முந்தைய snapshot-உடன் compare பண்ணி Long Buildup / Short Buildup / Short Covering / Long Unwinding classify பண்றேன். (Unit-tested.) |
| 6,7 | Tamil indicator module wire ஆகல | `market_analyzer.py` | `build_tamil_indicators()`-ஐ pipeline-ல் call பண்ணி `tamil_indicators` field-ஆ response-ல் சேர்த்தேன். Frontend-ல் புது "📊 Indicators — தமிழில்" card. |
| 8,9,25 | Next Move + Scenarios + Invalidation இல்லை | `scenario_engine.py` (புதிய file) | Upside/Downside/Sideways scenarios, probability %, target zone, invalidation level — review-ல் கொடுத்த exact format-ல். (Unit-tested — probabilities sum ~100%.) |
| 10 | Support/Resistance placeholder | `technical_indicators.py` (`build_key_levels`) | Pivot + OI Max-OI strikes + 1H swing high/low combine பண்ணி Strong Support/Support/Pivot/Resistance/Strong Resistance ladder. |
| 11 | PCR context indicator இல்ல | `decision_engine.py` (`_score_pcr`) | Price-vs-VWAP + RSI/MACD momentum உடன் agree பண்ணாதபோது PCR score damp ஆகும். (Unit-tested with the exact PCR=1.35 example from the review.) |
| 12 | VIX direction indicator ஆக தவறா score | `decision_engine.py` (`_score_vix`) | VIX இப்போ momentum-ஐ confirm பண்ணும்போது மட்டும் direction points தரும் — இல்லாட்டி "risk regime" ஆக மட்டும் காட்டும். |
| 13 | ATR bullish signal ஆக தவறா score | `decision_engine.py` (`_score_atr`) | ATR இனி direction score-க்கு contribute பண்ணாது — "expected range / risk" info ஆக மட்டும். |
| 14,15 | Supertrend/ADX approximate | `technical_indicators.py` (`compute_from_ohlc`) | Real High/Low/Close/Volume கிடைக்கும்போது standard Wilder formula-வுடன் calculate. |
| 16 | AI hallucination risk (timeframe) | `ai_engine.py` | Prompt-ல் AI-க்கு real multi-timeframe data கொடுத்து "invent பண்ணாதே, இதையே use பண்ணு" என்று instruct பண்றேன். Response-ல் AI-ன் guess எதுவானாலும் rule-engine-ன் real value-ஆல் override ஆகும். |
| 17 | Confidence நம்பகமா இல்ல | `decision_engine.py` | "Confidence" → "Signal Strength" (X/100). Data-completeness factor (எத்தனை live data sources) சேர்த்து calculate. |
| 19 | Expiry-day warning | `strategy.py` (already existed), `analysis.html` | Backend-ல் ஏற்கனவே `_expiry_filter()` இருந்தது — Final Signal-க்கு மேலே prominent banner-ஆ frontend-ல் elevate பண்ணினேன். |
| 20 | Market Closed UI இல்ல | `market_analyzer.py`, `analysis.html` | `market_closed` flag + last-updated timestamp-உடன் banner. |
| 21,22,23 | FII/DII/Global 0 vs N/A | `market_analyzer.py`, `analysis.py`, `strategy.py` | ஒவ்வொரு field-க்கும் `_status: LIVE/UNAVAILABLE` தனியாக track பண்ணி "🛰 Data Quality" card-ஆ காட்டறேன். |
| 24 | Final page structure | `analysis.html` | Market Closed/Expiry banners → Final Signal → Next Move (scenarios) → Multi-timeframe → Key Levels → OI Buildup → Tamil Indicators → Data Quality — review-ல் கொடுத்த layout-ஐ ஒட்டி. |

---

## 🟡 Verify பண்ணப்பட்ட முறை

இந்த sandbox-க்கு **network access இல்லை** (Angel One/NSE live API call
பண்ண முடியாது), எனவே:
- எல்லா Python files-உம் `py_compile`-ஆல் compile check பண்ணப்பட்டது.
- எல்லா templates-ன் JS-உம் `node --check`-ஆல் syntax verify பண்ணப்பட்டது.
- **புதிதாக எழுதப்பட்ட logic** (`compute_from_ohlc`, `build_scenarios`,
  `classify_oi_buildup`, decision_engine-ன் PCR/VIX/ATR fix) — synthetic
  OHLC/market data வைத்து **functionally unit-tested** செய்யப்பட்டது (review-ல்
  கொடுத்த exact examples-உடன், எ.கா PCR=1.35 + bearish momentum case).

## 🟠 இன்னும் தேவைப்படுவது (live environment-ல்)

- Angel One credentials வைத்து `.env`-ல் configure பண்ணி, real market
  hours-ல் ஒரு முறை test run பண்ண வேண்டும் — `get_futures_ltp`,
  `get_multi_timeframe_ohlc` ஆகியவை Angel One SDK-ன் response format-ஐ
  சார்ந்திருக்கின்றன (இங்கு network இல்லாததால் live-ஆ verify பண்ண முடியல).
- DB migration தேவையில்லை — `OptionData` table ஏற்கனவே இருக்கும் columns-ஐயே
  `get_previous_option_snapshot`-க்கு use பண்றேன்.
- Points 18 (UI clarity "No order placement"), 26-priority list-ன் 🟢
  bucket (signal-history accuracy tracking, false-signal tracking, model
  performance dashboard) — இவை product-features, இன்னொரு round-ல் தனியா
  செய்யலாம்.

---

## 🆕 இந்த session-ல் சேர்க்கப்பட்ட fixes

| # | Issue | Fix |
|---|---|---|
| 27 | **Accuracy page-ல் "data கிடைக்கவில்லை" — root cause: history எதுவும் save ஆகவே இல்லை** | `MarketData`/`AnalysisResult` rows ஒரு browser tab open பண்ணி page view பண்ணும்போது மட்டும் தான் save ஆகும் — server standalone-ஆ run ஆகும்போது எதுவும் save ஆகாது. `app/services/history_collector.py` புது file — app startup-லேயே ஒரு background `asyncio` task run ஆகி, `HISTORY_COLLECTOR_INTERVAL_MINUTES` (default 5 min) க்கு ஒருமுறை ஒவ்வொரு symbol-க்கும் market snapshot + AI signal save பண்ணும் — யாரும் page-ஐ பாக்காம இருந்தாலும். `app/api/routes/analysis.py`-ல் `build_ai_analysis()` helper-ஆ extract பண்ணி, route-உம் background collector-உம் ஒரே logic-ஐ share பண்றது (duplicate code இல்லை). `.env.example` + `render.yaml`-ல் புது env vars சேர்த்தேன்: `HISTORY_COLLECTOR_INTERVAL_MINUTES`, `HISTORY_COLLECTOR_SYMBOLS`. |
| 28 | **Live page-ல் price chart இல்லை** | `/api/market/candles/{symbol}` புது endpoint — real intraday OHLC (Angel One/Zerodha) return பண்ணும். `live.html`-ல் Plotly live line chart சேர்த்தேன் (1-min-க்கு ஒருமுறை refresh, symbol tab switch-க்கு redraw). Provider configure ஆகாதபோது "data கிடைக்கவில்லை" honest message காட்டும் — fabricate பண்ணாது. |
| 29 | **`trend_detection()` test disagreement** (MERGE_NOTES.md-ல் flag பண்ணப்பட்டது) | Production threshold (`ma5 > ma20*1.005`, 0.5% noise band) live signal-ஐ affect பண்றதால் அதை மாற்றவில்லை — `tests/test_analyzer.py`-ன் fixture slope-ஐ (`i*5` → `i*20`) ஒரு realistic-ஆன, threshold-ஐ தெளிவா clear பண்ணும் steady uptrend-ஆ மாற்றினேன். |

**Verify பண்ணப்பட்ட முறை (இந்த sandbox-க்கு network இல்லாததால்):**
- எல்லா மாற்றப்பட்ட/புதிய `.py` files-உம் `py_compile`-ஆல் clean.
- `live.html`, `market.py`, `analysis.py`, `main.py`, `history_collector.py`-ன் import graph manually trace பண்ணி circular-import இல்லைன்னு confirm பண்ணேன் (`history_collector.py` `analysis.py`-ஐ function-க்குள்ளதான் import பண்றது, module-level இல்ல).
- `fastapi`/`sqlalchemy` install பண்ண network இல்லாததால், **முழு `pytest` suite run பண்ண முடியல** — live environment-ல் ஒரு முறை `pytest` run பண்ணி confirm பண்ணிக்கோங்க.

---

## 🆕 Review feedback (Tamil bug report) — 5 major fixes

இந்த session-ல் தந்த Tamil review-ல் இருந்த 20 points-ல், மிக முக்கியமான 5-ஐ கீழே fix பண்ணேன்:

| # | Review Point | Fix |
|---|---|---|
| 1 | **Spot accuracy ≠ Option premium accuracy** (review #1, #5, #15) | `app/api/routes/analysis.py`-ல் `_pick_recommended_option()` — ஒவ்வொரு AI signal-க்கும் concrete strike/type/expiry/mid-price entry `recommended_option`-ஆ save ஆகும் (strategy.py-ன் existing liquidity-filtered `_pick_strikes()` reuse பண்ணி). `app/services/signal_accuracy.py`-ல் புது `compute_premium_accuracy()` — spot direction இல்ல, **actual saved option premium** (OptionData table) வைத்து correct/wrong/flat grade பண்ணும். Puதிய endpoint: `GET /api/accuracy/premium/{symbol}`. Accuracy page-ல் "Spot Direction Accuracy" vs "Option Premium Accuracy" இரண்டு தனித்தனி sections-ஆ காட்டி, disagreement gap-ஐயும் highlight பண்றேன் (theta/IV-crush pattern-ஐ directly காட்ட). |
| 2 | **flat/pending/no-data தெளிவா பிரிக்கல** (review #2) | Data layer-ல் ஏற்கனவே இது இருந்தது (`_fmt_bucket`-ல் correct/wrong/flat/total_graded தனித்தனி) — ஆனா UI-ல் ஒரே பெரிய % மட்டும் காட்டிச்சு. `accuracy.html`-ல் Graded/Flat/Pending/No-data 4 chips-ஆ, headline %-க்கு அடியில் தெளிவா காட்டறேன் — spot-ம் premium-ம் இரண்டு views-க்கும். |
| 3 | **Indicator double-counting** (review #7) | `decision_engine.py`-ல் trend (vwap/ema20/ema50/macd/adx/supertrend/rsi — 7), options-flow (pcr/oi_change/max_pain/call_writing/put_writing — 5), flow_global (futures/global/gift/fii/dii — 5) buckets-ஆ group பண்ணி, ஒரே bucket-ல் agree ஆகும் indicators-க்கு diminishing-returns weight (`1.0, 0.55, 0.35, 0.22, 0.15, 0.10, 0.08`) apply பண்றேன் — முதல் agreeing indicator full weight, அடுத்தவை shrinking fraction. `raw_bull_score`/`raw_bear_score` (பழைய undamped total) transparency-க்காக return dict-ல் இன்னும் இருக்கு, ஆனா bias/confidence dampened totals-ல் இருந்தே derive ஆகும். Confidence denominator-உம் (`MAX_DAMPENED_SCORE`) இதற்கு ஏத்தமாதிரி recalibrate பண்ணேன். எல்லா 8 `test_decision_engine.py` tests இன்னும் pass ஆகுது. |
| 4 | **Bid/Ask + IV + Greeks + Theta** (review #13, #14, #15) | புது `app/services/options_greeks.py` — dependency-free Black-Scholes (delta/gamma/theta/vega). `option_analyzer.py`-ல் `ce_mid`/`pe_mid`/`ce_spread_pct`/`pe_spread_pct` columns + `attach_greeks()`. `strategy.py`-ன் `_pick_strikes()` இப்போ **mid-price** (bid+ask/2, LTP இல்ல) entry/SL/target calc பண்றது, delta/theta_per_day/vega/spread_pct ஒவ்வொரு pick-க்கும் attach ஆகுது, spread 4%-க்கு மேல் இருந்தா "wide spread" warning வரும். |
| 5 | **SQLite → PostgreSQL** (review #16, #17) | `requirements.txt`-ல் `asyncpg` இப்போ **normal dependency** (optional-commented இல்ல — review #17-ன் exact complaint). `render.yaml`-ல் `databases:` block சேர்த்து Render-ன் managed free Postgres-ஐ Blueprint மூலமே auto-provision + auto-wire பண்றேன் (`DATABASE_URL` `fromDatabase` reference) — manual copy-paste step தேவையில்ல. **Real bug கண்டுபிடிச்சு fix பண்ணேன்**: Render Postgres `postgres://`/`postgresql://` URL கொடுக்கும் (default driver psycopg2, sync) — `create_async_engine()` அதை reject பண்ணும். `app/database.py`-ல் startup-லேயே `postgresql+asyncpg://`-ஆ auto-rewrite பண்ணறேன், இல்லாட்டி Postgres wiring startup-லேயே crash ஆயிருக்கும். |

**Verify பண்ணது:** எல்லா `.py`/`.html` files clean-ஆ compile/parse ஆகுது. `tests/test_decision_engine.py`-ன் 8 tests-ஐயும் manual-ஆ run பண்ணி confirm பண்ணேன் (dampening logic regression இல்ல). Postgres URL-rewrite logic தனியா simulate பண்ணி verify பண்ணேன். Network இல்லாததால் Angel One/live-data இணைந்த full integration path (option chain fetch → Greeks → accuracy) live environment-ல் ஒருமுறை verify பண்ணிக்கோங்க.

**Scope-ல் இன்னும் மீதி (இந்த session-ல் தொடவில்ல, future session):**
- #3 (5/10/15/30/60min exact-candle alignment vs current ±4min tolerance matching) — approximation ஆகவே இருக்கு.
- #4 (background collector-ஐ AI calls குறைக்க rule-engine-only fast path) — இப்போ AI இன்னும் 5min-க்கு ஒருமுறை call ஆகுது; rate-limit/cost கவனிக்கணும்னா `HISTORY_COLLECTOR_INTERVAL_MINUTES`-ஐ 15+ ஆக உயர்த்தலாம்.
- #6 (Confidence/Signal Strength UI framing) — ஏற்கனவே "Signal Strength" என்ற label இருக்கு, ஆனா UI-ல் full audit பண்ணல.
- #9 (strike-wise granular OI + price-vs-OI buildup classification) — broad PE>CE heuristic இன்னும் அப்படியே.
- #10 (Max Pain context-only framing in UI) — logic மாறல, weight ஏற்கனவே 5 (moderate) தான்.
- #12 (short-strategy risk engine: IV/liquidity/margin/event-risk முழுமையா) — Greeks + spread இப்போ கிடைக்குது, ஆனா risk_engine.py-ல் full integration பண்ணல.
- #18 (CORS `*`) — render.yaml-ல் ஏற்கனவே "tighten after first deploy" comment இருக்கு, doesn't auto-fix.
- #19 (WebSocket placeholder → real streaming) — polling தொடர்கிறது, "Real-time" வார்த்தை UI-ல் இருக்கான்னு தனியா பாக்கணும்.

---

## 🆕 OpenAI review follow-up — 4 targeted fixes

| # | Review Point | Fix |
|---|---|---|
| 1 | **Data இல்லை → 0 → bullish/bearish calculation ஆகக்கூடாது** | Verify பண்ணதில் decision_engine.py-ன் எல்லா `_score_*` functions ஏற்கனவே 0/missing-ஐ correctly "neutral" ஆக treat பண்றது (இது pre-existing, correct). ஆனா confidence அந்த "neutral-because-no-data" vs "neutral-because-genuinely-balanced" வித்தியாசத்தை தெரிஞ்சுக்கல — 6/20 indicators-ல் மட்டும் live data இருந்தாலும் அதே confidence formula run ஆகும். புது `_data_availability()` + `data_completeness_pct` — எவ்வளவு weight "real data" vs "missing" -ன்னு track பண்ணி, confidence-ஐ proportionally dampen பண்றேன் (test: full data confidence=89 vs degraded data (futures/gift/fii/dii unavailable + technicals placeholder) confidence=**50**, completeness=36%). Analysis page-ல் "⚠ X% data live" indicator சேர்த்தேன். |
| 2 | **Futures premium value_display "0" காட்டல் status unavailable-ஆ இருந்தாலும்** | Real bug — `tamil_explainer.py`-ல் global/gift/fii/dii எல்லாம் status unavailable-ஆ இருந்தா value_display "N/A" காட்டும் (ஏற்கனவே correct), ஆனா futures_premium மட்டும் அந்த pattern follow பண்ணல, எப்போதும் `+0` காட்டிச்சு — reviewer exact-ஆ இதைத்தான் flag பண்ணாங்க. `futures_status == "live"` check சேர்த்து மத்த 4 indicators-ஓட consistent-ஆ மாற்றினேன். (decision_engine.py-ன் scoring logic ஏற்கனவே status சரியா check பண்ணிக்கிட்டு இருந்துச்சு — அது unaffected.) |
| 3 | **Gross Premium Change vs Net P&L (transaction cost)** | `signal_accuracy.py`-ல் `ESTIMATED_ROUND_TRIP_COST_PCT = 2.0` — brokerage/STT/exchange charges/exit-side spread-ஐ ஒரு rough estimate-ஆ கணக்கில் எடுத்து, gross premium accuracy-க்கு அடுத்த ஒரு **தனி** "net_of_costs" grade/accuracy metric சேர்த்தேன் (`overall_net_of_costs`, `*_accuracy_net_of_costs`, per-horizon). Accuracy page-ல் "Overall — Net of Est. Costs" card தனியா காட்டுது. Live broker-specific charges இல்ல-ன்னு clearly labelled — estimate மட்டும். |
| 4 | **README.md stale "placeholder data" warning** | Futures premium/trend/RSI/MACD இப்போ real Angel One data (status-checked, fabricate பண்ணல) — ஆனா README இன்னும் பழைய "placeholder" warning வெச்சிருந்துச்சு, "project production-ready இல்ல"-ன்னு தவறா தோன்ற வைச்சுச்சு. Current data-honesty model (live vs unavailable vs confidence-dampening) accurately describe பண்ணி rewrite பண்ணினேன், DATABASE_URL/history-collector deployment pre-reqs-ஐயும் நினைவூட்டல்ஆ சேர்த்தேன். |

**Verify பண்ணது:** `py_compile` எல்லா `.py` files, JS syntax எல்லா templates, `tests/test_decision_engine.py`-ன் 8/8 tests pass (confidence dampening logic regression இல்ல), functional simulation-ல் confidence dampening expected-ஆ வேலை செய்யுது-ன்னு confirm பண்ணேன்.

**இன்னும் open (reviewer flag பண்ணது, இந்த round-ல் தொடல):**
- Market-regime-adaptive indicator weighting (TRENDING/SIDEWAYS/HIGH-VIX-க்கு வேற வேற weight profile) — இது ஒரு பெரிய architecture change, தனி session தேவை.
- 5/10/15/30/60min exact-candle horizon alignment (இப்போ ±4min nearest-snapshot tolerance).
- Background collector-ன் live Render reliability — code-level-ல் verify பண்ண முடியாது, live deployment-ல் மட்டும் confirm பண்ண முடியும்.
- Analysis page full UI hierarchy redesign (Market Status → Data Quality table → Final Signal → ... → Accuracy, reviewer suggested layout) — individual pieces (data_completeness, recommended_option, Greeks) இப்போ backend-ல் இருக்கு, full page redesign தொடல.

---

## 🆕 Production-readiness review follow-up — code-level fixes only

இந்த round-ல் reviewer-ன் 10-point production checklist-ல் இருந்து, **sandbox-ல் code-level-ல் verify+fix பண்ண முடிகிற** items மட்டும் எடுத்துக்கிட்டேன் (live Angel One/Render testing தேவைப்படும் items அடுத்த section-ல் தனியா குறிப்பிட்டிருக்கேன்).

| # | Issue | Fix |
|---|---|---|
| 1 | **PCR=0.0 → "Strong bearish" fabrication (severe)** | `decision_engine._score_pcr()`-ல் `pcr<=0` guard இல்லாததால், option chain fetch fail ஆனா 0.0 sentinel value **full-weight bearish signal**-ஆ score ஆகிடுச்சு — real bug, review #2-ன் exact concern. Guard சேர்த்தேன் (decision_engine.py + tamil_explainer.py இரண்டிலும் mirror பண்ணி). |
| 2 | **VIX=0.0 → "low volatility" fabrication** | `tamil_explainer.py`-ன் VIX section-ல் மட்டும் `vix<=0` guard இல்லாம "12-க்கு கீழ், low volatility regime"-ன்னு fabricate ஆகிச்சு (மத்த எல்லா indicators-லும் இந்த guard இருந்தது, இது ஒன்னு மட்டும் miss). `_volatility_context()`-லும் VIX/ATR-ல் ஒன்னு மட்டும் missing ஆனாலும் அது score-ல் real data-ஆ கலந்துடுற bug fix பண்ணேன் — இப்போ ஒவ்வொரு component-உம் தன் own availability-ஐ மட்டும் வைத்து contribute பண்ணும், label-ல் "partial data" flag தெரியும். |
| 3 | **Data Quality Gate — hard dampening** | `data_completeness_pct` ஏற்கனவே சேர்த்திருந்த confidence-dampening-ஐ Analysis page UI-ல் "⚠ X% data live" indicator-ஆ visible ஆக்கினேன் — signal strength-க்கு அடியில் WHY குறைவா இருக்குன்னு தெரியும். |
| 4 | **Structured CALL/PUT recommendation** | `recommended_option`-ல் strike/entry/SL/T1/T2/R:R/delta/theta/vega/spread எல்லாம் சேர்த்தேன் (strategy.py-ன் `_pick_strikes` single-source-of-truth-ஐ reuse பண்ணி). Analysis page-ல் புது "Recommended Option" card — reviewer suggest பண்ண format-ஐ ஒத்திருக்கு. |
| 5 | **AI Final Decision Maker இல்ல** | Verify பண்ணேன் — ஏற்கனவே சரியா implement ஆகியிருக்கு. `ai_engine.py`-ல் AI-ன் `market_bias`/`preferred_side` guess எதுவானாலும், rule-engine-ன் `decision` dict values-ஆலேயே unconditionally override ஆகுது (lines 62, 65). Code மாற்றல, existing architecture confirm பண்ணினேன் மட்டும். |
| 6 | **Order placement code** | Verify பண்ணேன் — `angel_one.py`-ல் `square_off_position()` (உண்மையான `placeOrder()` call) **already removed**, clear comment-ஆ இருக்கு "analysis/information only". `get_positions()` read-only. Code மாற்றல். |
| 7 | **CORS `*`** | `app/config.py`-ல் `cors_allowed_origins` property இப்போ raw value `*`-ஆ இருந்தா, Render-ன் auto-injected `RENDER_EXTERNAL_URL` env var-ஐ (எந்த manual config-உம் இல்லாம Render எல்லா web service-க்கும் தானாகவே கொடுக்கும்) auto-use பண்ணி domain-ஐ narrow பண்ணும் — RENDER_EXTERNAL_URL இல்லாத host-ல் மட்டும் `*` fallback. `main.py`-ல் startup-ல் resolved CORS origins log ஆகும் (இன்னும் `*`-ஆ இருந்தா loud warning). |

**Verify பண்ணது:** எல்லா `.py`/`.html` files clean-ஆ compile/parse ஆகுது. `tests/test_decision_engine.py` 8/8 pass. PCR/VIX fix-ஐ தனியா functional simulation பண்ணி confirm பண்ணேன் (`pcr=0.0` → neutral இப்போ, முன்ன bearish-ஆ இருந்துச்சு).

**இந்த sandbox-ல் என்னால் செய்ய/verify பண்ண முடியாதது (reviewer-ன் checklist-ல் இருந்து — live environment தேவை):**
- [ ] Angel One live API verification (Spot/Option Chain/OI/Futures/VIX/5m-15m-1H candles/Bid-Ask) — market open நேரத்தில் actual live test வேண்டும்.
- [ ] Render deployment-ல் background collector தொடர்ந்து இயங்குகிறதா (browser மூடினாலும்) — live confirm வேண்டும்.
- [ ] Render Postgres persistence — restart-க்கு பிறகு history இருக்குமா — live confirm வேண்டும்.
- [ ] Market Regime (TRENDING/SIDEWAYS/HIGH-VIX) → Decision Engine indicator-weight adaptive switching — இது ஒரு பெரிய, தனி architecture change (இப்போ static weights மட்டும்), இந்த session-ல் தொடல.
- [ ] Option Premium Accuracy production-grade validation — இப்போ code இருக்கு (compute_premium_accuracy), ஆனா real trading days-ன் history-ஐ வைத்து accuracy numbers meaningful-ஆ இருக்கான்னு live-ல் மட்டும் தெரியும்.

---

## 🆕 Market Regime → Decision Engine connected (review #4 — biggest remaining item)

முந்தைய round-ல் "இது ஒரு பெரிய architecture change, தனி session தேவை"-ன்னு flag பண்ணிருந்த review #4-ஐ இந்த session-ல் implement பண்ணேன்.

**கண்டுபிடிச்சது:** `app/services/market_regime.py` ஏற்கனவே முழுமையா இருந்தது (TREND_UP/TREND_DOWN/RANGE/BREAKOUT/BREAKDOWN/HIGH_VOLATILITY/LOW_VOLATILITY/EXPIRY_HIGH_GAMMA/NO_TRADE classify பண்ணும்) — ஆனா `/strategy` route-க்கு மட்டும் connect ஆகியிருந்தது, core `decision_engine.py`-க்கு (இதுதான் AI analysis + Accuracy engine இரண்டும் உபயோகிக்கும் scoring) இணைக்கப்படல.

**Fix:**
- `run_decision_engine()`-ல் ஒவ்வொரு run-லும் `classify_market_regime(market_data)` call பண்ணி, ஒவ்வொரு indicator-ன் points-ஐ அதன் bucket (trend/options_flow/flow_global/volume) அடிப்படையில் regime-specific multiplier வைத்து reweight பண்றேன் (correlation-dampening-க்கு முன்னாடி) — Trending day-ல் trend indicators (VWAP/EMA/ADX/Supertrend) 1.3x weight, options-flow 0.8x; Range day-ல் reverse (options-flow 1.3x, trend 0.65x); Breakout-ல் volume 1.4x.
- **HIGH_VOLATILITY / EXPIRY_HIGH_GAMMA** regimes-க்கு extra confidence multiplier (0.80x / 0.85x) — review-ன் "High VIX → Signal confidence Reduced" exact ask.
- **Hard gate**: `regime == "NO_TRADE"` (market opening 30min, VIX>30 extreme, market closed) ஆனா preferred_side="NONE"/Sideways-ஆ force பண்றேன் — soft confidence dampening இல்ல, actual block.
- ⚠️ Careful bug தவிர்த்தது: market_regime.py-ன் `no_trade` boolean field HIGH_VOLATILITY-க்கும் True ஆகும் (அது option buying/selling strategy gate பண்ண, வேற reason-க்காக) — அதை நேரடியா hard-gate-க்கு உபயோகிச்சிருந்தா VIX 25-30 range-லேயே எல்லா directional calls-உம் அடங்கிடும் (review expect பண்ணாத அளவுக்கு aggressive). `regime == "NO_TRADE"` (string) மட்டும் hard-gate-க்கு, HIGH_VOLATILITY-க்கு confidence-cut மட்டும் (review-ன் actual example "confidence reduced"-க்கு match ஆகுது).
- Analysis page-ல் "Regime: 📈 Trending Up" போன்ற badge சேர்த்தேன், result-ல் `market_regime`/`market_regime_confidence`/`market_regime_reasons` expose பண்ணேன்.
- `tests/test_decision_engine.py`-ன் VIX test-ஐ புது architecture-க்கு ஏத்தமாதிரி update பண்ணேன் (VIX இப்போ regime வழியா bull/bear-ஐ சிறிது reweight பண்ணும் — deliberate, review-ன் exact ask — ஆனா direction flip பண்ணாது, confidence-ஐ மட்டும் குறைக்கும்-ன்னு assert பண்றது).

**Verify பண்ணது:** `classify_market_regime` → `decision_engine.py` circular import இல்ல (market_regime.py `app.utils.helpers` மட்டும் import பண்றது). 8/8 tests pass. Functional test: TREND_UP day confidence=95 vs அதே setup HIGH_VOLATILITY (VIX 28)-ல் confidence=68, direction CALL-ஆவே இருக்கு (flip ஆகல) — expected behavior confirm பண்ணேன்.
