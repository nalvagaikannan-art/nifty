# GitHub + Render Deployment Guide — AI NIFTY Option Analyzer Pro

இந்த guide உங்கள் project-ஐ **GitHub-க்கு push** செய்து, **Render-ல் live-ஆக deploy** செய்ய தேவையான அனைத்து steps-ஐயும் கொடுக்கிறது. இரண்டு Render deploy methods கொடுக்கப்பட்டுள்ளன — Blueprint (fast, recommended) மற்றும் Manual (full control).

---

## தேவையானவை (Prerequisites)

- [GitHub](https://github.com) account
- [Render](https://render.com) account (GitHub-உடன் நேரடியாக sign up செய்யலாம்)
- குறைந்தது ஒரு AI provider API key — Gemini/OpenAI/DeepSeek-ல் ஏதேனும் ஒன்று
  (Gemini key இலவசமாக [aistudio.google.com/apikey](https://aistudio.google.com/apikey)-ல் கிடைக்கும்)
- `git` உங்கள் கணினியில் install செய்யப்பட்டிருக்க வேண்டும் (`git --version` எனச் சரிபார்க்கவும்)

---

## Part 1 — GitHub-க்கு Push செய்தல்

### Step 1.1 — Local-ல் Git repo initialize செய்யவும்

Project folder-ஐ unzip செய்த பின், அந்த folder-க்குள் சென்று:

```bash
cd ai_nifty_analyzer
git init
git add .
git status
```

`git status` output-ல் `.env` **தென்படக்கூடாது** (`.gitignore`-ல் ஏற்கனவே exclude செய்யப்பட்டுள்ளது). `.env.example` மட்டும் தென்பட வேண்டும் — அது சரி, அதில் real secrets இல்லை.

```bash
git commit -m "Initial commit: AI NIFTY Option Analyzer Pro"
```

### Step 1.2 — GitHub-ல் புதிய repository உருவாக்கவும்

1. [github.com/new](https://github.com/new)-க்குச் செல்லவும்
2. Repository name: `ai-nifty-analyzer` (அல்லது உங்கள் விருப்பம்)
3. **Public** அல்லது **Private** — உங்கள் விருப்பம் (Private recommended, code-ல் உங்கள் architecture இருப்பதால்)
4. **"Add a README file" checkbox-ஐ UNCHECK செய்யவும்** (ஏற்கனவே README.md உள்ளது)
5. **Create repository** click செய்யவும்

### Step 1.3 — Local repo-ஐ GitHub-உடன் இணைக்கவும்

GitHub, repo உருவான பின் ஒரு URL தரும் (இது போல இருக்கும்):

```bash
git remote add origin https://github.com/<உங்கள்-username>/ai-nifty-analyzer.git
git branch -M main
git push -u origin main
```

Username/password கேட்டால் — GitHub password ஏற்காது, **Personal Access Token** தேவை:
[github.com/settings/tokens](https://github.com/settings/tokens) → "Generate new token (classic)" → `repo` scope select செய்யவும் → அந்த token-ஐ password-க்கு பதிலாக paste செய்யவும்.

✅ **சரிபார்க்க:** உங்கள் GitHub repo பக்கத்தை refresh செய்யவும் — எல்லா files-ம் தெரிய வேண்டும், `.env` தெரியக்கூடாது.

---

## Part 2 — Render-ல் Deploy செய்தல்

### Method A — Blueprint மூலம் (Fast, ஒரே click)

இந்த repo-வில் ஏற்கனவே `render.yaml` இருக்கிறது — Render அதை படித்து தானாகவே எல்லா settings-ஐயும் configure செய்யும்.

1. [dashboard.render.com](https://dashboard.render.com)-ல் login செய்யவும்
2. **New +** → **Blueprint** click செய்யவும்
3. உங்கள் GitHub account-ஐ connect செய்யவும் (முதல் முறை என்றால் permission கேட்கும்)
4. `ai-nifty-analyzer` repo-ஐ select செய்யவும்
5. Render, `render.yaml`-ஐ கண்டுபிடித்து preview காட்டும் — **Apply** click செய்யவும்
6. Deploy தொடங்கும் (2-4 நிமிடங்கள் ஆகலாம்)

Deploy ஆன பின், **Step 2.3 (Environment Variables)**-க்குச் செல்லவும் — API keys set செய்ய வேண்டும்.

### Method B — Manual Setup (Full control)

1. [dashboard.render.com](https://dashboard.render.com) → **New +** → **Web Service**
2. GitHub repo-ஐ connect செய்து select செய்யவும்
3. கீழ்க்கண்டவாறு நிரப்பவும்:

| Field | Value |
|---|---|
| **Name** | `ai-nifty-analyzer` |
| **Region** | Singapore (இந்தியாவுக்கு அருகில்) |
| **Branch** | `main` |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | Free (testing-க்கு) |

4. **Create Web Service** click செய்யவும் (இப்போது build fail ஆகலாம் — env vars இன்னும் set ஆகவில்லை, அது Step 2.3-ல் சரி செய்யப்படும்)

### Step 2.3 — Environment Variables Set செய்யவும்

Render dashboard-ல் உங்கள் service page-க்குச் சென்று → **Environment** tab:

இதை ஒவ்வொன்றாக **Add Environment Variable** மூலம் சேர்க்கவும் (உங்கள் `.env.example`-ஐ reference ஆகப் பயன்படுத்தவும்):

| Key | Value |
|---|---|
| `ENV` | `production` |
| `AI_PROVIDER` | `gemini` (அல்லது `openai`/`deepseek`) |
| `GEMINI_API_KEY` | உங்கள் real key |
| `SQLITE_DB_PATH` | `./data/analyzer.db` |
| `CACHE_TTL` | `60` |
| `LOG_LEVEL` | `INFO` |
| `LOG_FILE` | `./logs/app.log` |
| `CORS_ALLOWED_ORIGINS` | `*` (deploy ஆன பின் உங்கள் actual URL-க்கு மாற்றவும் — Step 2.5 பார்க்கவும்) |
| `API_RATE_LIMIT_PER_MINUTE` | `60` |

**⚠️ Method A (Blueprint) பயன்படுத்தினால்:** `GEMINI_API_KEY` (`sync: false` என render.yaml-ல் குறிக்கப்பட்டுள்ளதால்) dashboard-ல் manual-ஆக set செய்ய Render கேட்கும் — மற்ற variables ஏற்கனவே render.yaml-லிருந்து auto-set ஆகி இருக்கும்.

Environment variable சேர்த்த பின் **Save Changes** click செய்யவும் — Render தானாக redeploy செய்யும்.

### Step 2.4 — Deployment Logs சரிபார்க்கவும்

**Logs** tab-ல்:
- `Starting AI NIFTY Option Analyzer Pro` தெரிய வேண்டும்
- Errors எதுவும் இல்லாமல் `Application startup complete` தெரிய வேண்டும்

Errors இருந்தால் **Troubleshooting** section பார்க்கவும் (கீழே).

### Step 2.5 — உங்கள் Live URL-ஐ Test செய்யவும்

Render, ஒரு URL தரும்: `https://ai-nifty-analyzer.onrender.com` போல.

1. அதை browser-ல் திறக்கவும் — Home page தெரிய வேண்டும்
2. `/dashboard` பக்கத்திற்குச் சென்று NIFTY/BANKNIFTY/FINNIFTY cards load ஆகிறதா பார்க்கவும்
3. **இப்போது `CORS_ALLOWED_ORIGINS`-ஐ actual URL-க்கு update செய்யவும்:**
   Environment tab-ல் `CORS_ALLOWED_ORIGINS` = `https://ai-nifty-analyzer.onrender.com` எனச் சரி செய்யவும் → Save

---

## Part 3 — Deploy-க்குப் பின் முக்கியமான குறிப்புகள்

### ⚠️ SQLite Data Ephemeral (Free Tier)

Render-ன் **free web service disk permanent இல்லை** — ஒவ்வொரு redeploy/restart-லும் `./data/analyzer.db`, `./logs/app.log` **அழிந்துவிடும்**. இது history/analysis tracking-ஐ பாதிக்கும்.

**தீர்வுகள் (தேர்ந்தெடுக்கவும்):**
1. **Render Persistent Disk** (paid plan தேவை) — `render.yaml`-ல் commented-out `disk:` block-ஐ uncomment செய்யவும்
2. **Render Postgres** (recommended, production-க்கு better) — Render dashboard-ல் **New +** → **PostgreSQL** உருவாக்கி, கிடைக்கும் "Internal Database URL"-ஐ `SQLITE_DB_PATH`-க்கு பதிலாக DB connection string-ஆக பயன்படுத்த `app/database.py`-ஐ Postgres driver (`asyncpg`)-க்கு switch செய்ய வேண்டும் (`CODE_REVIEW.md`-ல் இது open item-ஆக குறிப்பிடப்பட்டுள்ளது)

### ⚠️ NSE IP Block Risk on Shared Hosting

Render-ன் free/shared IP ranges-லிருந்து NSE-க்கு requests அனுப்பும்போது, **NSE அதிக அளவில் block செய்யக்கூடும்** (பல apps ஒரே IP range பகிர்வதால்). இது code bug இல்லை — hosting infrastructure-ன் இயல்பான limitation. Logs-ல் `NSEBlockedError` அடிக்கடி வந்தால், இதுவே காரணமாக இருக்கலாம்.

### Free Tier Cold Start

Render free web services 15 நிமிடம் idle-ஆக இருந்தால் **sleep** ஆகும் — அடுத்த request வரும்போது எழுப்ப ~30-50 விநாடிகள் ஆகும். Production-க்கு paid plan (`Starter` அல்லது மேல்) பரிந்துரைக்கப்படுகிறது.

### Auto-Deploy on Git Push

Default-ஆக, Render `main` branch-க்கு நீங்கள் push செய்யும் ஒவ்வொரு முறையும் **தானாக redeploy** செய்யும். இதை Render dashboard → **Settings** → **Auto-Deploy** toggle மூலம் மாற்றலாம்.

புதிய code மாற்றங்களை deploy செய்ய:
```bash
git add .
git commit -m "உங்கள் மாற்றம் பற்றிய குறிப்பு"
git push
```

---

## Troubleshooting

| பிரச்சனை | காரணம் / தீர்வு |
|---|---|
| Build fails: `ModuleNotFoundError` | `requirements.txt`-ல் அந்த package இல்லை — சேர்த்து மீண்டும் push செய்யவும் |
| `Application failed to respond` | Start command-ல் `$PORT` சரியாக இருக்கிறதா பார்க்கவும் — Render dynamic port assign செய்யும், hardcoded `8000` வேலை செய்யாது |
| `AIProviderError: No AI provider API key configured` | Environment tab-ல் `GEMINI_API_KEY` (அல்லது தேர்ந்தெடுத்த provider key) சரியாக set ஆகவில்லை |
| Dashboard cards load ஆகவில்லை, CORS error console-ல் | `CORS_ALLOWED_ORIGINS` உங்கள் actual Render URL-உடன் match ஆகிறதா சரிபார்க்கவும் |
| `NSEBlockedError` frequent-ஆக logs-ல் | மேலே "NSE IP Block Risk" பார்க்கவும் — hosting IP-ன் limitation |
| Restart-க்குப் பின் dashboard history காலியாக உள்ளது | மேலே "SQLite Data Ephemeral" பார்க்கவும் |

---

## Summary Checklist

- [ ] `git init` → `git add .` → `git commit`
- [ ] GitHub-ல் repo உருவாக்கி `git push`
- [ ] `.env` repo-வில் இல்லை என சரிபார்த்தது
- [ ] Render-ல் Blueprint அல்லது Manual Web Service உருவாக்கியது
- [ ] `GEMINI_API_KEY` (அல்லது தேர்ந்தெடுத்த provider) set செய்தது
- [ ] Deploy logs-ல் errors இல்லை என சரிபார்த்தது
- [ ] Live URL open ஆகி dashboard load ஆகிறது என சரிபார்த்தது
- [ ] `CORS_ALLOWED_ORIGINS`-ஐ actual URL-க்கு update செய்தது
