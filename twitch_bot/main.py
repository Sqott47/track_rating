import asyncio
import os
from typing import Dict, Optional

from fastapi import FastAPI, Request, HTTPException
from twitchio.ext import commands


def _split_channels(value: str) -> list[str]:
    chans = []
    for part in (value or "").split(","):
        c = part.strip().lstrip("#")
        if c:
            chans.append(c)
    # unique preserving order
    out = []
    seen = set()
    for c in chans:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out


TWITCH_TOKEN = (os.getenv("TWITCH_TOKEN") or "").strip()
TWITCH_NICK = (os.getenv("TWITCH_NICK") or "").strip()
TWITCH_CHANNELS = _split_channels(os.getenv("TWITCH_CHANNELS") or os.getenv("TWITCH_CHANNEL") or "")
WEBHOOK_SECRET = (os.getenv("TWITCH_BOT_WEBHOOK_SECRET") or "").strip()

if not TWITCH_TOKEN:
    raise RuntimeError("TWITCH_TOKEN is required")
if not TWITCH_NICK:
    raise RuntimeError("TWITCH_NICK is required")
if not TWITCH_CHANNELS:
    raise RuntimeError("TWITCH_CHANNELS (or TWITCH_CHANNEL) is required")


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            token=TWITCH_TOKEN,
            prefix="!",
            initial_channels=TWITCH_CHANNELS,
            nick=TWITCH_NICK,
        )

    async def event_ready(self):
        print(f"[twitch_bot] ready as {self.nick}; channels={TWITCH_CHANNELS}")


bot = Bot()
app = FastAPI()

# anti-duplicate: per channel -> last track_id announced
last_sent: Dict[str, int] = {}


async def _send_to_channel(channel: str, message: str):
    chan_obj = bot.get_channel(channel)
    if chan_obj is None:
        # give twitchio a moment after startup
        await asyncio.sleep(0.5)
        chan_obj = bot.get_channel(channel)
    if chan_obj is None:
        print(f"[twitch_bot] warn: channel not joined: {channel}")
        return
    await chan_obj.send(message)


@app.post("/track-changed")
async def track_changed(req: Request):
    if WEBHOOK_SECRET:
        got = req.headers.get("X-Webhook-Secret") or ""
        if got != WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="bad secret")

    data = await req.json()
    channel = (data.get("channel") or "").strip().lstrip("#")
    if not channel:
        channel = TWITCH_CHANNELS[0]
    if channel not in TWITCH_CHANNELS:
        # do not spam random channels; require explicit join via config
        raise HTTPException(status_code=400, detail="channel not configured")

    try:
        track_id = int(data.get("track_id"))
    except Exception:
        raise HTTPException(status_code=400, detail="bad track_id")

    rate_url = (data.get("rate_url") or "").strip()
    track_name = (data.get("track_name") or "").strip()
    if not rate_url:
        raise HTTPException(status_code=400, detail="missing rate_url")

    if last_sent.get(channel) == track_id:
        return {"ok": True, "skipped": "duplicate"}
    last_sent[channel] = track_id

    if track_name:
        text = f"🎧 Сейчас оцениваем: {track_name} — {rate_url}"
    else:
        text = f"🎧 Сейчас оцениваем: {rate_url}"

    bot.loop.create_task(_send_to_channel(channel, text))
    return {"ok": True}


def main():
    import uvicorn

    port = int(os.getenv("BOT_PORT") or "5055")
    host = os.getenv("BOT_HOST") or "127.0.0.1"

    bot.loop.create_task(bot.start())
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
