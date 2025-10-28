from __future__ import annotations

import discord


class GiveawayView(discord.ui.View):
    def __init__(self, manager, giveaway_id: str) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.giveaway_id = giveaway_id

        join_button = discord.ui.Button(
            label="Join 🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"giveaway:join:{giveaway_id}",
        )
        join_button.callback = self.join_callback  # type: ignore[assignment]
        self.add_item(join_button)

        leave_button = discord.ui.Button(
            label="Leave",
            style=discord.ButtonStyle.secondary,
            custom_id=f"giveaway:leave:{giveaway_id}",
        )
        leave_button.callback = self.leave_callback  # type: ignore[assignment]
        self.add_item(leave_button)

        info_button = discord.ui.Button(
            label="Participants",
            style=discord.ButtonStyle.primary,
            custom_id=f"giveaway:info:{giveaway_id}",
        )
        info_button.callback = self.info_callback  # type: ignore[assignment]
        self.add_item(info_button)

    async def join_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "You can only join giveaways from a guild.", ephemeral=True
            )
            return
        response = await self.manager.add_participant(self.giveaway_id, interaction.user)
        await interaction.response.send_message(response, ephemeral=True)

    async def leave_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "You must be part of the guild to leave the giveaway.", ephemeral=True
            )
            return
        response = await self.manager.remove_participant(self.giveaway_id, interaction.user)
        await interaction.response.send_message(response, ephemeral=True)

    async def info_callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Guild members only.", ephemeral=True)
            return
        if not self.manager.is_admin(
            interaction.user,
            guild_owner_id=getattr(interaction.guild, "owner_id", None) if interaction.guild else None,
            base_permissions=getattr(interaction, "permissions", None),
            role_ids=getattr(interaction.user, "_roles", None),
        ):
            await interaction.response.send_message(
                "Only administrators can view the participant list.", ephemeral=True
            )
            return
        giveaway = await self.manager.get_giveaway(self.giveaway_id)
        if not giveaway:
            await interaction.response.send_message("Giveaway not found.", ephemeral=True)
            return
        if not giveaway.participants:
            await interaction.response.send_message("No participants yet.", ephemeral=True)
            return
        description = "\n".join(f"- <@{participant}>" for participant in giveaway.participants)
        await interaction.response.send_message(
            f"Participants for **{giveaway.title}** (`{giveaway.id}`):\n{description}",
            ephemeral=True,
        )
