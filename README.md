# Donut Bot

Discord bot with:
- `/stats`, `/pay` (DonutSMP, via the Minecraft worker account in `mc-bot/`)
- AI chat: @mention the bot or reply to it. It chats, remembers people, can search the web,
  and (for the bot owner only) can look up player stats in any channel.
- Admin panel at `/admin` (your MC sessions, bot servers, per-server AI switches, stats channels). No plans or limits; only the owner exists

## Structure
- `main.py` - entrypoint: Discord bot + MC bot subprocess + admin panel (Flask)
- `app.py` - admin panel
- `cogs/` - stats, pay, ai_chat (no commands; enabled per server in the admin panel) (+ config / command_perms_core helpers)
- `utils/`, `database/`, `config/` - shared helpers
- `mc-bot/` - Node Minecraft bot used by stats/pay

## Setup
1. Copy `.env.example` to `.env` and fill it in.
2. `pip install -r requirements.txt` and `cd mc-bot && npm install`
3. `python main.py`
