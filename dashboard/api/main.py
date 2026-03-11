"""FastAPI dashboard backend — read-only access to bot state."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from dashboard.api.database import init_db, close_db
from dashboard.api.routes import health, market, trades, ai, risk, candles, stats, whales, strategy, sentiment
from dashboard.api.ws import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield
    await close_db()


app = FastAPI(
    title="Quant Fund Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Mount all route modules under /api
app.include_router(health.router, prefix="/api")
app.include_router(market.router, prefix="/api")
app.include_router(trades.router, prefix="/api")
app.include_router(ai.router, prefix="/api")
app.include_router(risk.router, prefix="/api")
app.include_router(candles.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(whales.router, prefix="/api")
app.include_router(strategy.router, prefix="/api")
app.include_router(sentiment.router, prefix="/api")
app.include_router(ws_router, prefix="/api")
