import logging
import os
import re
import sys
from app.config import settings


class SecretRedactionFilter(logging.Filter):
    """Redacts configured API key values (and anything that looks like a long
    API-key-shaped token) out of log records before they're written anywhere.

    Without this, something like `logger.error(f"AI API error: {e}")` could
    leak key material if an SDK's exception message ever echoes back the
    key it was called with (some HTTP client libraries include the request
    URL/headers in error text, and a key passed as a query param or in a
    stringified request object would show up in plaintext logs).
    """

    # Known key-shaped patterns as a defense-in-depth backstop, in addition to
    # redacting the exact configured key values below.
    _GENERIC_KEY_PATTERN = re.compile(
        r"(sk-[a-zA-Z0-9]{16,}|AIza[a-zA-Z0-9_\-]{16,}|Bearer\s+[a-zA-Z0-9._\-]{16,})"
    )

    def __init__(self):
        super().__init__()
        self._known_secrets = [
            v for v in (
                settings.gemini_api_key,
                settings.openai_api_key,
                settings.deepseek_api_key,
            ) if v
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = msg
        for secret in self._known_secrets:
            if secret and secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        redacted = self._GENERIC_KEY_PATTERN.sub("***REDACTED***", redacted)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging():
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    redaction_filter = SecretRedactionFilter()

    # logs/ is .gitignored (only the placeholder file inside it is tracked
    # elsewhere), so on a fresh `git clone` — e.g. Render's build step — the
    # directory itself won't exist yet. FileHandler() does NOT create parent
    # directories, so without this the very first log line at startup raises
    # FileNotFoundError and the app never comes up. Same reasoning applies to
    # the sqlite data dir, so both are ensured here defensively.
    log_dir = os.path.dirname(settings.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    db_dir = os.path.dirname(settings.sqlite_db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    handlers = [
        logging.FileHandler(settings.log_file),
        logging.StreamHandler(sys.stdout),
    ]
    for h in handlers:
        h.addFilter(redaction_filter)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    # Silence noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
