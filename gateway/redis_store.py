from __future__ import annotations

import asyncio
from pathlib import Path

import redis.asyncio as redis

LUA_DIR = Path(__file__).resolve().parent
MAX_CONNECTIONS = 400


class RedisStore:
    """Async Redis: every call awaits, so the event loop is never blocked
    while dashboard refreshes fan out cap/spent reads.

    Loop-aware client: asyncio connections bind to the loop that dialed
    them, and every `asyncio.run()` (each pytest, server startup) is a new
    loop. On loop change the client is rebuilt; the abandoned idle
    connections are reaped server-side. In prod the loop never changes, so
    this costs nothing and leaks nothing there.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._reserve_src = (LUA_DIR / "reserve.lua").read_text(encoding="utf-8")
        self._release_src = (LUA_DIR / "release.lua").read_text(encoding="utf-8")
        self._loop = None
        self.redis = None
        self._reserve = None
        self._release = None

    def _client(self):
        loop = asyncio.get_running_loop()
        if loop is not self._loop or self.redis is None:
            self.redis = redis.Redis.from_url(self._url, decode_responses=True, max_connections=MAX_CONNECTIONS)
            self._reserve = self.redis.register_script(self._reserve_src)
            self._release = self.redis.register_script(self._release_src)
            self._loop = loop
        return self.redis

    async def drop(self) -> None:
        """Forget client-side connections (e.g. end of bootstrap on a
        throwaway loop). Next use rebuilds on the calling loop."""
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                pass
        self.redis = None
        self._reserve = None
        self._release = None
        self._loop = None

    async def ping(self) -> None:
        await self._client().ping()

    async def reserve(self, path: list[str], amount: int, reasons: list[str]) -> tuple[bool, str, int]:
        self._client()
        keys: list[str] = []
        for node_id in path:
            keys.append(f"budget:{node_id}:cap")
            keys.append(f"budget:{node_id}:spent")
        raw = await self._reserve(keys=keys, args=[amount, ",".join(reasons)])
        ok = int(raw[0]) == 1
        reason = str(raw[1])
        remaining = int(raw[2])
        return ok, reason, remaining

    async def release(self, path: list[str], amount: int) -> None:
        self._client()
        keys = [f"budget:{node_id}:spent" for node_id in path]
        await self._release(keys=keys, args=[amount])

    async def get_int(self, key: str) -> int:
        value = await self._client().get(key)
        return int(value or 0)

    async def get_text(self, key: str) -> str | None:
        value = await self._client().get(key)
        return None if value is None else str(value)

    async def set_value(self, key: str, value: str) -> None:
        await self._client().set(key, value)

    async def incr(self, key: str) -> int:
        return int(await self._client().incr(key))

    async def flush(self) -> None:
        await self._client().flushdb()
