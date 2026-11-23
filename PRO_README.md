# AI NIFTY Option Analyzer **PRO+** (nifty-pro)

உங்கள் **production பைல் (nifty-production-source-20261114.zip) எதுவும் மாற்றப்படவில்லை**. அதன் சுத்தமான நகல்
(backup `.bak/.before_*` பைல்கள், காலி பைல்கள், DB, logs நீக்கப்பட்டது) + blueprint அம்சங்கள் இதில் சேர்க்கப்பட்டுள்ளன.
இயல்பாக: **port 8001**, **தனி DB `data/analyzer_pro.db`**, **PAPER mode**.

## புதிதாக சேர்ந்தவை
| பகுதி | பைல் | என்ன செய்கிறது |
|---|---|---|
| IV Rank/Percentile, PCR, Max Pain, OI walls | `app/services/pro_metrics.py`, `iv_history.py` | ATM IV-யை நாள்தோறும் சேமித்து Rank கணக்கிடும் (≥30 நாள் வரலாறு வரை "நம்பகமில்லை" என்று WAIT) |
| SELL சிக்னல் | `app/services/sell_signals.py` | SHORT_STRANGLE / SHORT_STRADDLE (IV Rank>70 & range-bound), IRON_CONDOR (Rank≥30), COVERED_CALL (advisory). Strike ≈1σ, OI-wall-க்கு அப்பால்; hedge wings; SL=1.4× premium, target=50% decay; expiry நாளில் "3 PM-க்குள் மூடு" |
| MCX + Impact meter | `app/services/commodity_feed.py` | Kite MCX LTP + 15-min RSI; Kite இல்லையென்றால் global % மட்டும். Crude→Nifty தாக்கம் (heuristic β −0.4) |
| செய்தி சென்டிமென்ட் | `app/services/news_sentiment.py` | NewsAPI headline score (key இல்லையென்றால் "unavailable") |
| ஆர்டர் எக்ஸிக்யூஷன் | `app/services/order_executor.py` | PAPER இயல்பு; LIVE-க்கு பல கதவுகள் (கீழே). Angel One NFO/BFO மட்டும் |
| API | `app/api/routes/pro.py` | `/api/pro/*` |
| டேஷ்போர்டு | `app/templates/pro.html` → **/pro** | சிக்னல் கார்டுகள், ஆர்டர் பேனல், MCX, செய்தி, லெட்ஜர் |
| Tests | `tests/test_pro_*.py` | 35 புதிய tests (மொத்தம் 581 PASS) |

## அசல் கோடில் நான் சரிசெய்தவை (நகலில் மட்டும்)
1. **Expiry நாள்:** `economic_calendar.py` வியாழன் என்று கணக்கிட்டது. செப்டம்பர் 2025 முதல் **Nifty weekly = செவ்வாய்**, monthly = மாதத்தின் கடைசி செவ்வாய்; **Sensex = வியாழன்**; BANKNIFTY/FINNIFTY **monthly மட்டும்**. (Blueprint-ம் "வியாழன்" என்றது — அது பழையது.) SELL எஞ்சின் DTE-ஐ chain-ன் உண்மையான expiry-யிலிருந்தே எடுக்கும்.
2. `config.py`: புதிய settings; `run.py`: default port 8001; default DB பெயர் `analyzer_pro.db`.
3. `main.py`, `base.html`: `/pro` route + menu.

## ஆர்டர் பாதுகாப்பு கதவுகள் (LIVE-க்கு எல்லாமே தேவை)
`TRADING_MODE=live` **மற்றும்** `LIVE_TRADING_ENABLED=true` • `ORDER_PIN` அமைத்து header `X-Order-Pin` • Kill switch OFF •
qty = lot size-ன் மடங்கு, `MAX_LOTS_PER_ORDER` (1) • `MAX_ORDERS_PER_DAY` (10) • hedge இல்லாத naked SELL தடை (`ALLOW_NAKED_SELL=false`) •
hedge BUY முதலில், பிறகு SELL; ஒரு leg தோற்றால் மீதி நிறுத்தப்படும் • free cash ≥ `MIN_FREE_CASH_PER_SELL_LOT`×lots (இது SPAN margin அல்ல, உங்கள் வரம்பு) •
requested expiry ≠ broker-ல் கிடைத்த expiry என்றால் ஆர்டர் இல்லை.
**Client அனுப்பும் விலை/legs நம்பப்படாது** — `signal_id` மட்டும்; server மீண்டும் chain-ல் இருந்து கணக்கிடும் (மாறியிருந்தால் 409).

## நிறுவல் (Oracle server; production-ஐ நிறுத்தாமல்)
```bash
# Windows PowerShell-ல் (zip உள்ள folder-ல்):
scp -i "$HOME\Downloads\ssh-key-2026-08-28.key" .\nifty-pro-20261114.zip ubuntu@<SERVER_IP>:~/

# Server-ல்:
cd ~ && unzip -o nifty-pro-20261114.zip && cd ~/nifty-pro
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env      # keys நிரப்பவும்
./venv/bin/python -m pytest -q                           # 581 passed என்று வர வேண்டும்
PORT=8001 ./venv/bin/python run.py                       # முதலில் கையால் ஓட்டி பார்க்கவும்
```
தொலைபேசி/PC-யிலிருந்து பார்க்க (port திறக்காமல், பாதுகாப்பானது):
`ssh -i "$HOME\Downloads\ssh-key-2026-08-28.key" -L 8001:localhost:8001 ubuntu@<SERVER_IP>` → உலாவியில் `http://localhost:8001/pro`

## முக்கிய எச்சரிக்கைகள் (நேர்மையாக)
- **Production பாதுகாப்பு:** இரண்டாவது app அதே Angel One கணக்கில் login செய்தால் production-ன் session/rate-limit பாதிக்கப்படலாம் (இது எனக்கு உறுதியாகத் தெரியாது). உங்கள் paper-validation (13–27 நவ) நடக்கிறது — **சந்தை நேரத்தில் இதை ஓட்ட வேண்டாம்**; சந்தை முடிந்த பிறகு மட்டும் சோதிக்கவும், அல்லது தனி API key பயன்படுத்தவும்.
- **IV Rank** புதிய DB-ல் வரலாறு இல்லாததால் **≥30 வர்த்தக நாட்கள்** வரை SELL சிக்னல் வராது ("IV வரலாறு போதாது"). இது வேண்டுமென்றே; வரலாறு இல்லாமல் rank கணக்கிடுவது தவறான நம்பிக்கை தரும்.
- **Range-bound (ADX):** ADX தரப்படாவிட்டால் proxy (spot≈max pain, PCR 0.8–1.2) பயன்படும்.
- **Broker பகுதிகள் உண்மையான கணக்கில் சோதிக்கப்படவில்லை:** Angel `placeOrder`/`rmsLimit`, Kite MCX (`instruments`, `quote`, `historical_data`) — fake objects-உடன் மட்டுமே tests. முதல் ஓட்டத்தில் logs பாருங்கள்.
- **Static IP:** ஆர்டர் API-க்கு broker portal-ல் server IP பதிவு தேவை; விதிகள் மாறக்கூடும் — Angel One-ல் சரிபார்க்கவும். (Oracle reserved public IP பயன்படுத்தவும்.)
- **MCX ஆர்டர்கள் இல்லை:** MCX சிக்னல்கள் ஐடியா மட்டும். Covered call = advisory மட்டும். SELL (premium விற்பனை) இழப்பு வரம்பற்றதாகலாம் — naked இயல்பாகத் தடை.
- Blueprint-ன் "SEBI RA/IA தேவையில்லை", "10 orders/sec" போன்ற சட்ட/விதி கூற்றுகளை நான் சரிபார்க்கவில்லை — நான் வழக்கறிஞர் அல்ல; broker/SEBI அறிவிப்புகளில் உறுதி செய்யவும்.
- Kite: `pip install kiteconnect` (நீங்கள் 23 அக்-ல் முடக்கியிருந்தீர்கள்; இங்கு விருப்பம்). Access token தினமும் புதுப்பிக்க வேண்டும்.
- Auth இல்லை: `/pro` page-ல் login கிடையாது (PIN ஆர்டருக்கு மட்டும்). Port 8000/8001-ஐ உலகிற்குத் திறந்து வைக்காதீர்கள் — SSH tunnel பயன்படுத்தவும்.

## LIVE-க்கு மாறுவது (paper முடிந்த பிறகு மட்டும்)
`.env`-ல்: `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, `ORDER_PIN=<ரகசிய எண்>` → restart → `/pro`-ல் PIN உள்ளிடவும். முதலில் **1 lot**.
