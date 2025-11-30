import asyncio
import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from cogs.admin import Admin
from cogs.music import Music
from utils.queue_manager import QueueManager
from utils.playlist_store import PlaylistStore


CONFIG_PATH = Path("config.json")
LOG_PATH = Path("logs/bot.log")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError("config.json not found. Please create it before running the bot.")
    with CONFIG_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def resolve_token(config: dict) -> str:
    return os.getenv("DISCORD_TOKEN") or config.get("discord_token", "")


class MusicBot(commands.Bot):
    def __init__(self, config: dict, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.queue_manager = QueueManager(max_length=config.get("max_queue_length", 50))
        self.playlist_store = PlaylistStore()
        self.logger = logging.getLogger("MusicBot")

    async def setup_hook(self):
        await self.add_cog(Music(self, self.config, self.queue_manager, self.playlist_store))
        await self.add_cog(Admin(self, self.config, self.queue_manager, self.playlist_store))
        await self.tree.sync()
        self.logger.info("Slash commands synced.")
        print("Bot is ready.")
        print("Invite URL:", discord.utils.oauth_url(self.user.id, permissions=discord.Permissions(permissions=8)))


def main():
    load_dotenv()
    setup_logging()
    config = load_config()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.voice_states = True

    bot = MusicBot(
        config=config,
        command_prefix=config.get("command_prefix", "!"),
        intents=intents,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    token = resolve_token(config)
    if not token:
        raise RuntimeError("Discord token missing. Set DISCORD_TOKEN env var or fill config.json.")

    bot.logger.info("Starting bot...")
    bot.run(token)


if __name__ == "__main__":
    main()
