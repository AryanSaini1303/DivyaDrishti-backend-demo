import os
import re
import uuid
import decimal
import datetime
import asyncpg
from dotenv import load_dotenv

load_dotenv()

_pool = None

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create)\b", re.IGNORECASE)
MAX_ROWS = int(os.getenv("DB_MAX_ROWS", 200))
STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", 5000))


def _jsonable(value):
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    return value


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL"),
            min_size=1,
            max_size=10,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def execute_readonly_sql(sql: str):
    stripped = sql.strip().rstrip(";")
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return {"error": "Only SELECT statements are allowed."}
    if FORBIDDEN.search(stripped):
        return {"error": "Statement contains a disallowed keyword."}
    if "limit" not in stripped.lower():
        stripped += f" LIMIT {MAX_ROWS}"

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            rows = await conn.fetch(stripped)
            safe_rows = [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]
            return {
                "rows": safe_rows,
                "row_count": len(safe_rows),
                "truncated": len(safe_rows) == MAX_ROWS,
            }
    except Exception as e:
        return {"error": str(e)}