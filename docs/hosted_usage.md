# Daily Giveaway Bot – Hosted Instance Guide

If you do not want to run the bot yourself, you can invite the developer-hosted instance directly to your Discord server.

## 1. Invite the Bot
1. Ensure you have the *Manage Server* permission in the guild you want to add the bot to.
2. Open the invite link:
   [https://discord.com/oauth2/authorize?client_id=1432780049488281662&permissions=277025483840&integration_type=0&scope=bot+applications.commands](https://discord.com/oauth2/authorize?client_id=1432780049488281662&permissions=277025483840&integration_type=0&scope=bot+applications.commands)
3. Select your server from the dropdown, review the permissions, and authorise.
4. Once authorised, the bot will join the server and immediately sync slash commands.

## 2. Post-Invite Configuration
After the bot is present in your server:
- Run `/giveaway-settings get` to review the default configuration.
- Set a log channel with `/giveaway-logger <channel>` to receive lifecycle updates.
- If you have specific admin roles, add them using `/giveaway-add-admin-role <role>`.
- Optionally configure the recent winner cooldown via `/giveaway-settings set recent_winner_days <days>` and toggle it with `/giveaway-settings enable recent_winner_cooldown`.

All command behaviour and scheduling features are identical to a self-hosted deployment. Refer to [docs/user_guide.md](user_guide.md) for detailed explanations of every command and feature.
