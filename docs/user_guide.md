# Daily Giveaway Bot – Operator Guide

Looking to use the developer-hosted bot instead of self-hosting? See [hosted_usage.md](hosted_usage.md) for the invite link and quick configuration steps.

## 1. Prerequisites
- Python 3.10 or newer with `discord.py` dependencies (see `requirements.txt`).
- A Discord application/bot with the **applications.commands** scope and **Server Members Intent** enabled.
- Permissions in the target guild to manage channels/roles when configuring logging and admin access.

## 2. Installation
```bash
git clone <your-fork-or-repo>
cd dailygiveawaybot
python3 -m venv .venv
source .venv/bin/activate    # or .venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
```

## 3. Configuration

### 3.1 Secrets (`.env`)
Create a `.env` file in the project root (or copy `.env.example`) and set:
```
DISCORD_TOKEN=your-discord-bot-token
```

### 3.2 Runtime Settings (`config/config.yaml`)
Key fields:
- `application_id`: the application snowflake ID.
- `default_timezone`: IANA timezone string used for scheduling (e.g. `Europe/Berlin`).
- `logging.logger_channel_id`: optional channel to receive lifecycle notices (can also be set via `/giveaway-settings` and `/giveaway-logger`).
- `manual_defaults.duration_minutes`: default duration for `/giveaway-start` when you omit times.
- `scheduling.auto_enabled` & `scheduling.giveaways`: preconfigured daily giveaways (optional).
- `permissions.admin_roles`: initial role IDs allowed to manage giveaways.
- `permissions.development_guild_id`: guild to receive development command syncs.

State is persisted to `data/state.json` and should be left alone while the bot runs.

## 4. Launching the Bot
```bash
source .venv/bin/activate
python -m src.bot --config config/config.yaml
```
Slash commands auto-sync when the bot starts; ensure the bot is invited with `applications.commands`.

## 5. Runtime Behaviour Highlights
- Giveaways are stored in `data/state.json` to survive restarts.
- Persistent buttons let members join/leave without chat spam.
- A *recent winner cooldown* can block past winners for N days; if not enough eligible entrants remain, the bot automatically re-selects the oldest recent winners who re-entered.
- Scheduled and recurring giveaways are handled even across restarts thanks to background tasks and audits.

## 6. Slash Commands

### 6.1 Settings Group
- `/giveaway-settings set <setting> <value>`  
  - `timezone`: update the guild timezone (IANA string).  
  - `recent_winner_days`: set the cooldown duration in days (0 disables).
- `/giveaway-settings get` – show timezone, daily automation status, and cooldown settings.
- `/giveaway-settings enable <feature>` / `/giveaway-settings disable <feature>`  
  - `recent_winner_cooldown`: toggle the cooldown enforcement.  
  - `auto_daily`: toggle daily automation from configured schedules.

### 6.2 Giveaway Lifecycle
- `/giveaway-start <channel> <winners> <title> <description> <start> <end> [run_daily]`  
  Schedule a giveaway. If `run_daily` is true, it is also saved as a recurring template.
- `/giveaway-end <id>` – finish immediately and draw winners.
- `/giveaway-edit <id> [winners] [title] [description] [end_time]` – update metadata or extend the end time (`YYYY-MM-DD HH:MM`).
- `/giveaway-reroll <id>` – draw replacement winners for a finished giveaway.

### 6.3 Visibility & Maintenance
- `/giveaway-list` – summary of all giveaways (active and finished).
- `/giveaway-show-participants <id>` – list current entrants (admin only).
- `/giveaway-logger <channel>` – choose a text channel for lifecycle and cooldown logs.
- `/giveaway-cleanup` – prune finished giveaways older than the current cooldown window.
- `/giveaway-add-admin-role <role>` / `/giveaway-remove-admin-role <role>` / `/giveaway-list-admin-roles` – manage giveaway admins (defaults to Manage Server permissions if empty).
- `/giveaway-enable` / `/giveaway-disable` – toggle configured daily schedules at runtime.

## 7. Recent Winner Cooldown Rules
1. Cooldown is per-guild and disabled by default. Configure using `/giveaway-settings`.
2. When a giveaway ends:
   - Eligible winners are sampled from participants not on cooldown.
   - If not enough entrants remain, the bot automatically selects the *oldest* recent winners who re-entered, filling the remaining slots.
   - Every override is logged to the configured log channel and application logs.
   - Giveaways with no winners flush non-winning participants to avoid repeated audit attempts.
3. The cleanup command respects the cooldown window, keeping recent giveaways to honour the history.

## 8. Troubleshooting Tips
- **Commands missing**: ensure the bot has re-synced (restart it) and you invited it with the commands scope.
- **Timezone errors**: the bot validates against the IANA database; incorrect values raise informative errors.
- **No winners**: the log channel will contain detailed reasons (e.g. all participants still on cooldown).
- **State debugging**: never hand-edit `data/state.json` while running. Stop the bot first to avoid corruption.

## 9. Development Notes
- Primary modules:  
  `src/bot.py` (command definitions),  
  `src/giveaway_manager.py` (business logic),  
  `src/views.py` (Discord UI views),  
  `src/config.py` / `src/models.py` / `src/storage.py` (config & persistence).
- Unit tests are not bundled; use careful manual testing via Discord or add tests targeting these modules.

For an at-a-glance overview see the updated `README.md`; refer back to this guide for detailed command usage and configuration advice.

## 10. Creating Your Own Discord Bot Application
If you prefer to operate your own bot identity instead of using the hosted instance:
1. Visit the [Discord Developer Portal](https://discord.com/developers/applications) and sign in.
2. Click **New Application**, provide a name, and confirm.
3. Under **Bot**, choose **Add Bot** → **Yes, do it!**. Optionally upload an avatar and set a username.
4. Enable the **SERVER MEMBERS INTENT** and **MESSAGE CONTENT INTENT** (optional but useful for detailed logging) under *Privileged Gateway Intents*.
5. Copy the **Bot Token**—store it securely, and set it as `DISCORD_TOKEN` in your `.env`.
6. Under **OAuth2 → General**, note the **Application ID**; place it into `config/config.yaml`.
7. Under **OAuth2 → URL Generator**, select **bot** and **applications.commands**, pick the permissions the bot needs (or reuse this project’s invite scopes), and copy the generated URL to invite your bot to servers you manage.
8. After configuring `config/config.yaml` and `.env`, follow the installation and launch steps earlier in this guide to run your personally branded instance.
