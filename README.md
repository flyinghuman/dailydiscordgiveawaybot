# Daily Giveaway Discord Bot

Powerful Discord slash-command bot for hosting manual and recurring giveaways with persistent state, rich logging, and fair winner selection.

## Key Features
- **Persistent giveaways** backed by `data/state.json`—restart-safe and automatically restored on boot.
- **Recurring & scheduled runs** with per-guild timezone awareness and runtime enable/disable controls.
- **Recent winner cooldowns** that temporarily block past winners, with intelligent fallback to the oldest entrants if participation is thin.
- **Rich logging** to console, file, and an optional Discord channel—including cooldown overrides and rerolls.
- **Secure draws** using cryptographically strong randomness.

## Quick Start
```bash
git clone <repo>
cd dailygiveawaybot
python3 -m venv .venv
source .venv/bin/activate        # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
```

1. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
2. Copy `config/config.example.yaml` to `config/config.yaml` and fill in:
   - `application_id`, `default_timezone`, `logging.logger_channel_id` (optional),
   - `manual_defaults`, `scheduling` templates, and `permissions.admin_roles`.
3. Invite the bot with `applications.commands` (and enable the Server Members Intent).
4. Launch with:
   ```bash
   python -m src.bot --config config/config.yaml
   ```

Slash commands synchronise automatically; restart the bot after adding new guilds or permissions.

## Command Highlights
- **Settings** – `/giveaway-settings set|get|enable|disable` for timezone, recent winner cooldown, and auto scheduling switches.
- **Lifecycle** – `/giveaway-start`, `/giveaway-end`, `/giveaway-edit`, `/giveaway-reroll`.
- **Oversight** – `/giveaway-list`, `/giveaway-show-participants`, `/giveaway-cleanup`.
- **Administration** – `/giveaway-logger`, `/giveaway-add-admin-role`, `/giveaway-enable`, and counterparts.

Every command is described in detail—parameters, permission requirements, and responses—in the [User Guide](docs/user_guide.md).

## Behaviour Notes
- Participants interact via persistent message components, keeping channels clutter-free.
- Recent winner cooldowns:
  - Block past winners for a configurable number of days.
  - Automatically fall back to the oldest re-entered winners if no eligible entrants remain, with detailed logging.
- Cleanup honours the cooldown window, ensuring the history required for fair draws remains intact.

## Project Structure
- `src/bot.py` – entry point, slash commands, settings group.
- `src/giveaway_manager.py` – giveaway lifecycle, persistence, cooldown logic.
- `src/models.py`, `src/storage.py`, `src/config.py` – data models, JSON persistence, YAML parsing.
- `src/views.py` – Discord UI components.
- `docs/user_guide.md` – full installation, setup, and command documentation.

## Logging & State
- Runtime logs: console and `logs/log.txt`.
- Optional Discord channel logging configurable via `/giveaway-logger` or `config/config.yaml`.
- Persistent state: `data/state.json` (gitignored). Do not edit while the bot is running.

For extended instructions—including troubleshooting tips and command walkthroughs—see [docs/user_guide.md](docs/user_guide.md).
