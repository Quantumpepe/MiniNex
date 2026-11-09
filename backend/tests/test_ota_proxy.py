"""Offline tests for the centralized MiniNex OTA release cache."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.ota_proxy import (
    CACHE_SECONDS, ERROR_RETRY_SECONDS, OtaReleaseCache, ReleaseUnavailable,
)

SHA = "b" * 40
ABI = "a" * 64
CHECKSUM = "c" * 64
TAG = "mininex-ota-v0.8.195"


def release():
    return {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-11-10T00:00:00Z",
        "target_commitish": SHA,
        "body": f"native-abi: {ABI}\nsource-commit: {SHA}\nsha256: {CHECKSUM}\n",
        "assets": [{
            "name": "mininex-web.zip",
            "size": 100000,
            "digest": "sha256:" + CHECKSUM,
            "browser_download_url":
                f"https://github.com/Quantumpepe/MiniNex/releases/download/{TAG}/mininex-web.zip",
        }],
        "secret_field": "must-not-appear",
    }


class TestOtaCache(unittest.IsolatedAsyncioTestCase):
    async def test_many_devices_use_one_upstream_request(self):
        clock = [100.]
        cache = OtaReleaseCache(clock=lambda: clock[0])
        response = httpx.Response(200, json=release())
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=response) as get:
            result = await asyncio.gather(*(cache.latest() for _ in range(25)))
            self.assertEqual(get.await_count, 1)
            self.assertTrue(all(not stale for _, stale in result))
            self.assertNotIn("secret_field", result[0][0])
            self.assertEqual(result[0][0]["assets"][0]["digest"], "sha256:" + CHECKSUM)
            clock[0] += CACHE_SECONDS + 1
            await cache.latest()
            self.assertEqual(get.await_count, 2)

    async def test_rate_limit_keeps_verified_cached_metadata(self):
        clock = [50.]
        cache = OtaReleaseCache(clock=lambda: clock[0])
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as get:
            get.return_value = httpx.Response(200, json=release())
            await cache.latest()
            clock[0] += CACHE_SECONDS + 1
            get.return_value = httpx.Response(403, json={"message": "API rate limit exceeded"})
            previous, stale = await cache.latest()
            self.assertTrue(stale)
            self.assertEqual(previous["tag_name"], TAG)
            await cache.latest()
            self.assertEqual(get.await_count, 2)
            clock[0] += ERROR_RETRY_SECONDS + 1
            get.return_value = httpx.Response(200, json=release())
            _, stale = await cache.latest()
            self.assertFalse(stale)
            self.assertEqual(get.await_count, 3)

    async def test_empty_cache_fails_closed_on_error(self):
        clock = [200.]
        cache = OtaReleaseCache(clock=lambda: clock[0])
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as get:
            get.return_value = httpx.Response(429, json={"message": "rate limited"})
            with self.assertRaises(ReleaseUnavailable):
                await cache.latest()
            with self.assertRaises(ReleaseUnavailable):
                await cache.latest()
            self.assertEqual(get.await_count, 1)

    async def test_malformed_metadata_not_cached(self):
        cache = OtaReleaseCache(clock=lambda: 100.)
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as get:
            get.return_value = httpx.Response(200, json={"tag_name": "wrong", "assets": []})
            with self.assertRaises(ReleaseUnavailable):
                await cache.latest()


if __name__ == "__main__":
    unittest.main()
