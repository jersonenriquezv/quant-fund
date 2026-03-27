"""Database connections — asyncpg (PostgreSQL) + redis.asyncio."""

import os

import asyncpg
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", "config", ".env"))

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'jer')}"
    f":{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":{os.getenv('POSTGRES_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB', 'quant_fund')}"
)

_REDIS_PASS = os.getenv('REDIS_PASSWORD', '')
_REDIS_AUTH = f":{_REDIS_PASS}@" if _REDIS_PASS else ""
REDIS_URL = f"redis://{_REDIS_AUTH}{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}"

pg_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


async def init_db() -> None:
    global pg_pool, redis_client
    pg_pool = await asyncpg.create_pool(PG_DSN, min_size=2, max_size=10)
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)


async def close_db() -> None:
    global pg_pool, redis_client
    if pg_pool:
        await pg_pool.close()
    if redis_client:
        await redis_client.aclose()
