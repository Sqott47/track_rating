from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

# In-memory TTL cache: (user_id, chat_id) -> (ok, expires_at)
_CACHE: Dict[Tuple[int, int], Tuple[bool, float]] = {}

# Resolve cache: username -> (chat_id, expires_at)
_RESOLVE_CACHE: Dict[str, Tuple[int, float]] = {}

@dataclass(frozen=True)
class SubscriptionCheckResult:
    ok: bool
    reason: str | None = None  # 'not_member' | 'cant_verify' | 'rate_limited'

async def check_subscription(
    bot: Bot,
    user_id: int,
    required_chat_ids: List[int],
    required_chat_usernames: Optional[List[str]] = None,
    *,
    ttl_seconds: int = 10 * 60,
) -> SubscriptionCheckResult:
    required_chat_usernames = required_chat_usernames or []

    if not required_chat_ids and not required_chat_usernames:
        return SubscriptionCheckResult(ok=True)

    now = time.time()

    # Resolve public usernames to chat IDs (best effort; requires public @username).
    resolved_ids: List[int] = list(required_chat_ids or [])
    for uname in required_chat_usernames:
        u = (uname or "").strip().lstrip("@").lower()
        if not u:
            continue

        cached = _RESOLVE_CACHE.get(u)
        if cached and cached[1] > now:
            resolved_ids.append(int(cached[0]))
            continue

        try:
            chat = await bot.get_chat(chat_id=f"@{u}")
            cid = int(chat.id)
            _RESOLVE_CACHE[u] = (cid, now + 10 * 60)
            resolved_ids.append(cid)
        except TelegramRetryAfter:
            return SubscriptionCheckResult(ok=False, reason="rate_limited")
        except (TelegramForbiddenError, TelegramBadRequest):
            return SubscriptionCheckResult(ok=False, reason="cant_verify")
        except Exception:
            return SubscriptionCheckResult(ok=False, reason="cant_verify")

    # De-dup while preserving order
    seen: set[int] = set()
    ordered_ids: List[int] = []
    for cid in resolved_ids:
        cid_int = int(cid)
        if cid_int in seen:
            continue
        seen.add(cid_int)
        ordered_ids.append(cid_int)

    for chat_id in ordered_ids:
        key = (int(user_id), int(chat_id))
        cached = _CACHE.get(key)
        if cached and cached[1] > now:
            if not cached[0]:
                return SubscriptionCheckResult(ok=False, reason="not_member")
            continue

        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            ok = member.status in ("member", "administrator", "creator")
            _CACHE[key] = (ok, now + ttl_seconds)
            if not ok:
                return SubscriptionCheckResult(ok=False, reason="not_member")
        except TelegramRetryAfter:
            # do not permanently poison cache; short TTL
            _CACHE[key] = (False, now + 15)
            return SubscriptionCheckResult(ok=False, reason="rate_limited")
        except (TelegramForbiddenError, TelegramBadRequest):
            # Bot can't verify (not enough rights / invalid chat). Safer: deny with clear reason.
            _CACHE[key] = (False, now + 60)
            return SubscriptionCheckResult(ok=False, reason="cant_verify")
        except Exception:
            _CACHE[key] = (False, now + 60)
            return SubscriptionCheckResult(ok=False, reason="cant_verify")

    return SubscriptionCheckResult(ok=True)
