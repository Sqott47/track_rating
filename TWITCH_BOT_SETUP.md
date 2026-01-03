# Twitch bot integration (auto-post track review link)

This repo now contains an optional Twitch chat bot that posts a link to the
current track page each time the streamer activates a new submission.

The bot runs as a **separate process** on the same server, so it won't interfere
with Flask/Socket.IO.

## 1) Configure the main app

In the main app environment, set:

```bash
# Where your site is reachable from the internet (recommended behind proxy)
PUBLIC_BASE_URL=https://your-domain.com

# Where the bot webhook listens (same server)
TWITCH_BOT_WEBHOOK_URL=http://127.0.0.1:5055/track-changed

# Optional shared secret; must match the bot
TWITCH_BOT_WEBHOOK_SECRET=change_me

# Default Twitch channel to post into (without #)
TWITCH_NOTIFY_CHANNEL=antigaz
```

When a track is activated via the panel, the server will call the bot and pass:
`track_id`, `track_name`, and the external link to the public track page
(with `#reviews` anchor).

Note: the track page is public, but leaving a review still requires login.

## 2) Run the bot

The bot code lives in `twitch_bot/`.

```bash
cd twitch_bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# set env (or copy .env.example and export)
export TWITCH_NICK=antigaz_bot
export TWITCH_TOKEN=oauth:YOUR_TOKEN
export TWITCH_CHANNELS=antigaz
export TWITCH_BOT_WEBHOOK_SECRET=change_me

python main.py
```

## 3) Multi-channel support

The bot can join multiple channels:

```bash
export TWITCH_CHANNELS=antigaz,another_channel
```

The main app currently posts to `TWITCH_NOTIFY_CHANNEL`. If you later want
per-stream dynamic channels, extend the panel to send `channel` in the
`admin_activate_submission` event (the webhook already supports it).

## 4) Troubleshooting

- If nothing appears in chat, check the bot logs for `ready as ...`.
- If activation works but no message is posted, check that:
  - `TWITCH_BOT_WEBHOOK_URL` is correct and reachable from the Flask process
  - `TWITCH_BOT_WEBHOOK_SECRET` matches on both sides (or is unset on both)
  - the bot joined the channel listed in `TWITCH_CHANNELS`
