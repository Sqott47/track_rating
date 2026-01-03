import os
from dataclasses import dataclass
from typing import List
from dotenv import load_dotenv

load_dotenv()

def _split_csv(v: str) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]

@dataclass(frozen=True)
class Settings:
    bot_token: str
    trackrater_base_url: str
    trackrater_bot_token: str
    required_chat_ids: List[int]
    sponsor_links: List[str]
    donationalerts_base_url: str
    allowed_exts: List[str]
    fsm_ttl_seconds: int = 30 * 60  # 30 minutes

def load_settings() -> Settings:
    bot_token = os.getenv("TG_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("TG_BOT_TOKEN is required")

    base_url = os.getenv("TRACKRATER_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("TRACKRATER_BASE_URL is required")

    api_token = os.getenv("TRACKRATER_TG_BOT_TOKEN", "").strip()
    if not api_token:
        raise RuntimeError("TRACKRATER_TG_BOT_TOKEN is required")

    required_chats = [int(x) for x in _split_csv(os.getenv("TG_REQUIRED_CHAT_IDS", ""))]  # ids of channels
    sponsor_links = _split_csv(os.getenv("TG_SPONSOR_LINKS", ""))  # urls to channels/chats
    da_url = os.getenv("DONATIONALERTS_URL", "").strip()

    allowed_exts = _split_csv(os.getenv("TG_ALLOWED_EXTS", "mp3,wav,flac,aiff,aif,ogg,m4a"))

    return Settings(
        bot_token=bot_token,
        trackrater_base_url=base_url,
        trackrater_bot_token=api_token,
        required_chat_ids=required_chats,
        sponsor_links=sponsor_links,
        donationalerts_base_url=da_url,
        allowed_exts=allowed_exts,
    )
