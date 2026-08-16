from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from app.config import settings
from app.utils import health_metrics
import os, re, logging

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/config")
async def get_config():
    return {
        "ai_provider": settings.ai_provider,
        "cache_ttl": settings.cache_ttl,
        "log_level": settings.log_level,
        "angel_configured": bool(getattr(settings, "angel_api_key", None)),
        "angel_client_id": getattr(settings, "angel_client_id", None),
        "zerodha_configured": bool(
            getattr(settings, "kite_api_key", None) and getattr(settings, "kite_access_token", None)
        ),
    }


@router.get("/health")
async def get_health():
    """
    Rolling data-source health (CODE_REVIEW.md #19/#20) — NSE/Angel One
    block-rate over the last ~50 calls this process has made. Not a
    substitute for real uptime monitoring across a fleet, but answers
    "is our IP getting blocked right now" without grepping logs.
    """
    metrics = health_metrics.snapshot()
    return {
        "sources": metrics,
        "alert_webhook_configured": bool(settings.alert_webhook_url),
        "redis_configured": bool(settings.redis_url),
        "database": "postgres" if settings.database_url else "sqlite",
    }


class AngelCredsPayload(BaseModel):
    angel_api_key: Optional[str] = None
    angel_client_id: Optional[str] = None
    angel_password: Optional[str] = None
    angel_totp_secret: Optional[str] = None


@router.post("/angel")
async def save_angel_creds(payload: AngelCredsPayload):
    """
    Angel One credentials-ஐ .env file-ல் write செய்யும்.
    Production-ல் Render env vars மூலம் set செய்வது நல்லது.
    """
    env_path = ".env"
    updates = {
        "ANGEL_API_KEY":     payload.angel_api_key,
        "ANGEL_CLIENT_ID":   payload.angel_client_id,
        "ANGEL_PASSWORD":    payload.angel_password,
        "ANGEL_TOTP_SECRET": payload.angel_totp_secret,
    }
    updates = {k: v for k, v in updates.items() if v}

    if not updates:
        raise HTTPException(status_code=400, detail="No credentials provided")

    try:
        # Read existing .env
        existing = {}
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
            for line in lines:
                m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
                if m:
                    existing[m.group(1)] = m.group(2)

        # Update values
        existing.update(updates)

        # Rewrite .env
        with open(env_path, "w") as f:
            for k, v in existing.items():
                f.write(f"{k}={v}\n")

        # Update live settings object (takes effect without restart)
        for k, v in updates.items():
            attr = k.lower()
            setattr(settings, attr, v)

        logger.info(f"Angel One credentials saved to .env: {list(updates.keys())}")
        return {"status": "saved", "keys": list(updates.keys())}

    except PermissionError:
        raise HTTPException(
            status_code=501,
            detail="manual — .env file write permission இல்லை. Render dashboard-ல் env vars set செய்யவும்."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save error: {e}")
