from aiogram import Bot

async def check_subscription(bot: Bot, user_id: int, required_chat_ids: list[int]) -> bool:
    if not required_chat_ids:
        return True
    for chat_id in required_chat_ids:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            # can't verify -> treat as not subscribed (safer)
            return False
    return True
