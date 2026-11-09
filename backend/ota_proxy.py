"""Public, bounded, server-side cache for the MiniNex OTA GitHub Release.

One Render process checks GitHub at most once per 5 minutes, regardless of the
number of phones. No GitHub token, wallet secret or trading endpoint is exposed.
"""
import asyncio
import os
import time

import httpx

RELEASE_URL = "https://api.github.com/repos/Quantumpepe/MiniNex/releases/latest"
CACHE_SECONDS = 5 * 60
ERROR_RETRY_SECONDS = 2 * 60
MAX_RESPONSE_BYTES = 1024 * 1024


class ReleaseUnavailable(Exception):
    """No valid cached release and GitHub is temporarily unavailable."""


class OtaReleaseCache:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = asyncio.Lock()
        self._release = None
        self._next_check = 0.0
        self._last_error = None

    async def latest(self):
        now = self._clock()
        if now < self._next_check:
            if self._release is not None:
                return self._release, now < self._next_check and self._last_error is not None
            raise ReleaseUnavailable()
        async with self._lock:
            now = self._clock()
            if now < self._next_check:
                if self._release is not None:
                    return self._release, self._last_error is not None
                raise ReleaseUnavailable()
            try:
                headers = {"Accept": "application/vnd.github+json", "User-Agent": "MiniNex-OTA-Render"}
                # Optional backend-only token. Never return it to Android or store in Git.
                token = os.getenv("MININEX_GITHUB_RELEASE_TOKEN", "").strip()
                if token:
                    headers["Authorization"] = "Bearer " + token
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                    resp = await client.get(RELEASE_URL, headers=headers)
                if resp.status_code != 200:
                    raise ReleaseUnavailable()
                if len(resp.content) > MAX_RESPONSE_BYTES:
                    raise ReleaseUnavailable()
                raw = resp.json()
                if not isinstance(raw, dict) or not isinstance(raw.get("assets"), list):
                    raise ReleaseUnavailable()
                # Preserve ONLY the GitHub Release metadata required by the
                # existing strict native-ABI, source-commit and SHA256 verifier.
                release = {
                    "tag_name": raw.get("tag_name"),
                    "draft": raw.get("draft"),
                    "prerelease": raw.get("prerelease"),
                    "published_at": raw.get("published_at"),
                    "target_commitish": raw.get("target_commitish"),
                    "body": raw.get("body"),
                    "assets": [{
                        "name": a.get("name"), "size": a.get("size"),
                        "browser_download_url": a.get("browser_download_url"),
                        "digest": a.get("digest"),
                    } for a in raw["assets"] if isinstance(a, dict) and a.get("name") == "mininex-web.zip"],
                }
                if not isinstance(release["tag_name"], str) or not release["assets"]:
                    raise ReleaseUnavailable()
                self._release = release
                self._last_error = None
                self._next_check = self._clock() + CACHE_SECONDS
                return release, False
            except (httpx.HTTPError, ValueError, TypeError, ReleaseUnavailable):
                self._last_error = "upstream"
                self._next_check = self._clock() + ERROR_RETRY_SECONDS
                # Serve the last successfully obtained release during API
                # rate limits or temporary outages. Client still re-verifies it.
                if self._release is not None:
                    return self._release, True
                raise ReleaseUnavailable() from None


ota_release_cache = OtaReleaseCache()
