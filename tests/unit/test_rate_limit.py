import asyncio

from src.core.rate_limit import RedisRateLimiter


def test_rate_limiter_falls_back_when_redis_is_unavailable(monkeypatch):
    limiter = RedisRateLimiter("test", limit=1, window_seconds=60)

    async def unavailable(*_args, **_kwargs):
        raise OSError("unavailable")

    class BrokenRedis:
        @classmethod
        def from_url(cls, *_args, **_kwargs):
            class Client:
                incr = unavailable
                async def aclose(self):
                    pass
            return Client()

    monkeypatch.setattr("src.core.rate_limit.Redis", BrokenRedis)
    assert asyncio.run(limiter.allow("user")) == (True, 0)
    allowed, retry = asyncio.run(limiter.allow("user"))
    assert not allowed and retry > 0
