"""Authentication and input validation dependencies for the dashboard API."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config.settings import settings

_bearer = HTTPBearer()


async def require_api_key(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """Verify the bearer token matches DASHBOARD_API_KEY.

    Applied to write endpoints (POST/PATCH/DELETE/PUT) that affect trades or balances.
    Read-only endpoints remain open (behind Tailscale).
    """
    if not settings.DASHBOARD_API_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DASHBOARD_API_KEY not configured — write operations disabled",
        )
    if creds.credentials != settings.DASHBOARD_API_KEY:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key")
    return creds.credentials


def validate_pair(pair: str) -> str:
    """Validate that pair is in the configured TRADING_PAIRS list."""
    if pair not in settings.TRADING_PAIRS:
        raise HTTPException(400, f"Unknown pair: {pair}")
    return pair
