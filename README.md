# H-1B Slot Watch (India)

Polls public US visa slot trackers for H-1B availability at posts in India and
sends a Telegram alert when new sightings appear. Alert-only.

## How it works

Every 10 minutes (GitHub Actions schedule), `monitor.py` fetches two public
tracker pages, diffs sightings against `state.json`, and messages a Telegram
bot only when something new appears.

Safety properties: read-only requests to public pages only; never visits any
login or scheduling portal; no credentials in code — the Telegram token and
chat ID come from GitHub Actions secrets (environment variables). Standard
library only, no dependencies.

## Setup

1. Create a Telegram bot via @BotFather and save the token. Message the bot
   once, then read your chat ID from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
2. Create a public GitHub repo and upload every file in this folder, keeping
   the `.github/workflows/` path intact.
3. Add repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
   (Settings → Secrets and variables → Actions).
4. Actions tab → run the workflow once. The first run saves a baseline without
   alerting; subsequent runs alert only on new sightings.

## Notes

- Tracker data is crowdsourced and may be stale or wrong; always verify on the
  official portal before acting.
- Read `monitor.py` before deploying — it is one short file, written to be audited.
