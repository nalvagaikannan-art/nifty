# Architecture

- **Backend**: FastAPI, SQLite (async), Pandas/Numpy for calculations.
- **Frontend**: Jinja2 templates, Plotly.js, vanilla JS.
- **Data**: NSE public APIs (scraping with httpx).
- **AI**: Plug-and-play providers (Gemini, OpenAI, DeepSeek) via environment variable.
- **Caching**: In-memory TTL cache to reduce API calls.
- **Error Handling**: Centralized exceptions, logging with Loguru.

## Modules
- `data_fetcher`: Fetches live data.
- `market_analyzer`: Orchestrates data aggregation and metrics.
- `option_analyzer`: Option-specific calculations.
- `technical_indicators`: S/R, trend, RSI, MACD.
- `ai_engine`: Prompt engineering and AI response parsing.
- `risk_manager`: Risk assessment.

## API Routes
All under `/api/` with clear separation.

## Security
API keys via .env, never hardcoded.
