"""
Command permissions for the whole bot.

Every command is locked to server Administrators until you open it up on the
dashboard's **Command Permissions** page. There, per command, you choose:

  * Enabled          - turn the command on at all
  * Allowed roles    - which roles may use it (you pick them yourself)
  * Everyone         - let every member use it (for public commands)
  * Help category    - "public" (shown by /help) or "staff" (shown by /staffhelp)

Nothing is tied to hardcoded "Admin" / "Moderator" role names.

Use:
    @command_check()   - on hybrid commands (work as / and prefix commands)
    @slash_check()     - on slash-only (app_commands) commands

Both register the command on the dashboard automatically and enforce the same
rules, so every command shows up in one place.
"""
import functools

import discord
from discord import app_commands
from discord.ext import commands

from database import models
from utils.embeds import error_embed

NOT_ENABLED_MSG = (
    "This command hasn't been enabled yet. Ask an admin to turn it on in the "
    "dashboard's **Command Permissions** page."
)
NO_PERMISSION_MSG = "You don't have permission to use this command."


def member_allowed(member, perm: dict) -> tuple:
    """Pure check (no DB access). Returns (allowed, reason_message_or_None)."""
    if member is None:
        return False, NO_PERMISSION_MSG
    if member.guild_permissions.administrator:
        return True, None
    if perm.get("admin_only", True) or not perm.get("enabled", False):
        return False, NOT_ENABLED_MSG
    if perm.get("everyone"):
        return True, None
    allowed_roles = {str(r) for r in perm.get("allowed_roles", [])}
    if allowed_roles & {str(r.id) for r in member.roles}:
        return True, None
    return False, NO_PERMISSION_MSG


async def _is_bypass(guild_id: int, member) -> bool:
    """Bot owner, or a member holding a Bot Bypass (Staff) role from the dashboard."""
    from utils.personality import OWNER_USER_ID
    if OWNER_USER_ID and str(member.id) == str(OWNER_USER_ID):
        return True
    try:
        from database.mongo import get_db
        doc = await get_db()["bot_config"].find_one({"guild_id": guild_id}, {"BOT_BYPASS_ROLES": 1}) or {}
    except Exception:  # noqa: BLE001
        return False
    ids = {str(r) for r in (doc.get("BOT_BYPASS_ROLES") or [])}
    return bool(ids & {str(r.id) for r in getattr(member, "roles", [])})


async def evaluate(guild_id: int, member, command_name: str, cog_name: str) -> tuple:
    """Register the command (so it appears on the dashboard) and check access."""
    await models.register_command(guild_id, command_name, cog_name)
    if member is not None and await _is_bypass(guild_id, member):
        return True, None
    perm = await models.get_command_permission(guild_id, command_name)
    return member_allowed(member, perm)


def command_check():
    """Decorator for hybrid commands."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, ctx, *args, **kwargs):
            if ctx.guild is None:
                await ctx.send(embed=error_embed("This command can only be used in a server."))
                return
            ok, msg = await evaluate(
                ctx.guild.id, ctx.author, ctx.command.qualified_name, self.__class__.__name__
            )
            if not ok:
                await ctx.send(embed=error_embed(msg), ephemeral=True)
                return
            return await func(self, ctx, *args, **kwargs)
        wrapper.__command_check__ = True
        return wrapper
    return decorator


def slash_check():
    """Decorator for slash-only (app_commands) commands. Put it under @app_commands.command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("This command can only be used in a server."), ephemeral=True
            )
            return False
        cmd = interaction.command
        binding = getattr(cmd, "binding", None)
        cog_name = binding.__class__.__name__ if binding is not None else "Unknown"
        ok, msg = await evaluate(interaction.guild.id, interaction.user, cmd.qualified_name, cog_name)
        if not ok:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(msg), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(msg), ephemeral=True)
            return False
        return True

    predicate.__command_check__ = True
    return app_commands.check(predicate)


async def get_prefix_for_guild(bot, message):
    """Per-guild custom prefix from the dashboard, falling back to '/'."""
    if message.guild is None:
        return commands.when_mentioned_or("/")(bot, message)
    prefix = await models.get_prefix(message.guild.id)
    return commands.when_mentioned_or(prefix)(bot, message)


def _is_checked(cmd) -> bool:
    cb = getattr(cmd, "callback", None)
    if getattr(cb, "__command_check__", False):
        return True
    return any(getattr(c, "__command_check__", False) for c in getattr(cmd, "checks", []) or [])


def iter_checked_commands(bot):
    """Yield (qualified_name, cog_name, description, usage) for every protected command.
    Walks both prefix/hybrid commands and slash commands, de-duplicated by name."""
    seen = set()
    for cmd in bot.walk_commands():
        if _is_checked(cmd) and cmd.qualified_name not in seen:
            seen.add(cmd.qualified_name)
            cog = cmd.cog.__class__.__name__ if cmd.cog else "Unknown"
            desc = getattr(cmd, "description", None) or cmd.help or ""
            yield cmd.qualified_name, cog, desc, (cmd.signature or "")
    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue
        if _is_checked(cmd) and cmd.qualified_name not in seen:
            seen.add(cmd.qualified_name)
            binding = getattr(cmd, "binding", None)
            cog = binding.__class__.__name__ if binding is not None else "Unknown"
            usage = " ".join(
                f"<{p.name}>" if p.required else f"[{p.name}]" for p in getattr(cmd, "parameters", [])
            )
            yield cmd.qualified_name, cog, (cmd.description or ""), usage


async def register_all_commands(bot, guild_ids=None):
    """Register every protected command for the given guilds (default: all) so they
    all show on the Command Permissions page without anyone running them first.
    Also stores each command's description + usage so the dashboard AI knows how to run it."""
    targets = list(iter_checked_commands(bot))
    if guild_ids is None:
        guild_ids = [g.id for g in bot.guilds]
    for gid in guild_ids:
        for name, cog_name, desc, usage in targets:
            await models.register_command(gid, name, cog_name, desc, usage)
    print(f"[permissions] registered {len(targets)} command(s) for {len(guild_ids)} guild(s)")
