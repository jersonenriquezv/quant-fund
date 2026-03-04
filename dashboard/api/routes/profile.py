"""Strategy profile endpoints — read and switch profiles."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from dashboard.api import database as db

router = APIRouter()

# Redis key for active profile
PROFILE_KEY = "qf:bot:strategy_profile"

# Profile definitions (must match config/settings.py STRATEGY_PROFILES)
AVAILABLE_PROFILES = {
    "default": {
        "label": "Default",
        "description": "Conservative SMC strategy (~1-2 setups/day)",
        "color": "#22c55e",  # green
    },
    "aggressive": {
        "label": "Aggressive",
        "description": "Wider zones, lower thresholds (~3-5 setups/day)",
        "color": "#eab308",  # yellow
    },
    "scalping": {
        "label": "Scalping",
        "description": "LTF-only trades, equilibrium allowed (~10-20+ setups/day)",
        "color": "#ef4444",  # red
    },
}


class ProfileResponse(BaseModel):
    active: str
    profiles: dict


class ProfileSetRequest(BaseModel):
    profile: str


@router.get("/profile", response_model=ProfileResponse)
async def get_profile():
    """Get active profile and all available profiles."""
    active = "default"
    try:
        if db.redis_client:
            stored = await db.redis_client.get(PROFILE_KEY)
            if stored and stored in AVAILABLE_PROFILES:
                active = stored
    except Exception:
        pass

    return ProfileResponse(active=active, profiles=AVAILABLE_PROFILES)


@router.post("/profile", response_model=ProfileResponse)
async def set_profile(req: ProfileSetRequest):
    """Switch strategy profile. Takes effect on next candle evaluation."""
    if req.profile not in AVAILABLE_PROFILES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown profile: {req.profile}. "
                   f"Available: {', '.join(AVAILABLE_PROFILES.keys())}",
        )

    if not db.redis_client:
        raise HTTPException(status_code=503, detail="Redis not available")

    await db.redis_client.set(PROFILE_KEY, req.profile)

    return ProfileResponse(active=req.profile, profiles=AVAILABLE_PROFILES)
