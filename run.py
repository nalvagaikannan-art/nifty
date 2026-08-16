import os
import uvicorn
from app.main import app

if __name__ == "__main__":
    # Render (and most PaaS platforms) inject the port to bind via the PORT
    # env var — it changes per deploy, so it must never be hardcoded.
    # `reload=True` is a dev-only convenience and should not run in production
    # (it spawns a file-watcher process and reloads on every code change).
    port = int(os.environ.get("PORT", 8000))
    is_dev = os.environ.get("ENV", "development") == "development"
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=is_dev)
