import aiohttp
from typing import Any, Dict, List, Optional

class TrackRaterAPI:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {"X-Bot-Token": self.token}

    async def create_submission(self, *, tg_user_id: int, tg_username: str | None, filename: str, ext: str, file_bytes: bytes) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions"
        data = aiohttp.FormData()
        data.add_field("tg_user_id", str(tg_user_id))
        data.add_field("tg_username", tg_username or "")
        data.add_field("original_filename", filename)
        data.add_field("original_ext", ext)
        data.add_field("file", file_bytes, filename=filename, content_type="application/octet-stream")
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, headers=self._headers(), timeout=60) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater create_submission failed: {resp.status} {text}")
                return await resp.json()

    async def set_metadata(self, submission_id: int, artist: str, title: str) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/metadata"
        payload = {"artist": artist, "title": title}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(), timeout=30) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater set_metadata failed: {resp.status} {text}")
                return await resp.json()

    async def enqueue_free(self, submission_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/enqueue_free"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={}, headers=self._headers(), timeout=120) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater enqueue_free failed: {resp.status} {text}")
                return await resp.json()

    async def set_waiting_payment(self, submission_id: int, priority: int, provider: str | None = None, provider_ref: str | None = None) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/waiting_payment"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"priority": priority, **({"provider": provider} if provider else {}), **({"provider_ref": provider_ref} if provider_ref else {})}, headers=self._headers(), timeout=30) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater set_waiting_payment failed: {resp.status} {text}")
                return await resp.json()

    async def mark_paid(self, submission_id: int, *, provider: str, provider_ref: str, amount: int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/mark_paid"
        payload = {"provider": provider, "provider_ref": provider_ref, "amount": amount}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self._headers(), timeout=120) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater mark_paid failed: {resp.status} {text}")
                return await resp.json()

    async def my_queue(self, tg_user_id: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/api/tg/my_queue"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params={"tg_user_id": str(tg_user_id)}, headers=self._headers(), timeout=30) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"TrackRater my_queue failed: {resp.status} {text}")
                return await resp.json()

    async def cancel_submission(self, submission_id: int) -> Dict[str, Any]:
        """Best-effort cancellation/cleanup used by "Отмена" button."""
        url = f"{self.base_url}/api/tg/submissions/{submission_id}/cancel"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={}, headers=self._headers(), timeout=30) as resp:
                # even if backend says not found - treat as ok
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"TrackRater cancel_submission failed: {resp.status} {text}")
                return await resp.json()
