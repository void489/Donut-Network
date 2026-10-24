"""
cogs/afk.py

AFK status tracking, with a visible nickname marker.

Setting AFK (via /afk or the AI agent's set_afk tool) prefixes the member's
current display name with "[AFK] " so it's visible everywhere (member list,
mentions, etc.), not just in-channel replies. Coming back — posting any
message, or having staff call clear_afk — restores the exact
nickname the member had before going AFK (including no nickname at all).

State lives in a module-level dict (for fast, low-latency reads on every
single message) that's mirrored to MongoDB's "afk" collection on every
write. The dict is repopulated from the DB on cog load, and clear_afk_entry
falls back to a direct DB read if a guild/user isn't in memory — this is
what makes AFK survive bot restarts *and* stay correct if the AI agent's
set_afk/clear_afk tools ever run from a different process than the bot's
own gateway connection (e.g. the web dashboard running as its own worker).
Same pattern as cogs.moderation._warnings + save_user_warnings.
"""

import logging
import time

import discord
from discord.ext import commands
from discord import app_commands
from utils.permissions import slash_check

logger = logging.getLogger(__name__)

# {guild_id: {user_id: {"reason": str, "original_nick": str | None}}}
_afk: dict[int, dict[int, dict]] = {}

AFK_PREFIX = "[AFK] "
NICK_MAX_LEN = 32  # Discord's hard cap on guild nicknames


def is_afk(guild_id: int, user_id: int) -> bool:
    return user_id in _afk.get(guild_id, {})


def get_afk(guild_id: int, user_id: int) -> dict | None:
    return _afk.get(guild_id, {}).get(user_id)


def load_afk_from_db(db) -> None:
    """Repopulates the in-memory cache from MongoDB. Call once on bot startup."""
    if db is None:
        return
    _afk.clear()
    try:
        for doc in db["afk"].find({}):
            guild_id = doc.get("guild_id")
            user_id = doc.get("user_id")
            if guild_id is None or user_id is None:
                continue
            _afk.setdefault(guild_id, {})[user_id] = {
                "reason": doc.get("reason", "No reason provided"),
                "original_nick": doc.get("original_nick"),
            }
    except Exception:
        # Don't block bot startup over this — AFK just won't be pre-populated.
        pass


SELF_SET_GRACE_SECONDS = 2.0  # how long a freshly-set entry is immune to the on_message clear


def set_afk_entry(guild_id: int, user_id: int, reason: str, original_nick: str | None, db=None):
    # "set_at" is only used locally (see on_message) to recognize that the
    # message which just set AFK shouldn't immediately clear it again - it's
    # not meaningful across restarts, but harmless to persist.
    entry = {"reason": reason, "original_nick": original_nick, "set_at": time.monotonic()}
    _afk.setdefault(guild_id, {})[user_id] = entry
    if db is not None:
        try:
            db["afk"].update_one(
                {"guild_id": guild_id, "user_id": user_id},
                {"$set": {"guild_id": guild_id, "user_id": user_id, **entry}},
                upsert=True,
            )
        except Exception:
            pass


def clear_afk_entry(guild_id: int, user_id: int, db=None) -> dict | None:
    """Removes and returns the entry, or None if the member wasn't AFK.

    Checks the in-memory cache first; if it's missing there (e.g. this
    process didn't see the original set_afk call), falls back to the DB
    before giving up, so a real AFK status never gets reported as absent.
    """
    guild_afk = _afk.setdefault(guild_id, {})
    entry = guild_afk.pop(user_id, None)

    if entry is None and db is not None:
        try:
            doc = db["afk"].find_one({"guild_id": guild_id, "user_id": user_id})
        except Exception:
            doc = None
        if doc is not None:
            entry = {"reason": doc.get("reason", "No reason provided"), "original_nick": doc.get("original_nick")}

    if entry is not None and db is not None:
        try:
            db["afk"].delete_one({"guild_id": guild_id, "user_id": user_id})
        except Exception:
            pass

    return entry


def afk_nickname(base_name: str) -> str:
    """Builds the '[AFK] Name' nickname, trimmed to fit Discord's 32-char cap."""
    return (AFK_PREFIX + base_name)[:NICK_MAX_LEN]


def parse_afk_args(ctx, remainder: str):
    """Text-bridge parser for /afk -> !afk [reason].

    The slash command's only parameter is a free-text `reason` string with
    a default, so this just passes the whole remainder through as-is (or
    no args at all, to fall back to the callback's own default) — no
    member/role resolution needed.
    """
    remainder = remainder.strip()
    if not remainder:
        return []
    return [remainder]


class AFK(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Repopulate the in-memory cache from MongoDB so AFK survives restarts.
        load_afk_from_db(getattr(self.bot, "db", None))

        # Let /afk also be invoked as a text command (e.g. "!afk brb lunch"),
        # honoring any custom prefix set for "afk" on the dashboard.
        if hasattr(self.bot, "register_text_bridge"):
            self.bot.register_text_bridge("afk", self, self.afk, parse_afk_args)

    @app_commands.command(name="afk", description="Set your AFK status with an optional reason")
    @app_commands.describe(reason="Why you're going AFK")
    @slash_check()
    async def afk(self, interaction: discord.Interaction, reason: str = "No reason provided"):
        guild = interaction.guild
        member = interaction.user

        set_afk_entry(guild.id, member.id, reason, member.nick, db=getattr(self.bot, "db", None))

        new_nick = afk_nickname(member.display_name)
        try:
            await member.edit(nick=new_nick, reason=f"AFK: {reason}"[:512])
        except discord.Forbidden:
            # Missing Manage Nicknames, or target outranks the bot (e.g. the
            # server owner) — status is still tracked even if the nick can't
            # be changed.
            pass
        except discord.HTTPException:
            pass

        embed = discord.Embed(
            description=f"💤 {member.mention} is now AFK: **{reason}**",
            color=discord.Color.greyple()
        )
        await interaction.response.send_message(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        db = getattr(self.bot, "db", None)

        # If this message is the very one that just set AFK (e.g. `!afk brb`
        # via the text bridge, which is itself a real message and therefore
        # also reaches this listener), don't treat it as "coming back" — the
        # bridge's afk() callback runs first and stamps a fresh "set_at" on
        # the entry, so a freshly-set entry gets a short grace window instead
        # of being cleared in the same instant it was created.
        existing_entry = _afk.get(guild_id, {}).get(message.author.id)
        just_set = (
            existing_entry is not None
            and time.monotonic() - existing_entry.get("set_at", 0) < SELF_SET_GRACE_SECONDS
        )

        # If the author was AFK, clear their status and restore their nickname.
        entry = None if just_set else clear_afk_entry(guild_id, message.author.id, db=db)
        if entry is not None:
            try:
                await message.author.edit(
                    nick=entry["original_nick"], reason="No longer AFK"
                )
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"AFK: couldn't restore nick for {message.author.id}: {e}")
            try:
                await message.channel.send(
                    f"✅ Welcome back {message.author.mention}! Your AFK status has been removed.",
                    delete_after=5
                )
            except discord.HTTPException as e:
                logger.warning(f"AFK: couldn't send welcome-back message in {message.channel.id}: {e}")

        # Check if any mentioned user or replied-to user is AFK
        guild_afk = _afk.get(guild_id, {})
        logger.debug(f"AFK: guild {guild_id} currently AFK: {list(guild_afk.keys())}, mentions: {[u.id for u in message.mentions]}")
        afk_hits: list[tuple[discord.Member, str]] = []

        # Direct mentions
        for user in message.mentions:
            if user.id in guild_afk and user.id != message.author.id:
                afk_hits.append((user, guild_afk[user.id]["reason"]))

        # Reply reference
        if message.reference and message.reference.resolved:
            ref = message.reference.resolved
            if isinstance(ref, discord.Message):
                ref_author = ref.author
                if (
                    ref_author.id in guild_afk
                    and ref_author.id != message.author.id
                    and ref_author not in message.mentions  # avoid duplicates
                ):
                    afk_hits.append((ref_author, guild_afk[ref_author.id]["reason"]))

        for afk_user, reason in afk_hits:
            try:
                await message.channel.send(
                    f"💤 {afk_user.mention} is AFK: **{reason}**",
                    delete_after=5
                )
            except discord.HTTPException as e:
                logger.warning(f"AFK: couldn't send AFK-ping notice for {afk_user.id} in {message.channel.id}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AFK(bot))



