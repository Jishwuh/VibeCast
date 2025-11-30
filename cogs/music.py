import asyncio
import logging
import math
import time
from typing import List, Optional, Tuple

import discord
import requests
from discord import app_commands
from discord.ext import commands

from utils.audio_source import AudioSource
from utils.queue_manager import QueueManager, Track


BASE_FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
DEFAULT_FFMPEG_OPTIONS = {"options": "-vn"}


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot, config: dict, queue_manager: QueueManager):
        self.bot = bot
        self.config = config
        self.queue_manager = queue_manager
        self.audio_source = AudioSource(config)
        self.logger = logging.getLogger("MusicCog")
        self.current: dict[int, Optional[Track]] = {}
        self.default_volume = float(config.get("default_volume", 0.5))
        self.youtube_api_key = (
            config.get("youtube_api_key") or self.audio_source.youtube_api_key
        )
        self.idle_timeout = 120
        self.start_times: dict[int, float] = {}
        self.pause_offsets: dict[int, float] = {}
        self.pause_marks: dict[int, float] = {}
        self.panels: dict[int, list[discord.Message]] = {}
        self.last_channel: dict[int, discord.TextChannel] = {}
        self.skip_after: dict[int, bool] = {}

    async def ensure_voice_interaction(
        self, interaction: discord.Interaction
    ) -> Optional[discord.VoiceClient]:
        if not interaction.user.voice or not interaction.user.voice.channel:
            if interaction.response.is_done():
                await interaction.followup.send("You need to be in a voice channel first.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "You need to be in a voice channel first.", ephemeral=True
                )
            return None
        voice_client = interaction.guild.voice_client
        if voice_client is None or not voice_client.is_connected():
            voice_client = await interaction.user.voice.channel.connect()
        elif voice_client.channel != interaction.user.voice.channel:
            await voice_client.move_to(interaction.user.voice.channel)
        return voice_client

    def _build_before_options(self, track: Track, start_at: int = 0) -> str:
        before_opts = BASE_FFMPEG_BEFORE
        if start_at > 0:
            before_opts = f"{before_opts} -ss {start_at}"
        if track.headers:
            header_blob = "\r\n".join(f"{k}: {v}" for k, v in track.headers.items())
            before_opts = f'{before_opts} -headers "{header_blob}\r\n"'
        return before_opts

    async def _start_playback(
        self,
        guild_id: int,
        channel: Optional[discord.abc.Messageable],
        voice_client: discord.VoiceClient,
        track: Track,
        start_at: int = 0,
        announce: bool = True,
        replace_panel: bool = True,
    ):
        before_opts = self._build_before_options(track, start_at=start_at)

        def after_playback(error: Optional[Exception]):
            if error:
                self.logger.error("Playback error: %s", error)
            if self.skip_after.pop(guild_id, False):
                return
            fut = self.bot.loop.create_task(self.play_next(guild_id, channel))
            fut.add_done_callback(lambda f: f.exception() if f.exception() else None)

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                track.stream_url,
                before_options=before_opts,
                **DEFAULT_FFMPEG_OPTIONS,
            ),
            volume=self.default_volume,
        )
        voice_client.play(source, after=after_playback)
        self.current[guild_id] = track
        now = time.monotonic()
        self.start_times[guild_id] = now - start_at
        self.pause_offsets[guild_id] = 0.0
        self.pause_marks.pop(guild_id, None)
        if isinstance(channel, discord.TextChannel):
            self.last_channel[guild_id] = channel
        if channel:
            if announce:
                await channel.send(f"Now playing: **{track.title}** [{track.source}] requested by {track.requester}")
            if replace_panel:
                await self._send_panel(guild_id, channel, replace=True)
            else:
                await self._update_panels(guild_id)

    async def play_next(self, guild_id: int, channel: discord.abc.Messageable):
        voice_client = discord.utils.get(self.bot.voice_clients, guild__id=guild_id)
        if voice_client is None or not voice_client.is_connected():
            return
        next_track = await self.queue_manager.pop_next(guild_id)
        if next_track:
            await self._start_playback(guild_id, channel, voice_client, next_track)
            return

        self.current[guild_id] = None
        self._reset_progress(guild_id)
        await self._update_panels(guild_id)

        async def disconnect_after_idle():
            await asyncio.sleep(self.idle_timeout)
            if not voice_client.is_playing() and not voice_client.is_paused():
                await voice_client.disconnect()
                await channel.send("Disconnected due to inactivity.")

        self.bot.loop.create_task(disconnect_after_idle())

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

    def _reset_progress(self, guild_id: int):
        self.start_times.pop(guild_id, None)
        self.pause_offsets.pop(guild_id, None)
        self.pause_marks.pop(guild_id, None)

    def _format_time(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "??:??"
        m, s = divmod(max(0, int(seconds)), 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _parse_timestamp(self, value: str) -> Optional[int]:
        value = value.strip()
        if not value:
            return None
        if ":" in value:
            parts = value.split(":")
            try:
                parts = [int(p) for p in parts]
            except ValueError:
                return None
            seconds = 0
            for p in parts:
                seconds = seconds * 60 + p
            return seconds
        if value.isdigit():
            return int(value)
        return None

    def _current_elapsed(self, guild_id: int) -> Optional[float]:
        if guild_id not in self.start_times:
            return None
        elapsed = time.monotonic() - self.start_times[guild_id] - self.pause_offsets.get(guild_id, 0.0)
        if guild_id in self.pause_marks:
            elapsed = self.pause_marks[guild_id] - self.start_times.get(guild_id, 0.0) - self.pause_offsets.get(guild_id, 0.0)
        return max(elapsed, 0.0)

    async def _handle_seek(self, interaction: discord.Interaction, target: int, mode: str):
        guild_id = interaction.guild.id
        track = self.current.get(guild_id)
        vc = interaction.guild.voice_client if interaction.guild else None
        if not track or not vc:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if track.duration and (target < 0 or target > track.duration):
            await interaction.response.send_message("Timestamp out of range for this track.", ephemeral=True)
            return
        elapsed = self._current_elapsed(guild_id) or 0.0
        if mode == "rewind" and target >= elapsed:
            await interaction.response.send_message("Rewind must be earlier than the current time.", ephemeral=True)
            return
        if mode == "forward" and target <= elapsed:
            await interaction.response.send_message("Fast-forward must be later than the current time.", ephemeral=True)
            return
        self.skip_after[guild_id] = True
        vc.stop()
        channel = self.last_channel.get(guild_id) or interaction.channel
        await self._start_playback(guild_id, channel, vc, track, start_at=target, announce=False, replace_panel=False)
        if interaction.response.is_done():
            await interaction.followup.send(f"Jumped to {self._format_time(target)}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Jumped to {self._format_time(target)}.", ephemeral=True)

    def _progress(self, guild_id: int, duration: Optional[int]) -> tuple[str, str]:
        if duration is None or duration <= 0:
            return "🔘 " + ("▬" * 18), "?:?? / ??:??"
        elapsed = 0.0
        if guild_id in self.start_times:
            elapsed = time.monotonic() - self.start_times[guild_id] - self.pause_offsets.get(guild_id, 0.0)
        if guild_id in self.pause_marks:
            elapsed = self.pause_marks[guild_id] - self.start_times.get(guild_id, 0.0) - self.pause_offsets.get(guild_id, 0.0)
        elapsed = max(0.0, min(float(duration), elapsed))
        ratio = elapsed / float(duration) if duration else 0.0
        total_slots = 18
        filled = min(total_slots - 1, max(0, math.floor(ratio * total_slots)))
        bar = "".join("▬" if i != filled else "🔘" for i in range(total_slots))
        return bar, f"{self._format_time(int(elapsed))} / {self._format_time(duration)}"

    def _thumbnail_for(self, track: Track) -> Optional[str]:
        if "youtube.com/watch?v=" in track.url or "youtu.be/" in track.url:
            vid = None
            if "watch?v=" in track.url:
                vid = track.url.split("watch?v=")[-1].split("&")[0]
            elif "youtu.be/" in track.url:
                vid = track.url.split("youtu.be/")[-1].split("?")[0]
            if vid:
                return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"
        return None

    def _build_now_playing_embed(self, guild_id: int) -> discord.Embed:
        track = self.current.get(guild_id)
        if not track:
            embed = discord.Embed(title="Nothing Playing", description="Queue is empty.", color=0x2b2d31)
            embed.set_footer(text="Use /play or /search to add a track.")
            return embed
        bar, progress_text = self._progress(guild_id, track.duration)
        desc_lines = [
            f"**[{track.title}]({track.url})**",
            f"{bar}",
            progress_text,
            f"Requested by: {track.requester}",
            f"Source: {track.source}",
        ]
        embed = discord.Embed(
            title="Now Playing",
            description="\n".join(desc_lines),
            color=0x5865F2,
        )
        thumb = self._thumbnail_for(track)
        if thumb:
            embed.set_thumbnail(url=thumb)
        embed.set_footer(text="Use the controls below to manage playback.")
        return embed

    async def _delete_panels(self, guild_id: int):
        for msg in list(self.panels.get(guild_id, [])):
            try:
                await msg.delete()
            except Exception:
                pass
        self.panels[guild_id] = []

    async def _send_panel(self, guild_id: int, channel: Optional[discord.abc.Messageable], replace: bool = True):
        if channel is None:
            return
        if replace:
            await self._delete_panels(guild_id)
        embed = self._build_now_playing_embed(guild_id)
        view = ControlView(self, guild_id)
        try:
            msg = await channel.send(embed=embed, view=view)
            self._register_panel(guild_id, msg)
        except Exception as exc:
            self.logger.warning("Failed to send panel: %s", exc)

    async def _ensure_panel_exists(self, guild_id: int, channel: Optional[discord.abc.Messageable]):
        if not self.panels.get(guild_id):
            await self._send_panel(guild_id, channel, replace=False)
        else:
            await self._update_panels(guild_id)

    async def _update_panels(self, guild_id: int):
        if guild_id not in self.panels:
            return
        embed = self._build_now_playing_embed(guild_id)
        view_factory = lambda: ControlView(self, guild_id)
        alive = []
        for msg in list(self.panels.get(guild_id, [])):
            try:
                await msg.edit(embed=embed, view=view_factory())
                alive.append(msg)
            except Exception:
                continue
        self.panels[guild_id] = alive

    def _register_panel(self, guild_id: int, message: discord.Message):
        self.panels.setdefault(guild_id, []).append(message)

    # Slash commands
    @app_commands.command(name="join", description="Bot joins your voice channel.")
    async def join(self, interaction: discord.Interaction):
        voice_client = await self.ensure_voice_interaction(interaction)
        if voice_client:
            await interaction.response.send_message(f"Joined {voice_client.channel.mention}", ephemeral=True)

    @app_commands.command(name="leave", description="Bot leaves the voice channel.")
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc:
            await vc.disconnect()
            await interaction.response.send_message("Left the voice channel.", ephemeral=True)
        else:
            await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

    @app_commands.command(name="play", description="Play or queue a track from a URL or search term.")
    @app_commands.describe(query="YouTube/Spotify/SoundCloud URL or search text")
    async def play(self, interaction: discord.Interaction, query: str):
        voice_client = await self.ensure_voice_interaction(interaction)
        if not voice_client:
            return
        self.last_channel[interaction.guild.id] = interaction.channel  # type: ignore
        await interaction.response.defer(thinking=True)
        try:
            track = self.audio_source.resolve(query, requester=str(interaction.user))
        except Exception as exc:
            await interaction.followup.send(f"Could not get audio: {exc}", ephemeral=True)
            return

        added = await self.queue_manager.add_track(interaction.guild.id, track)
        if not added:
            await interaction.followup.send("Queue is full.", ephemeral=True)
            return

        if not voice_client.is_playing() and not voice_client.is_paused():
            next_track = await self.queue_manager.pop_next(interaction.guild.id) or track
            await self._start_playback(interaction.guild.id, interaction.channel, voice_client, next_track)  # type: ignore
            await interaction.followup.send(f"Now playing **{track.title}**", ephemeral=False)
        else:
            await interaction.followup.send(f"Queued **{track.title}**", ephemeral=False)
            await self._ensure_panel_exists(interaction.guild.id, interaction.channel)  # type: ignore

    @app_commands.command(name="pause", description="Pause playback.")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.is_playing():
            vc.pause()
            self.pause_marks[interaction.guild.id] = time.monotonic()
            await interaction.response.send_message("Paused playback.", ephemeral=True)
            await self._update_panels(interaction.guild.id)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback.")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.is_paused():
            now = time.monotonic()
            paused_at = self.pause_marks.pop(interaction.guild.id, None)
            if paused_at:
                self.pause_offsets[interaction.guild.id] = self.pause_offsets.get(interaction.guild.id, 0.0) + (
                    now - paused_at
                )
            vc.resume()
            await interaction.response.send_message("Resumed playback.", ephemeral=True)
            await self._update_panels(interaction.guild.id)
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc or not vc.is_connected():
            await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
        await self._update_panels(interaction.guild.id)

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    async def stop(self, interaction: discord.Interaction):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc:
            await self.queue_manager.clear(interaction.guild.id)
            vc.stop()
            self.current[interaction.guild.id] = None
            self._reset_progress(interaction.guild.id)
            await interaction.response.send_message("Stopped playback and cleared the queue.", ephemeral=True)
            await self._update_panels(interaction.guild.id)
            await self._delete_panels(interaction.guild.id)
        else:
            await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)

    @app_commands.command(name="queue", description="Show the current queue.")
    async def show_queue(self, interaction: discord.Interaction):
        items = await self.queue_manager.list_queue(interaction.guild.id)
        current = self.current.get(interaction.guild.id)
        if not items and not current:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        description_lines = []
        if current:
            description_lines.append(f"**Now:** {current.title} [{current.source}]")
        for idx, track in enumerate(items[:10], start=1):
            description_lines.append(f"{idx}. {track.title} — {track.requester} [{track.source}]")
        if len(items) > 10:
            description_lines.append(f"...and {len(items) - 10} more.")
        embed = discord.Embed(title="Queue", description="\n".join(description_lines))
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="nowplaying", description="Show the currently playing track.")
    async def now_playing(self, interaction: discord.Interaction):
        current = self.current.get(interaction.guild.id)
        if not current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        embed = discord.Embed(title="Now Playing", description=current.title)
        embed.add_field(name="Source", value=current.source)
        embed.add_field(name="Requested by", value=current.requester)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="volume", description="Set playback volume (0-100).")
    @app_commands.describe(level="Volume percent")
    async def volume(self, interaction: discord.Interaction, level: int):
        if level < 0 or level > 100:
            await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
            return
        vc = interaction.guild.voice_client if interaction.guild else None
        self.default_volume = level / 100
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.default_volume
        await interaction.response.send_message(f"Volume set to {level}%.", ephemeral=True)
        await self._update_panels(interaction.guild.id)

    @app_commands.command(name="clear", description="Clear the queue.")
    async def clear(self, interaction: discord.Interaction):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission to clear the queue.", ephemeral=True)
            return
        await self.queue_manager.clear(interaction.guild.id)
        await interaction.response.send_message("Cleared the queue.", ephemeral=True)
        await self._update_panels(interaction.guild.id)

    @app_commands.command(name="panel", description="Show an interactive player panel with controls.")
    async def panel(self, interaction: discord.Interaction):
        embed = self._build_now_playing_embed(interaction.guild.id)
        view = ControlView(self, interaction.guild.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        msg = await interaction.original_response()
        self._register_panel(interaction.guild.id, msg)

    # Slash command with autocomplete
    @app_commands.command(name="search", description="Search YouTube and play the selected track.")
    @app_commands.describe(query="Search term")
    async def search(self, interaction: discord.Interaction, query: str):
        voice_client = await self.ensure_voice_interaction(interaction)
        if not voice_client:
            return
        self.last_channel[interaction.guild.id] = interaction.channel  # type: ignore
        await interaction.response.defer(thinking=True)
        try:
            track = self.audio_source.resolve(query, requester=str(interaction.user))
        except Exception as exc:
            await interaction.followup.send(f"Could not get audio: {exc}", ephemeral=True)
            return
        added = await self.queue_manager.add_track(interaction.guild.id, track)
        if not added:
            await interaction.followup.send("Queue is full.", ephemeral=True)
            return
        if not voice_client.is_playing() and not voice_client.is_paused():
            next_track = await self.queue_manager.pop_next(interaction.guild.id) or track
            await self._start_playback(interaction.guild.id, interaction.channel, voice_client, next_track)  # type: ignore
            await interaction.followup.send(f"Now playing **{track.title}**", ephemeral=False)
        else:
            await interaction.followup.send(f"Queued **{track.title}**", ephemeral=False)
            await self._ensure_panel_exists(interaction.guild.id, interaction.channel)  # type: ignore

    @search.autocomplete("query")
    async def query_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        if not current or not self.youtube_api_key:
            return []

        def fetch_suggestions() -> List[app_commands.Choice[str]]:
            params = {
                "part": "snippet",
                "maxResults": 5,
                "q": current,
                "type": "video",
                "key": self.youtube_api_key,
            }
            resp = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=5)
            if not resp.ok:
                return []
            data = resp.json()
            choices: List[app_commands.Choice[str]] = []
            for item in data.get("items", []):
                title = item["snippet"]["title"]
                video_id = item["id"]["videoId"]
                full_title = f"{title}"
                url = f"https://www.youtube.com/watch?v={video_id}"
                choices.append(app_commands.Choice(name=full_title[:100], value=url))
            return choices

        return await asyncio.to_thread(fetch_suggestions)


class SeekModal(discord.ui.Modal):
    def __init__(self, cog: Music, guild_id: int, mode: str):
        title = "Rewind to..." if mode == "rewind" else "Fast forward to..."
        super().__init__(title=title, timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.mode = mode
        self.timestamp = discord.ui.TextInput(
            label="Enter time (mm:ss or seconds)",
            style=discord.TextStyle.short,
            required=True,
            placeholder="e.g., 1:30 or 90",
            max_length=16,
        )
        self.add_item(self.timestamp)

    async def on_submit(self, interaction: discord.Interaction):
        seconds = self.cog._parse_timestamp(str(self.timestamp))
        if seconds is None:
            await interaction.response.send_message("Invalid time format.", ephemeral=True)
            return
        await self.cog._handle_seek(interaction, seconds, self.mode)


class VolumeModal(discord.ui.Modal):
    def __init__(self, cog: Music, guild_id: int):
        super().__init__(title="Set Volume (0-100)", timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.level = discord.ui.TextInput(
            label="Volume percent",
            style=discord.TextStyle.short,
            required=True,
            placeholder="50",
            max_length=3,
        )
        self.add_item(self.level)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.level))
        except ValueError:
            await interaction.response.send_message("Please enter a number between 0 and 100.", ephemeral=True)
            return
        if value < 0 or value > 100:
            await interaction.response.send_message("Volume must be between 0 and 100.", ephemeral=True)
            return
        self.cog.default_volume = value / 100
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = self.cog.default_volume
        await interaction.response.send_message(f"Volume set to {value}%.", ephemeral=True)
        await self.cog._update_panels(self.guild_id)


class SearchModal(discord.ui.Modal):
    def __init__(self, cog: Music, guild_id: int):
        super().__init__(title="Play a song", timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.query = discord.ui.TextInput(
            label="Enter a URL or search text",
            style=discord.TextStyle.short,
            required=True,
            placeholder="Song URL or keywords",
            max_length=200,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        voice_client = await self.cog.ensure_voice_interaction(interaction)
        if not voice_client:
            return
        self.cog.last_channel[interaction.guild.id] = interaction.channel  # type: ignore
        try:
            track = self.cog.audio_source.resolve(str(self.query), requester=str(interaction.user))
        except Exception as exc:
            await interaction.response.send_message(f"Could not get audio: {exc}", ephemeral=True)
            return
        added = await self.cog.queue_manager.add_track(interaction.guild.id, track)
        if not added:
            await interaction.response.send_message("Queue is full.", ephemeral=True)
            return
        if not voice_client.is_playing() and not voice_client.is_paused():
            next_track = await self.cog.queue_manager.pop_next(interaction.guild.id) or track
            await self.cog._start_playback(interaction.guild.id, interaction.channel, voice_client, next_track)  # type: ignore
            await interaction.response.send_message(f"Now playing **{track.title}**", ephemeral=False)
        else:
            await interaction.response.send_message(f"Queued **{track.title}**", ephemeral=False)
            await self.cog._ensure_panel_exists(interaction.guild.id, interaction.channel)  # type: ignore

class ControlView(discord.ui.View):
    def __init__(self, cog: Music, guild_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild and interaction.guild.id != self.guild_id:
            await interaction.response.send_message("This control panel is not for this server.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Rewind", emoji="⏪", style=discord.ButtonStyle.gray, row=0)
    async def rewind_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SeekModal(self.cog, self.guild_id, mode="rewind"))

    @discord.ui.button(label="Play/Pause", emoji="⏯️", style=discord.ButtonStyle.blurple, row=0)
    async def pause_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            self.cog.pause_marks[self.guild_id] = time.monotonic()
            await interaction.response.send_message("Paused.", ephemeral=True)
        elif vc.is_paused():
            now = time.monotonic()
            paused_at = self.cog.pause_marks.pop(self.guild_id, None)
            if paused_at:
                self.cog.pause_offsets[self.guild_id] = self.cog.pause_offsets.get(self.guild_id, 0.0) + (
                    now - paused_at
                )
            vc.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing to pause/resume.", ephemeral=True)
            return
        await self.cog._update_panels(self.guild_id)

    @discord.ui.button(label="Fast Forward", emoji="⏩", style=discord.ButtonStyle.gray, row=0)
    async def ff_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SeekModal(self.cog, self.guild_id, mode="forward"))

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.cog._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc:
            await self.cog.queue_manager.clear(interaction.guild.id)
            self.cog.skip_after[self.guild_id] = True
            vc.stop()
            self.cog.current[interaction.guild.id] = None
            self.cog._reset_progress(interaction.guild.id)
            await interaction.response.send_message("Stopped and cleared.", ephemeral=True)
            await self.cog._delete_panels(self.guild_id)
        else:
            await interaction.response.send_message("Not connected.", ephemeral=True)

    @discord.ui.button(label="Volume", emoji="🔊", style=discord.ButtonStyle.gray, row=1)
    async def volume_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(VolumeModal(self.cog, self.guild_id))

    @discord.ui.button(label="Skip Song", emoji="⏭️", style=discord.ButtonStyle.gray, row=1)
    async def skip_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc or not vc.is_connected():
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)
        await self.cog._update_panels(self.guild_id)

    @discord.ui.button(label="Clear Queue", emoji="🧹", style=discord.ButtonStyle.gray, row=1)
    async def clear_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self.cog._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        await self.cog.queue_manager.clear(interaction.guild.id)
        await interaction.response.send_message("Queue cleared.", ephemeral=True)
        await self.cog._update_panels(self.guild_id)

    @discord.ui.button(label="Search", emoji="🔍", style=discord.ButtonStyle.blurple, row=2)
    async def search_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(SearchModal(self.cog, self.guild_id))


async def setup(bot: commands.Bot):
    # Not used; cogs are loaded manually in bot.py
    pass
