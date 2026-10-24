import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone
import aiohttp
import asyncio
import os
import logging
import time

from cogs.config import has_role
from cogs.command_perms_core import check_enabled_or_reply, is_enabled_ctx

# ── Account-based stats (a real Minecraft account reads the /stats GUI) ─────
# This is the ONLY stats source. There is no API fallback.
# STATS_WORKER_DISCORD_ID : Discord ID whose linked MC session(s) do the lookups
#                           (the same ID you use on the dashboard's MC login).
MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")
STATS_WORKER_DISCORD_ID = os.getenv("STATS_WORKER_DISCORD_ID")
STATS_ATTEMPTS = 1            # tries per session before giving up
STATS_REQUEST_TIMEOUT = 240   # seconds; the bot queues lookups one at a time

logger = logging.getLogger(__name__)

# ── Stats channel lock ───────────────────────────────────────────────────────
# The bot owner adds channels on the admin panel's Stats Channels page (as many
# channels and servers as you like). /stats and !stats only work in those
# channels. Anywhere else (another channel, another server, DMs) the user is told where stats can be used
# (with an invite), unless that channel is ticked "Don't advertise" on the dashboard: hidden channels are
# never mentioned (/stats then only says it can't be used here, !stats stays silent).
# Stored in Mongo as a single global document (not per server):
#   bot_global_settings {_id: "stats_channel", channels: [
#       {channel_id, guild_id, guild_name, channel_name, invite_url, set_by, no_advertise}, ...]}
# (The old single-channel format - channel_id etc. at the top level - is still
# read, and is converted the next time the admin panel saves the list.)
# No channels listed = no restriction (the old behaviour).
SETTINGS_COLLECTION = "bot_global_settings"
STATS_CHANNEL_DOC_ID = "stats_channel"
_ENTRY_KEYS = ("channel_id", "guild_id", "guild_name", "channel_name", "invite_url", "set_by", "no_advertise")
NO_HERE_MESSAGE = "❌ You can't use this command here."
INVITE_RECHECK_SECONDS = 600  # re-validate the saved invite at most every 10 min

# ── Dashboard permission check (same convention as cogs/utilities.py) ────────
# db["command_perms"].find_one({"guild_id": ..., "command_name": "stats"}) ->
# {"roles": [role_name, ...]} — command_perms stores role NAMES (see
# commands.html), not IDs, matching cogs.config.has_role. Empty/missing doc
# = default perms = everyone. /stats is also usable in DMs (see
# allowed_contexts below), where there's no guild/roles to check against, so
# a DM invocation always passes through.

async def check_command_perm(interaction: discord.Interaction, command_name: str) -> bool:
    if interaction.guild is None:
        return True  # DM context — no roles to check
    if not await check_enabled_or_reply(interaction, command_name):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    db = interaction.client.db
    if db is None:
        return True
    doc = db["command_perms"].find_one({"guild_id": interaction.guild.id, "command_name": command_name})
    role_names = doc.get("roles", []) if doc else []
    if not role_names:
        return True  # no restriction configured -> everyone can use it
    if not has_role(interaction, *role_names):
        await interaction.response.send_message(
            f"❌ You don't have permission to use `/{command_name}`.", ephemeral=True
        )
        return False
    return True


def check_command_perm_ctx(guild: discord.Guild | None, author: discord.abc.User, db) -> bool:
    """Same check as check_command_perm, but for the prefix (`!stats`) form,
    which has a discord.ext.commands.Context instead of an Interaction and
    has to send its own denial message rather than relying on
    interaction.response. Returns True/False only — callers handle messaging."""
    if guild is None:
        return True  # DMs — !stats has no guild context to check roles against
    member = guild.get_member(author.id) if hasattr(guild, "get_member") else None
    if member is None or not hasattr(member, "guild_permissions"):
        return True
    if member.guild_permissions.administrator:
        return True
    if db is None:
        return True
    doc = db["command_perms"].find_one({"guild_id": guild.id, "command_name": "stats"})
    role_names = doc.get("roles", []) if doc else []
    if not role_names:
        return True
    user_role_names = {r.name for r in member.roles}
    return bool(user_role_names.intersection(role_names))

ACCOUNT_STAT_KEYS = {
    "balance": "money", "shards": "shards", "kills": "kills", "deaths": "deaths",
    "playtime": "playtime", "blocks_placed": "blocks_placed",
    "blocks_broken": "blocks_broken", "mobs_killed": "mobs_killed",
}

# error_code from the MC bot -> what the user sees
ERROR_MESSAGES = {
    "not_found":      "This user doesn't exist.",
    "not_configured": "Stats aren't set up yet (no worker account configured).",
    "no_session":     "The stats account isn't linked right now.",
    "not_ready":      "The stats account is offline or reconnecting. Try again in a few seconds.",
    "no_gui":         "DonutSMP didn't open the stats menu. The name may be wrong, or the server is lagging.",
    "title_mismatch": "DonutSMP opened a different player's menu. Try again.",
    "incomplete":     "The stats menu opened but didn't fully load. Try again.",
    "busy":           "Too many stats lookups at once. Try again in a few seconds.",
    "bad_user":       "That doesn't look like a valid Minecraft username.",
    "timeout":        "The stats lookup timed out. Try again.",
}
RETRYABLE = {"not_ready", "no_gui", "incomplete", "title_mismatch", "timeout"}


async def lookup_stats(db, ign: str):
    """Ask the MC bot to run `/stats <ign>` on the worker account and read the
    GUI. Returns (stats, None) on success, where stats is
    {stat: display_string} exactly as the server shows it ('420K', '45d 19h'),
    or (None, error_code) on failure. Retries transient failures."""
    if not STATS_WORKER_DISCORD_ID or db is None:
        return None, "not_configured"
    try:
        docs = await asyncio.to_thread(
            lambda: list(db["mc_sessions"].find({"discord_id": str(STATS_WORKER_DISCORD_ID)}, {"session_id": 1}))
        )
    except Exception as e:
        logger.error(f"Stats worker: couldn't read mc_sessions: {e}")
        return None, "no_session"
    if not docs:
        logger.warning("Stats worker: no MC session found for STATS_WORKER_DISCORD_ID.")
        return None, "no_session"

    last_code = "no_session"
    timeout = aiohttp.ClientTimeout(total=STATS_REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for doc in docs:
            for attempt in range(1, STATS_ATTEMPTS + 1):
                try:
                    async with session.get(
                        f"{MC_BOT_URL}/stats-lookup/{doc['session_id']}", params={"user": ign}
                    ) as resp:
                        data = await resp.json()
                except asyncio.TimeoutError:
                    last_code = "timeout"
                    logger.warning(f"Stats lookup for {ign}: timeout (attempt {attempt})")
                    continue
                except Exception as e:
                    last_code = "not_ready"
                    logger.error(f"Stats worker request failed: {e}")
                    break  # bot service unreachable; retrying the same session won't help
                if data.get("ok"):
                    raw = data.get("stats") or {}
                    out = {stat: (raw.get(key) or {}).get("raw") for stat, key in ACCOUNT_STAT_KEYS.items()}
                    out["online"] = data.get("online")  # True / False / None (ping got no reply)
                    if out["online"] is None:
                        logger.warning(f"Stats lookup for {ign}: stats OK but /ping gave no usable reply "
                                       f"(online status unknown) - see the MC bot log for the chat it saw.")
                    return out, None
                code = data.get("error_code") or "error"
                last_code = code
                logger.warning(f"Stats lookup for {ign}: {code} - {data.get('error')} (attempt {attempt})")
                if code == "not_ready":
                    break  # this session isn't connected; try the next session
                if code not in RETRYABLE:
                    return None, code
                await asyncio.sleep(1.5)
    return None, last_code


# Maps our internal emoji "slots" to the exact names of the application
# emojis you uploaded to the bot (see EmojiCache below for how these get
# resolved to actual <:name:id> strings at runtime).
EMOJI_NAMES = {
    "balance":       "emerald",
    "shards":        "shard",
    "kills":         "sword",
    "deaths":        "skull",
    "playtime":      "clock",
    "blocks_placed": "stone",
    "blocks_broken": "cobblestone",
    "mobs_killed":   "zombie_head",
    "online":        "online",
    "offline":       "offline",
}


class EmojiCache:
    """Fetches the bot's application emojis once and caches them, so every
    !stats/`/stats` call doesn't hit the API again. Falls back to a plain
    ':name:' placeholder if an emoji hasn't been uploaded / isn't found yet,
    so a missing emoji never crashes the command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: dict[str, str] | None = None
        self._lock = None

    async def get(self, name: str) -> str:
        if self._cache is None:
            await self._load()
        return self._cache.get(name, f":{name}:")

    async def _load(self):
        self._cache = {}
        try:
            app_emojis = await self.bot.fetch_application_emojis()
            for e in app_emojis:
                self._cache[e.name] = str(e)
        except Exception as e:
            logger.error(f"Failed to fetch application emojis: {e}")

    def invalidate(self):
        """Call this (e.g. from a re-sync command) if you add/remove
        application emojis after the bot has already cached them."""
        self._cache = None


def _build_embed(ign: str, stats: dict, emoji: dict) -> discord.Embed:
    online = stats.get("online")
    if online is True:
        status_emoji, status_text, color = emoji["online"], f"{ign} is currently Online", discord.Color.green()
    elif online is False:
        status_emoji, status_text, color = emoji["offline"], f"{ign} is currently Offline", discord.Color.red()
    else:
        status_emoji, status_text, color = "", "Live from DonutSMP", discord.Color.blurple()
    embed = discord.Embed(
        title=f"{status_emoji} {ign}'s Statistics".strip(),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{ign}/128")
    rows = [
        ("balance", "Balance"), ("shards", "Shards"), ("kills", "Kills"),
        ("deaths", "Deaths"), ("playtime", "Playtime"),
        ("blocks_placed", "Blocks Placed"), ("blocks_broken", "Blocks Broken"),
        ("mobs_killed", "Mobs Killed"),
    ]
    value = "\n".join(f"{emoji[k]} **{label}:** `{stats.get(k) or 'N/A'}`" for k, label in rows)
    embed.add_field(name="\u200b", value=value, inline=False)
    embed.set_footer(text=status_text)
    return embed


class Stats(commands.Cog):
    """!stats <ign> / /stats <ign> — player stats (real account GUI only). Locked to the channels added in the admin panel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.emoji_cache = EmojiCache(bot)

        self._invite_checked: dict[str, float] = {}
        self._invite_mem: dict[int, str] = {}

    # ── Stats channel lock helpers ───────────────────────────────────────
    def _load_entries(self) -> list[dict]:
        """Every allowed stats channel; [] means no restriction."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return []
        try:
            doc = db[SETTINGS_COLLECTION].find_one({"_id": STATS_CHANNEL_DOC_ID})
        except Exception as e:
            logger.error(f"Stats channel lock: couldn't read settings: {e}")
            return []  # fail open: never let a DB hiccup kill /stats
        if not doc:
            return []
        if isinstance(doc.get("channels"), list):
            return [e for e in doc["channels"] if e.get("channel_id")]
        if doc.get("channel_id"):  # old single-channel format
            return [{k: doc.get(k) for k in _ENTRY_KEYS}]
        return []

    def _save_entries(self, entries: list[dict]):
        coll = self.bot.db[SETTINGS_COLLECTION]
        if not entries:
            coll.delete_one({"_id": STATS_CHANNEL_DOC_ID})
            return
        coll.update_one(
            {"_id": STATS_CHANNEL_DOC_ID},
            {"$set": {"channels": entries}, "$unset": {k: "" for k in _ENTRY_KEYS}},
            upsert=True,
        )

    async def _resolve_channel(self, entry: dict):
        channel_id = int(entry["channel_id"])
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await self.bot.fetch_channel(channel_id)
        except discord.HTTPException:  # NotFound / Forbidden / transient
            return None

    async def _invite_for(self, entry: dict, channel) -> str | None:
        """A permanent invite to that channel's server: reuse the saved one if it
        is still valid, otherwise create a fresh one and save it."""
        cid = int(entry["channel_id"])
        url = entry.get("invite_url") or self._invite_mem.get(cid)
        now = time.monotonic()
        if url:
            last_checked = self._invite_checked.get(url)
            if last_checked is not None and now - last_checked < INVITE_RECHECK_SECONDS:
                return url
            try:
                await self.bot.fetch_invite(url)
                self._invite_checked[url] = now
                return url
            except discord.NotFound:
                url = None  # deleted/expired -> make a new one below
            except discord.HTTPException:
                return url  # transient error: keep using the saved link
        try:
            invite = await channel.create_invite(
                max_age=0, max_uses=0, unique=False, reason="Stats channel invite"
            )
        except discord.HTTPException as e:
            logger.warning(f"Stats channel lock: couldn't create an invite: {e}")
            return None
        self._invite_mem[cid] = invite.url
        try:
            self.bot.db[SETTINGS_COLLECTION].update_one(
                {"_id": STATS_CHANNEL_DOC_ID, "channels.channel_id": cid},
                {"$set": {"channels.$.invite_url": invite.url}},
            )
        except Exception as e:
            logger.error(f"Stats channel lock: couldn't save invite: {e}")
        self._invite_checked[invite.url] = now
        return invite.url

    async def _stats_lock_message(self, guild_id, channel_id, parent_id=None) -> str | None:
        """None -> stats may be used here. Otherwise the text to show instead."""
        entries = self._load_entries()
        if not entries:
            return None  # not configured -> unrestricted
        allowed = {int(e["channel_id"]) for e in entries}
        if channel_id in allowed or (parent_id is not None and parent_id in allowed):  # parent_id = thread inside one
            return None

        reachable = []
        for e in entries:
            ch = await self._resolve_channel(e)
            if ch is not None:
                reachable.append((e, ch))
        if not reachable:
            logger.warning("None of the stats channels are reachable any more - allowing /stats "
                           "everywhere until a reachable channel is added in the admin panel.")
            return None  # fail open so /stats never just dies

        # Channels ticked "Don't advertise" on the dashboard are never mentioned.
        reachable = [(e, ch) for e, ch in reachable if not e.get("no_advertise")]
        if not reachable:
            return NO_HERE_MESSAGE

        # Used inside one of the stats servers, just in the wrong channel
        here = [ch for e, ch in reachable if guild_id is not None and int(e["guild_id"]) == guild_id]
        if here:
            return f"❌ Stats can only be used in {', '.join(ch.mention for ch in here)}."
        if guild_id is not None and any(int(e["guild_id"]) == guild_id for e in entries):
            return NO_HERE_MESSAGE      # this server has stats channels, but all of them are hidden

        # Another server, or DMs: list every advertised stats server with an invite
        by_guild: dict[int, list] = {}
        for e, ch in reachable:
            by_guild.setdefault(int(e["guild_id"]), []).append((e, ch))
        lines = ["❌ Stats can only be used in these channels:"]
        for items in list(by_guild.values())[:5]:
            first_entry, first_ch = items[0]
            server = getattr(getattr(first_ch, "guild", None), "name", None) or first_entry.get("guild_name") or "the stats server"
            chans = ", ".join(f"**#{getattr(ch, 'name', None) or e.get('channel_name') or 'stats'}**" for e, ch in items)
            line = f"• **{server}**: {chans}"
            invite = await self._invite_for(first_entry, first_ch)
            if invite:
                line += f"\n   👉 {invite}"
            lines.append(line)
        return "\n".join(lines)

    # Stats channels are managed in the admin panel (Stats Channels page), not by slash commands.

    async def _resolve_emojis(self) -> dict:
        return {slot: await self.emoji_cache.get(name) for slot, name in EMOJI_NAMES.items()}

    async def _send_stats(self, send_func, ign: str):
        stats, code = await lookup_stats(getattr(self.bot, "db", None), ign)
        if not stats:
            msg = ERROR_MESSAGES.get(code, "Couldn't fetch stats right now. Try again.")
            await send_func(f"❌ `{ign}`: {msg}")
            return
        emoji = await self._resolve_emojis()
        await send_func(embed=_build_embed(ign, stats, emoji))

    @app_commands.command(name="stats", description="View a player's DonutSMP stats.")
    @app_commands.describe(ign="Minecraft username to look up")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def stats_slash(self, interaction: discord.Interaction, ign: str):
        lock = await self._stats_lock_message(
            interaction.guild_id, interaction.channel_id, getattr(interaction.channel, "parent_id", None)
        )
        if lock:
            await interaction.response.send_message(lock, ephemeral=True)
            return
        if not await check_command_perm(interaction, "stats"):
            return
        await interaction.response.defer()
        await self._send_stats(interaction.followup.send, ign)

    @commands.command(name="stats")
    async def stats_prefix(self, ctx: commands.Context, ign: str = None):
        lock = await self._stats_lock_message(
            ctx.guild.id if ctx.guild else None, ctx.channel.id, getattr(ctx.channel, "parent_id", None)
        )
        if lock:
            if lock != NO_HERE_MESSAGE:     # hidden channels: stay silent instead of replying
                await ctx.send(lock)
            return
        if ctx.guild is not None and not is_enabled_ctx(ctx.guild, self.bot.db, "stats"):
            await ctx.send("❌ `!stats` is disabled in this server.")
            return
        if not check_command_perm_ctx(ctx.guild, ctx.author, self.bot.db):
            await ctx.send("❌ You don't have permission to use `!stats`.")
            return
        if not ign:
            await ctx.send("❌ Usage: `!stats <ign>`")
            return
        async with ctx.typing():
            await self._send_stats(ctx.send, ign)


async def setup(bot: commands.Bot):
    await bot.add_cog(Stats(bot))



