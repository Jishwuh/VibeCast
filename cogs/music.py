import asyncio
import logging
import math
import time
from typing import List, Optional, Tuple

import discord
import requests
from discord import app_commands
from discord.ext import commands
import yt_dlp

from utils.audio_source import AudioSource
from utils.queue_manager import QueueManager, Track
from utils.playlist_store import PlaylistStore


BASE_FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"
DEFAULT_FFMPEG_OPTIONS = {"options": "-vn"}


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot, config: dict, queue_manager: QueueManager, playlist_store: PlaylistStore):
        self.bot = bot
        self.config = config
        self.queue_manager = queue_manager
        self.playlist_store = playlist_store
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
        self.temp_djs: dict[int, set[int]] = {}
        self.votes: dict[int, set[int]] = {}
        self.history: dict[int, List[Track]] = {}
        self.autoplay: dict[int, bool] = {}
        self.autoplay_playlist: dict[int, Optional[str]] = {}
        self.autoplay_playlist_pos: dict[int, int] = {}

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
        self.votes[guild_id] = set()
        self._push_history(guild_id, track)
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

        if self.autoplay.get(guild_id, False):
            radio = await self._try_autoplay(guild_id, channel)
            if radio:
                await self._start_playback(guild_id, channel, voice_client, radio)
                return

        async def disconnect_after_idle():
            await asyncio.sleep(self.idle_timeout)
            if not voice_client.is_playing() and not voice_client.is_paused():
                await voice_client.disconnect()
                await channel.send("Disconnected due to inactivity.")

        self.bot.loop.create_task(disconnect_after_idle())

    def _has_permission(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        if member.guild.id in self.temp_djs and member.id in self.temp_djs[member.guild.id]:
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
        self.votes[guild_id] = set()

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

    async def _try_autoplay(self, guild_id: int, channel: discord.abc.Messageable) -> Optional[Track]:
        # Playlist-driven autoplay
        plist_name = self.autoplay_playlist.get(guild_id)
        if plist_name:
            plist = self.playlist_store.get_playlist(guild_id, plist_name)
            if plist:
                pos = self.autoplay_playlist_pos.get(guild_id, 0) % len(plist)
                for offset in range(len(plist)):
                    idx = (pos + offset) % len(plist)
                    entry = plist[idx]
                    query = entry.get("url") or entry.get("title")
                    try:
                        track = self.audio_source.resolve(query, requester="Radio")
                        self.autoplay_playlist_pos[guild_id] = idx + 1
                        track.source = entry.get("source", track.source)
                        return track
                    except Exception:
                        continue
                await channel.send("Autoplay playlist items failed to load.")
            else:
                await channel.send("Autoplay playlist not found; turning off playlist autoplay.")
                self.autoplay_playlist[guild_id] = None

        last = self.history.get(guild_id, [])
        if not last:
            return None
        seed = last[-1]
        history_urls = {t.url for t in last[-15:]}
        seed_id = self._youtube_id(seed.url)
        candidate_urls: List[str] = []

        if self.youtube_api_key and seed_id:
            params = {
                "part": "snippet",
                "maxResults": 8,
                "type": "video",
                "key": self.youtube_api_key,
                "relatedToVideoId": seed_id,
            }
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    "https://www.googleapis.com/youtube/v3/search",
                    params=params,
                    timeout=6,
                )
                if resp.ok:
                    data = resp.json()
                    for item in data.get("items", []):
                        vid = item["id"]["videoId"]
                        url = f"https://www.youtube.com/watch?v={vid}"
                        if url not in history_urls:
                            candidate_urls.append(url)
            except Exception:
                pass

        # If no related results, fall back to artist/title search
        if not candidate_urls:
            artist_hint = seed.title.split("-")[0].strip() if "-" in seed.title else seed.title
            search_query = f"{artist_hint} music"[:150]
            candidate_urls.append(f"ytsearch1:{search_query}")

        for url in candidate_urls:
            if url in history_urls:
                continue
            try:
                return self.audio_source.resolve(url, requester="Radio")
            except Exception:
                continue

        await channel.send("Autoplay failed to find a next track.")
        return None

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

    def _youtube_id(self, url: str) -> Optional[str]:
        if "youtube.com/watch?v=" in url:
            return url.split("watch?v=")[-1].split("&")[0]
        if "youtu.be/" in url:
            return url.split("youtu.be/")[-1].split("?")[0]
        return None

    def _push_history(self, guild_id: int, track: Track, limit: int = 50):
        hist = self.history.setdefault(guild_id, [])
        hist.append(track)
        if len(hist) > limit:
            del hist[:-limit]

    def _serialize_track(self, track: Track) -> dict:
        return {
            "title": track.title,
            "url": track.url,
            "duration": track.duration,
            "source": track.source,
        }

    def _calc_required_votes(self, vc: discord.VoiceClient) -> int:
        humans = [m for m in vc.channel.members if not m.bot]
        count = max(1, len(humans))
        return max(1, math.ceil(count / 2))

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

    @app_commands.command(name="history", description="Show recently played tracks.")
    async def history_cmd(self, interaction: discord.Interaction):
        hist = list(self.history.get(interaction.guild.id, []))[-10:]
        if not hist:
            await interaction.response.send_message("No history yet.", ephemeral=True)
            return
        lines = [f"{idx}. {t.title} [{t.source}]" for idx, t in enumerate(hist[::-1], 1)]
        embed = discord.Embed(title="Recently Played", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="autoplay", description="Toggle autoplay/radio when queue ends.")
    @app_commands.describe(enabled="Turn autoplay on or off")
    async def autoplay_cmd(self, interaction: discord.Interaction, enabled: bool):
        self.autoplay[interaction.guild.id] = enabled
        await interaction.response.send_message(f"Autoplay {'enabled' if enabled else 'disabled'}.", ephemeral=True)

    @app_commands.command(name="vskip", description="Vote to skip the current track.")
    async def vskip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc or not vc.is_connected() or not vc.channel:
            await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)
            return
        if self._has_permission(interaction.user):  # DJs/admins should just use /skip
            await interaction.response.send_message("You can use /skip directly.", ephemeral=True)
            return
        voters = self.votes.setdefault(interaction.guild.id, set())
        if interaction.user.id in voters:
            await interaction.response.send_message("You already voted to skip.", ephemeral=True)
            return
        voters.add(interaction.user.id)
        required = self._calc_required_votes(vc)
        if len(voters) >= required:
            vc.stop()
            await interaction.response.send_message("Vote threshold reached. Skipping...", ephemeral=False)
            await self._update_panels(interaction.guild.id)
        else:
            await interaction.response.send_message(
                f"Vote recorded ({len(voters)}/{required}).", ephemeral=True
            )

    @app_commands.command(name="djadd", description="Add a temporary DJ (admin/DJ only).")
    async def dj_add(self, interaction: discord.Interaction, member: discord.Member):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        self.temp_djs.setdefault(interaction.guild.id, set()).add(member.id)
        await interaction.response.send_message(f"{member.mention} is now a DJ for this session.", ephemeral=True)

    @app_commands.command(name="djremove", description="Remove a temporary DJ (admin/DJ only).")
    async def dj_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        self.temp_djs.setdefault(interaction.guild.id, set()).discard(member.id)
        await interaction.response.send_message(f"{member.mention} removed from DJ list.", ephemeral=True)

    @app_commands.command(name="djlist", description="List temporary DJs for this server.")
    async def dj_list(self, interaction: discord.Interaction):
        ids = self.temp_djs.get(interaction.guild.id, set())
        if not ids:
            await interaction.response.send_message("No temporary DJs set.", ephemeral=True)
            return
        mentions = []
        for mid in ids:
            member = interaction.guild.get_member(mid)
            mentions.append(member.mention if member else f"<@{mid}>")
        await interaction.response.send_message("Temporary DJs: " + ", ".join(mentions), ephemeral=True)

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

    @app_commands.command(name="playlist_save", description="Save the current queue as a playlist.")
    @app_commands.describe(name="Playlist name")
    async def playlist_save(self, interaction: discord.Interaction, name: str):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        current = self.current.get(interaction.guild.id)
        queue = await self.queue_manager.list_queue(interaction.guild.id)
        tracks = []
        if current:
            tracks.append(self._serialize_track(current))
        tracks.extend(self._serialize_track(t) for t in queue)
        if not tracks:
            await interaction.response.send_message("Nothing to save.", ephemeral=True)
            return
        self.playlist_store.save_playlist(interaction.guild.id, name, tracks)
        await interaction.response.send_message(f"Saved playlist '{name}' with {len(tracks)} tracks.", ephemeral=True)

    @app_commands.command(name="playlist_list", description="List saved playlists.")
    async def playlist_list(self, interaction: discord.Interaction):
        names = self.playlist_store.list_playlists(interaction.guild.id)
        if not names:
            await interaction.response.send_message("No playlists saved yet.", ephemeral=True)
            return
        await interaction.response.send_message("Playlists: " + ", ".join(names), ephemeral=True)

    @app_commands.command(name="playlist_delete", description="Delete a saved playlist.")
    @app_commands.describe(name="Playlist name")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        ok = self.playlist_store.delete_playlist(interaction.guild.id, name)
        if ok:
            await interaction.response.send_message(f"Deleted playlist '{name}'.", ephemeral=True)
        else:
            await interaction.response.send_message("Playlist not found.", ephemeral=True)

    @app_commands.command(name="playlist_load", description="Load a playlist into the queue.")
    @app_commands.describe(name="Playlist name")
    async def playlist_load(self, interaction: discord.Interaction, name: str):
        data = self.playlist_store.get_playlist(interaction.guild.id, name)
        if not data:
            await interaction.response.send_message("Playlist not found.", ephemeral=True)
            return
        vc = await self.ensure_voice_interaction(interaction)
        if not vc:
            return
        await interaction.response.defer(thinking=True)
        added_count = 0
        for entry in data:
            query = entry.get("url") or entry.get("title")
            try:
                track = self.audio_source.resolve(query, requester=str(interaction.user))
            except Exception:
                continue
            added = await self.queue_manager.add_track(interaction.guild.id, track)
            if added:
                added_count += 1
        if added_count == 0:
            await interaction.followup.send("Nothing added from playlist.", ephemeral=True)
            return
        if not vc.is_playing() and not vc.is_paused():
            next_track = await self.queue_manager.pop_next(interaction.guild.id)
            if next_track:
                await self._start_playback(interaction.guild.id, interaction.channel, vc, next_track, replace_panel=True)  # type: ignore
        await interaction.followup.send(f"Queued {added_count} tracks from '{name}'.", ephemeral=False)

    @app_commands.command(name="playlist_add", description="Add a track/query to a playlist.")
    @app_commands.describe(name="Playlist name", query="Track URL or search text")
    async def playlist_add(self, interaction: discord.Interaction, name: str, query: str):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            track = self.audio_source.resolve(query, requester=str(interaction.user))
        except Exception as exc:
            await interaction.followup.send(f"Could not resolve track: {exc}", ephemeral=True)
            return
        self.playlist_store.append_track(interaction.guild.id, name, self._serialize_track(track))
        await interaction.followup.send(f"Added **{track.title}** to playlist '{name}'.", ephemeral=True)

    @app_commands.command(name="playlist_import", description="Import tracks from a Spotify or YouTube playlist into a named playlist.")
    @app_commands.describe(name="Playlist name to save into", url="Spotify/YouTube playlist URL")
    async def playlist_import(self, interaction: discord.Interaction, name: str, url: str):
        if not self._has_permission(interaction.user):  # type: ignore
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        entries = self.audio_source.fetch_playlist_entries(url, limit=75)
        if not entries:
            await interaction.followup.send("Could not read that playlist.", ephemeral=True)
            return
        added = 0
        for entry in entries:
            query = entry.get("url") or entry.get("query") or entry.get("title")
            if not query:
                continue
            try:
                track = self.audio_source.resolve(query, requester=str(interaction.user))
            except Exception:
                continue
            self.playlist_store.append_track(interaction.guild.id, name, self._serialize_track(track))
            added += 1
        if added == 0:
            await interaction.followup.send("No tracks could be imported.", ephemeral=True)
        else:
            await interaction.followup.send(f"Imported {added} tracks into '{name}'.", ephemeral=True)

    @app_commands.command(name="playlist_show", description="Show contents of a playlist.")
    @app_commands.describe(name="Playlist name")
    async def playlist_show(self, interaction: discord.Interaction, name: str):
        data = self.playlist_store.get_playlist(interaction.guild.id, name)
        if not data:
            await interaction.response.send_message("Playlist not found.", ephemeral=True)
            return
        lines = []
        for idx, entry in enumerate(data[:15], 1):
            title = entry.get("title") or entry.get("url") or "Unknown"
            lines.append(f"{idx}. {title}")
        if len(data) > 15:
            lines.append(f"...and {len(data) - 15} more.")
        embed = discord.Embed(title=f"Playlist: {name}", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="playlist_autoplay", description="Use a playlist for autoplay when the queue ends.")
    @app_commands.describe(name="Playlist name or 'off'")
    async def playlist_autoplay(self, interaction: discord.Interaction, name: str):
        if name.lower() in ("off", "none"):
            self.autoplay_playlist[interaction.guild.id] = None
            self.autoplay_playlist_pos[interaction.guild.id] = 0
            await interaction.response.send_message("Playlist autoplay disabled.", ephemeral=True)
            return
        data = self.playlist_store.get_playlist(interaction.guild.id, name)
        if not data:
            await interaction.response.send_message("Playlist not found.", ephemeral=True)
            return
        self.autoplay_playlist[interaction.guild.id] = name
        self.autoplay_playlist_pos[interaction.guild.id] = 0
        self.autoplay[interaction.guild.id] = True
        await interaction.response.send_message(f"Autoplay will use playlist '{name}'.", ephemeral=True)

    @app_commands.command(name="help", description="Show bot help and onboarding.")
    async def help_cmd(self, interaction: discord.Interaction):
        desc = (
            "Use `/play <url|search>` or `/search` to add music.\n"
            "Controls: `/pause`, `/resume`, `/skip`, `/stop`, `/queue`, `/nowplaying`, `/volume`, `/clear`.\n"
            "Extras: `/panel`, `/history`, `/autoplay on|off`, `/playlist_save/load/add/show/delete`, `/playlist_autoplay`, `/vskip`, `/djadd`."
        )
        embed = discord.Embed(title="Music Bot Help", description=desc, color=0x5865F2)
        embed.add_field(name="DJ/Admin", value="Manage roles with `/setroles` (admin), add temps with `/djadd`.", inline=False)
        embed.set_footer(text="Tip: ensure FFmpeg is on PATH for stable playback.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
