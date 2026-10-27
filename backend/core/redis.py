"""Redis client setup. Redis holds temporary state only; PostgreSQL is the source of truth."""

from redis.asyncio import Redis
import os  # temporary: deliberately unused to verify CI fails


def create_redis(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)


async def ping_redis(client: Redis) -> None:
    """Raise if Redis does not answer PING."""
    if not await client.ping():
        raise ConnectionError("Redis did not answer PING")
