"""
HealthProbe — 模型健康探测
=========================
在路由前主动探测每个模型的 API 端点是否可达。
30s 缓存，避免每次路由都发请求。

集成点: model_router.route() -> 过滤掉 unhealthy 模型
CLI: /health 命令查看各模型状态
"""
import asyncio, time, logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("core.health_probe")

MODEL_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/v1/models",
    "mimo": "https://api.xiaomimimo.com/v1/models",
}

PROBE_TIMEOUT = 5
CACHE_TTL = 30
MAX_RETRIES = 1


@dataclass
class ProbeResult:
    healthy: bool
    latency_ms: float
    error: str = ""
    checked_at: float = 0.0


class HealthProbe:
    def __init__(self):
        self._cache: dict[str, ProbeResult] = {}
        self._lock = asyncio.Lock()
        self._session = None

    async def _get_session(self):
        """Fresh session per probe — no cross-loop contamination."""
        import aiohttp
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT)
        )

    def _cached(self, model: str):
        r = self._cache.get(model)
        if r and time.time() - r.checked_at < CACHE_TTL:
            return r
        return None

    async def probe(self, model: str, force: bool = False) -> ProbeResult:
        if not force:
            cached = self._cached(model)
            if cached:
                return cached

        endpoint = MODEL_ENDPOINTS.get(model)
        if not endpoint:
            return ProbeResult(healthy=False, latency_ms=0,
                              error=f"unknown model: {model}",
                              checked_at=time.time())

        session = await self._get_session()
        try:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    t0 = time.time()
                    async with session.get(endpoint) as resp:
                        latency = (time.time() - t0) * 1000
                        if resp.status < 500:
                            result = ProbeResult(
                                healthy=True, latency_ms=round(latency, 1),
                                checked_at=time.time())
                            self._cache[model] = result
                            return result
                        else:
                            result = ProbeResult(
                                healthy=False, latency_ms=round(latency, 1),
                                error=f"HTTP {resp.status}",
                                checked_at=time.time())
                except Exception as e:
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(0.5)
                        continue
                    result = ProbeResult(
                        healthy=False, latency_ms=0,
                        error=f"{type(e).__name__}: {e}",
                        checked_at=time.time())

            self._cache[model] = result
            return result
        finally:
            await session.close()

    async def probe_all(self, force: bool = False) -> dict[str, ProbeResult]:
        models = list(MODEL_ENDPOINTS.keys())
        tasks = [self.probe(m, force) for m in models]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out = {}
        for model, r in zip(models, results):
            if isinstance(r, Exception):
                out[model] = ProbeResult(
                    healthy=False, latency_ms=0,
                    error=f"{type(r).__name__}: {r}",
                    checked_at=time.time())
            else:
                out[model] = r
        return out

    def is_healthy(self, model: str) -> bool:
        cached = self._cached(model)
        return cached is not None and cached.healthy

    def get_unhealthy(self) -> list[str]:
        return [m for m in MODEL_ENDPOINTS if not self.is_healthy(m)]

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None


_probe = None

def get_health_probe() -> HealthProbe:
    global _probe
    if _probe is None:
        _probe = HealthProbe()
    return _probe
