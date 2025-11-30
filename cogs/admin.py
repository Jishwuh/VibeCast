import discord
from discord import app_commands
from discord.ext import commands

from utils.queue_manager import QueueManager
from pathlib import Path
import json


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot, config: dict, queue_manager: QueueManager, playlist_store=None):
        self.bot = bot
        self.config = config
        self.queue_manager = queue_manager
        self.playlist_store = playlist_store
        self.config_path = Path("config.json")

    def _has_permission(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        allowed_roles = self.config.get("allowed_roles") or []
        if not allowed_roles:
            return True
        allowed_ids = set()
        allowed_names = set()
        for role in allowed_roles:
            if isinstance(role, int):
                allowed_ids.add(role)
            else:
                try:
                    allowed_ids.add(int(role))
                except (TypeError, ValueError):
                    allowed_names.add(str(role))
        for role in member.roles:
            if role.id in allowed_ids or role.name in allowed_names:
                return True
        return False

    @app_commands.command(name="shutdown", description="Admin: shut down the bot.")
    async def shutdown(self, interaction: discord.Interaction):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission to shut down the bot.", ephemeral=True)
            return
        await interaction.response.send_message("Shutting down...", ephemeral=True)
        await self.bot.close()

    @app_commands.command(name="clearqueue", description="Admin: clear the queue and stop playback.")
    async def clearqueue(self, interaction: discord.Interaction):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission to clear the queue.", ephemeral=True)
            return
        await self.queue_manager.clear(interaction.guild.id)
        if interaction.guild.voice_client:
            interaction.guild.voice_client.stop()
        await interaction.response.send_message("Queue cleared.", ephemeral=True)

    @app_commands.command(
        name="setroles",
        description="Admin: set allowed DJ roles (pick up to 5 roles, leave all blank to clear).",
    )
    @app_commands.describe(
        role1="First allowed role",
        role2="Second allowed role",
        role3="Third allowed role",
        role4="Fourth allowed role",
        role5="Fifth allowed role",
    )
    async def setroles(
        self,
        interaction: discord.Interaction,
        role1: discord.Role | None = None,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        role4: discord.Role | None = None,
        role5: discord.Role | None = None,
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permission required.", ephemeral=True)
            return
        role_objs = [r for r in (role1, role2, role3, role4, role5) if r is not None]
        role_ids = [r.id for r in role_objs]
        # Deduplicate while preserving order
        seen = set()
        deduped = []
        for rid in role_ids:
            if rid not in seen:
                deduped.append(rid)
                seen.add(rid)
        self.config["allowed_roles"] = deduped
        # Persist to config.json if possible
        try:
            with self.config_path.open("w", encoding="utf-8") as fp:
                json.dump(self.config, fp, indent=2)
        except Exception as exc:
            await interaction.response.send_message(f"Updated in memory, but failed to write config: {exc}", ephemeral=True)
            return
        if deduped:
            await interaction.response.send_message(
                f"Allowed roles updated to: {', '.join(str(r) for r in deduped)}", ephemeral=True
            )
        else:
            await interaction.response.send_message("Allowed roles cleared (anyone can use DJ/admin commands).", ephemeral=True)


async def setup(bot: commands.Bot):
    # Not used; cogs are loaded manually in bot.py
    pass
