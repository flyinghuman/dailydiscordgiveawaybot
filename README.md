# Daily Giveaway Discord Bot

## Overview
- Python 3 slash-command Discord bot that manages multiple concurrent giveaways and daily scheduled raffles.
- Stores state in `data/state.json` so giveaways persist across restarts; uses persistent buttons so members join or leave without new messages spam.

## Setup
- Ensure Python 3.10+ is installed with Discord intents enabled for members in the developer portal.
- Create the virtual environment in this repo (already created as `.venv/`):
  - `python3 -m venv .venv`
  - `source .venv/bin/activate` (Linux/macOS) or `.venv\Scripts\Activate.ps1` (Windows).
- Install dependencies: `pip install -r requirements.txt`.

## Configuration
- Copy `config/config.example.yaml` to `config/config.yaml` and populate it.
- Required keys:
  - `token`: bot token from the Discord developer portal.
  - `application_id`: your application's snowflake (needed for slash command sync).
  - `default_timezone`: IANA timezone name used for scheduling (for example `Europe/Berlin`).
  - `logging.logger_channel_id`: optional channel ID that receives giveaway lifecycle messages (overridden via `/giveaway-logger`).
  - `manual_defaults.duration_minutes`: default duration for manual `/giveaway-start` commands.
- Scheduled giveaways:
- `scheduling.auto_enabled`: global toggle for daily automation at startup (defaults to `false`).
- Each entry under `scheduling.giveaways` is optional; when configured it needs `id`, `channel_id`, `winners`, `title`, `description`, `start_time`, and `end_time` (24h clock).
  - Start times are interpreted in the configured timezone; when `end_time` is earlier than `start_time`, the giveaway ends the following day.
- Permissions:
  - Optionally seed `permissions.admin_roles` once; afterwards manage access with `/giveaway-add-admin-role` and `/giveaway-remove-admin-role` (users with Discord `Manage Server` always qualify).

## Commands
- `/giveaway-start <channel> <winners> <title> <description>`: creates an active giveaway using the default manual duration (`<channel>` may be a mention, numeric ID, or exact channel name).
- `/giveaway-end <id>`: immediately ends a giveaway and announces winners.
- `/giveaway-edit <id> [winners] [title] [description] [end_time]`: update metadata or reschedule the end time (`YYYY-MM-DD HH:MM`).
- `/giveaway-list`: show every giveaway tracked by the bot with status, end time, and participant count.
- `/giveaway-show-participants <id>`: admin-only participant roster (also reachable via the embed button).
- `/giveaway-reroll <id>`: draw new winners for a finished giveaway and announce the result.
- `/giveaway-logger <channel>`: set the channel for log messages (accepts mention/ID/name and persists in `data/state.json`).
- `/giveaway-add-admin-role <role>`: grant giveaway management permissions to the specified role.
- `/giveaway-remove-admin-role <role>`: revoke giveaway management permissions from the specified role.
- `/giveaway-list-admin-roles`: list all roles currently allowed to manage giveaways.
- `/giveaway-enable` & `/giveaway-disable`: toggle scheduled daily giveaways globally without editing config.

## Runtime Behaviour
- The bot uses embeds with persistent buttons for joining, leaving, or viewing participants—no extra messages are posted when counts change.
- Winners are selected with `random.sample`; the bot gracefully handles cases with fewer entrants than winners.
- State persistence keeps active giveaways, participant lists, and last winners across restarts.
- Daily scheduled giveaways run once per day per schedule entry and respect the configured timezone plus runtime toggles.
- Detailed debug output (including permission checks) is written to `logs/log.txt` in addition to console logging.

## Running
- Activate the virtual environment.
- Launch the bot: `python -m src.bot --config config/config.yaml`.
- Slash commands auto-sync on startup; ensure the bot has the `applications.commands` scope invited to the guild.

## Development Notes
- Core modules live under `src/`:
  - `bot.py`: entry point, slash command definitions, scheduler wiring.
  - `giveaway_manager.py`: state machine, persistence, embeds, and scheduling.
  - `config.py`: YAML parsing & validation.
  - `views.py`: persistent button view for member interactions.
- Persistent data is stored in `data/state.json` (ignored by git); delete it only if you intentionally want to reset ongoing giveaways.
