from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Tuple, List

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

# In-memory TTL cache: (user_id, chat_id) -> (ok, expires_at)
_CACHE: Dict[Tuple[int, int], Tuple[bool, float]] = {}

@dataclass(frozen=True)
class SubscriptionCheckResult:
    ok: bool
    reason: str | None = None  # 'not_member' | 'cant_verify' | 'rate_limited'

async def check_subscription(bot: Bot, user_id: int, required_chat_ids: List[int], *, ttl_seconds: int = 10 * 60) -> SubscriptionCheckResult:
    if not required_chat_ids:
        return SubscriptionCheckResult(ok=True)

    now = time.time()

    for chat_id in required_chat_ids:
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
