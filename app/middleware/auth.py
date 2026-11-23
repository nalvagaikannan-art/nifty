"""Single-origin HTTP Basic Auth for the standalone NIFTY-PRO deployment."""
import base64
import binascii
import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.config import settings


class AppAuthMiddleware(BaseHTTPMiddleware):
    """Protect all app surfaces; leave only the non-sensitive health probe public."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz" or request.method == "OPTIONS":
            return await call_next(request)

        username = settings.app_auth_username
        password = settings.app_auth_password
        if not username or not password:
            if settings.env.lower() == "production":
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Application authentication is not configured."},
                )
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token:
            return self._challenge()

        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
            supplied_user, separator, supplied_password = decoded.partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return self._challenge()

        if (
            not separator
            or not hmac.compare_digest(supplied_user, username)
            or not hmac.compare_digest(supplied_password, password)
        ):
            return self._challenge()

        return await call_next(request)

    @staticmethod
    def _challenge():
        return Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="NIFTY-PRO", charset="UTF-8"'},
        )
