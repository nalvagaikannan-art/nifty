# NIFTY-PRO — QA மற்றும் Production Deployment Gate

இந்த ஆவணம் `nalvagaikannan-art/nifty` repository-யின் `nifty-pro-qa` branch-க்கானது.
இது பழைய Oracle production service அல்லது பழைய Render service-ஐ மாற்றுவதற்கான வழிமுறை அல்ல.

## தற்போதைய நிலை

- QA branch: `nifty-pro-qa`
- Render Blueprint service பெயர்: `nifty-pro-analyzer`
- Render database பெயர்: `nifty-pro-db`
- Application mode: paper-only
- Broker live order placement: தடைசெய்யப்பட்டுள்ளது
- QA CI: 604 passed, 0 failed, 1 warning (2026-11-23 run #45124591499; commit `78c8cc97919efadd2abeac73744373eea987e7d6`)
- Render resources: இன்னும் உருவாக்கப்படவில்லை
- Production deployment: செய்யப்படவில்லை

**முக்கியம்:** இந்த ஆவணத்தைப் படிப்பது அல்லது GitHub-ல் commit செய்வது deploy அனுமதி அல்ல. கீழே உள்ள gates அனைத்தும் நிறைவேறும் வரை Render Dashboard-ல் Blueprint-க்கு Apply செய்யக்கூடாது; புதிய service/database உருவாக்கக்கூடாது.

## 1. GitHub source சரிபார்ப்பு

Repository: `nalvagaikannan-art/nifty`

Deploy செய்ய வேண்டிய branch: `nifty-pro-qa` மட்டும்.

GitHub Actions workflow:
`.github/workflows/nifty-pro-qa-tests.yml`

ஒவ்வொரு deploy candidate commit-க்கும்:
1. Branch head SHA-ஐ உறுதிப்படுத்தவும்.
2. அந்த SHA-க்கு உரிய `NIFTY-PRO QA Tests` workflow வெற்றியடைந்திருக்க வேண்டும்.
3. முழு pytest suite-ல் தோல்வி பூஜ்ஜியம் இருக்க வேண்டும்.
4. Workflow முடிந்த பின் branch-ல் புதிய commit வந்திருந்தால் பழைய CI முடிவை deploy approval ஆகப் பயன்படுத்தக்கூடாது.

QA CI வெற்றி மட்டும் live market-data validation அல்லது production readiness நிரூபிக்காது.

## 2. Render Blueprint — அமைப்பு

`render.yaml`-ல் வரையறுக்கப்பட்ட தனித்த வளங்கள்:

| Resource | பெயர் | Branch / Database |
|---|---|---|
| Web Service | `nifty-pro-analyzer` | `nifty-pro-qa` |
| PostgreSQL | `nifty-pro-db` | தனி NIFTY-PRO DB |

Blueprint-ல் தற்போது Free plans குறிப்பிடப்பட்டுள்ளன. அவை production market-data collector மற்றும் தொடர்ச்சியான paper-history சேமிப்புக்கு ஏற்றவை என்று கருதக்கூடாது. Free service sleep, database expiry, retention, backup, uptime மற்றும் persistent storage தேவைகள் தீர்மானிக்கப்பட வேண்டும்.

**செலவு அனுமதி இல்லாமல் paid plan தேர்வு செய்யவோ, Free plan-ஐ production-க்கு ஏற்றது எனக் கருதவோ கூடாது.** Render plan/pricing/limits-ஐ deployment நாளில் அதிகாரப்பூர்வ ஆவணத்தில் மீண்டும் சரிபார்க்க வேண்டும்.

## 3. முதல் deploy-க்கு முன் தேவையான secrets

Render Dashboard-ல் secrets ஆக மட்டும் அமைக்க வேண்டும்; Git-ல் உண்மையான values சேர்க்கக்கூடாது.

- `APP_AUTH_USERNAME` — தனிப்பட்ட strong username
- `APP_AUTH_PASSWORD` — நீளமான, unique password
- குறைந்தது ஒரு AI provider key: `GEMINI_API_KEY`, `OPENAI_API_KEY`, அல்லது `DEEPSEEK_API_KEY`
- `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD`, `ANGEL_TOTP_SECRET` — Angel One live market-data login (Render Blueprint-ல் secret placeholders சேர்க்கப்பட்டுள்ளன; values Render Dashboard-ல் மட்டும் அமைக்கவும்)
- Zerodha credentials — தேவையானால் மட்டும்; live data fallback ஆய்வு முடிந்த பின் அமைக்கவும்

Secrets-ஐ chat, commit, logs அல்லது screenshots-ல் பகிர வேண்டாம். Production authentication secrets இல்லையெனில் middleware fail-closed ஆக இருக்க வேண்டும்.

## 4. கட்டாய safety configuration

கீழே உள்ள மதிப்புகள் deploy candidate-ல் மாற்றமின்றி இருக்க வேண்டும்:

| Variable | Required value |
|---|---|
| `ENV` | `production` |
| `TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `ALLOW_CRED_WRITE` | `0` |
| `DATABASE_URL` | Blueprint-ன் `nifty-pro-db` connection |
| `SQLITE_DB_PATH` | `./data/analyzer_pro.db` (fallback மட்டும்) |
| `HISTORY_COLLECTOR_SYMBOLS` | `NIFTY,BANKNIFTY,FINNIFTY,SENSEX` |
| `HISTORY_COLLECTOR_INTERVAL_MINUTES` | `5` |

`/healthz` மட்டும் public health endpoint. மற்ற application routes Basic Authentication மூலம் பாதுகாக்கப்பட வேண்டும். `/healthz` response மட்டும் app readiness, DB readiness அல்லது market-feed readiness நிரூபிக்காது.

## 5. Deploy approval gates — அனைத்தும் PASS ஆக வேண்டும்

- [ ] Latest candidate SHA-க்கு GitHub Actions full suite PASS.
- [ ] Auth tests: unauthenticated request 401; production credentials missing 503.
- [ ] Live execution tests: PIN இருந்தாலும் broker order call சாத்தியமில்லை.
- [ ] Paper ledger tests PASS; paper records DB-ல் நிலையாக எழுதப்படுகின்றன.
- [ ] NIFTY, BANKNIFTY, FINNIFTY, SENSEX — ஒவ்வொன்றுக்கும் spot, option-chain, expiry, lot-size, quote freshness, exchange/token mapping சரிபார்க்கப்பட்டது.
- [ ] SENSEX BFO mapping மற்றும் NSE symbols-க்கு NFO mapping தனித்தனியாக சரிபார்க்கப்பட்டது.
- [ ] Live-feed validation உண்மையான provider data-வில் செய்யப்பட்டது; fixtures/mock மட்டும் போதாது.
- [ ] Database migration/initialization, backup மற்றும் restore rehearsal முடிந்தது.
- [ ] Render service/database plan, monthly cost, region, retention மற்றும் persistence உறுதி செய்யப்பட்டது.
- [ ] Authentication, secrets, CORS, rate limit, logs மற்றும் health checks இறுதி ஆய்வு முடிந்தது.
- [ ] Rollback procedure மற்றும் rollback trigger சோதிக்கப்பட்டது.
- [ ] Deploy செய்யும் நாள் weekday; NSE market close ஆன பிறகு 15:30 IST கடந்திருக்க வேண்டும்.

ஏதேனும் ஒரு symbol தோல்வியடைந்தால் deploy நிறுத்தப்பட வேண்டும். Deploy பிந்தைய முதல் 10 trading days real-money trading செய்யக்கூடாது; நான்கு symbols-ல் ஏதேனும் ஒன்று தோல்வியடைந்தால் rollback செய்ய வேண்டும். Paper validation முடியும் வரை project status `Preparation` ஆகவே இருக்கும்.

## 6. Deployment செய்யும் நாளில் மட்டும்

மேலுள்ள எல்லா gates-க்கும் ஆதாரம் கிடைத்து, செலவு/வள ஒப்புதல் கிடைத்த பின்:

1. GitHub branch மற்றும் commit SHA-ஐ இறுதியாக உறுதிப்படுத்தவும்.
2. Render Dashboard-ல் சரியான repository மற்றும் `nifty-pro-qa` branch தேர்ந்தெடுக்கப்பட்டுள்ளதா உறுதிப்படுத்தவும்.
3. Blueprint preview-ல் உருவாகும் resource பெயர்கள் `nifty-pro-analyzer`, `nifty-pro-db` என்பதையும் பழைய வளங்கள் எதுவும் மாற்றப்படவில்லை என்பதையும் சரிபார்க்கவும்.
4. Secrets மற்றும் paper-only environment values அமைக்கப்பட்டுள்ளதா உறுதிப்படுத்தவும்.
5. தனி deployment approval கிடைத்த பிறகே Apply செய்யவும்.
6. Deploy முடிந்ததும் auth, DB, health, four-symbol live-feed மற்றும் paper-ledger smoke tests செய்யவும்.
7. தோல்வி ஏற்பட்டால் உடனடியாக rollback; பழைய Oracle/Render service-ஐத் தொடவோ மாற்றவோ கூடாது.

## 7. பழைய வழிமுறைகள் செல்லாது

இந்த NIFTY-PRO split-க்கு கீழே உள்ள பழைய பொதுவான வழிமுறைகளைப் பயன்படுத்த வேண்டாம்:

- `main` branch-க்கு push செய்வது
- `ai-nifty-analyzer` என்ற பழைய service பெயர்
- `./data/analyzer.db` என்ற பழைய production DB பாதை
- CORS-ஐ `*` ஆக நிரந்தரமாக வைப்பது
- முதலில் deploy செய்து பின்னர் secrets/auth அமைப்பது

புதிய மாற்றங்கள் அனைத்தும் `nifty-pro-qa` branch-ல் மட்டுமே செய்யப்பட வேண்டும். பழைய Oracle production branch/service மற்றும் பழைய Render service/database தனியாகவே இருக்க வேண்டும்.
