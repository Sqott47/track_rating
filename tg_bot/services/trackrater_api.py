from __future__ import annotations

import aiohttp
from typing import Any, Dict, List, Optional

class TrackRaterAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {"X-Bot-Token": self.token}

    async def _raise_for(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status >= 400:
            text = await resp.text()
            raise RuntimeError(f"TrackRater API error: {resp.status} {text}")

    async def create_submission(
        self,
        *,
        tg_user_id: int,
        tg_username: str | None,
        filename: str,
        ext: str,
        file_bytes: bytes,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions"
        data = aiohttp.FormData()
        data.add_field("tg_user_id", str(int(tg_user_id)))
        data.add_field("tg_username", (tg_username or "").strip())
        data.add_field("filename", filename)
        data.add_field("ext", ext)
        data.add_field("file", file_bytes, filename=filename, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, headers=self._headers(), timeout=180) as resp:
                await self._raise_for(resp)
                return await resp.json()

    async def set_metadata(self, submission_id: int, *, artist: str, title: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/metadata"
        payload = {"artist": artist, "title": title}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(), timeout=30) as resp:
                await self._raise_for(resp)
                return await resp.json()

    async def enqueue_free(self, submission_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/enqueue_free"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={}, headers=self._headers(), timeout=180) as resp:
                await self._raise_for(resp)
                return await resp.json()

    async def set_waiting_payment(
        self,
        submission_id: int,
        *,
        priority: int,
        provider: str = "donationalerts",
        provider_ref: str | None = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/waiting_payment"
        payload: Dict[str, Any] = {"priority": int(priority), "provider": provider}
        if provider_ref:
            payload["provider_ref"] = provider_ref
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(), timeout=30) as resp:
                await self._raise_for(resp)
                return await resp.json()

    async def my_queue(self, tg_user_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/tg/my_queue"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"tg_user_id": str(int(tg_user_id))}, headers=self._headers(), timeout=30) as resp:
                await self._raise_for(resp)
                return await resp.json()

    async def cancel_submission(self, submission_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/cancel"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={}, headers=self._headers(), timeout=30) as resp:
                await self._raise_for(resp)
                return await resp.json()
