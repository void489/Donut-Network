"""
command_perms_core.py

Shared, non-cog helpers for per-guild command configuration: role
restrictions (existing feature), enable/disable, and custom prefixes.
Import this from bot.py / cogs so the enable + prefix checks live in one
place instead of being copy-pasted into every cog.

Collection used:
  command_perms {
      guild_id,
      command_name,     # e.g. "mute" or "giveaway create"
      roles: [str],     # role NAMES allowed to use it (empty/missing = default level)
      enabled: bool,     # default True
      prefix: str | None # custom prefix override for this command, None = use bot default
  }
"""

DEFAULT_ENABLED = True

import discord


def get_command_config(db, guild_id: int, command_name: str) -> dict:
    """Return the raw config doc for a command, with sane defaults filled
    in if nothing has been saved yet."""
    if db is None:
        return {"guild_id": guild_id, "command_name": command_name,
                "roles": [], "enabled": DEFAULT_ENABLED, "prefix": None}

    doc = db["command_perms"].find_one({"guild_id": guild_id, "command_name": command_name}) or {}
    return {
        "guild_id": guild_id,
        "command_name": command_name,
        "roles": doc.get("roles", []),
        "enabled": doc.get("enabled", DEFAULT_ENABLED),
        "prefix": doc.get("prefix") or None,
    }


def is_command_enabled(db, guild_id: int, command_name: str) -> bool:
    if db is None:
        return True
    doc = db["command_perms"].find_one(
        {"guild_id": guild_id, "command_name": command_name}, {"enabled": 1}
    )
    if doc is None:
        return DEFAULT_ENABLED
    return doc.get("enabled", DEFAULT_ENABLED)


def get_command_prefix(db, guild_id: int, command_name: str, default_prefix: str = "!") -> str:
    """Returns the per-command prefix override if one is set, otherwise
    falls back to `default_prefix` (usually the bot's global prefix)."""
    if db is None:
        return default_prefix
    doc = db["command_perms"].find_one(
        {"guild_id": guild_id, "command_name": command_name}, {"prefix": 1}
    )
    if doc and doc.get("prefix"):
        return doc["prefix"]
    return default_prefix


def set_command_enabled(db, guild_id: int, command_name: str, enabled: bool):
    if db is None:
        return
    db["command_perms"].update_one(
        {"guild_id": guild_id, "command_name": command_name},
        {"$set": {"guild_id": guild_id, "command_name": command_name, "enabled": enabled}},
        upsert=True,
    )


def set_command_prefix(db, guild_id: int, command_name: str, prefix: str | None):
    if db is None:
        return
    db["command_perms"].update_one(
        {"guild_id": guild_id, "command_name": command_name},
        {"$set": {"guild_id": guild_id, "command_name": command_name, "prefix": prefix or None}},
        upsert=True,
    )


def get_all_command_configs(db, guild_id: int) -> dict:
    """Returns {command_name: {roles, enabled, prefix}} for every command
    that has *any* saved config in this guild (role perms, disabled, or a
    custom prefix)."""
    if db is None:
        return {}
    out = {}
    for doc in db["command_perms"].find({"guild_id": guild_id}):
        out[doc["command_name"]] = {
            "roles": doc.get("roles", []),
            "enabled": doc.get("enabled", DEFAULT_ENABLED),
            "prefix": doc.get("prefix") or None,
        }
    return out


async def check_enabled_or_reply(interaction, command_name: str) -> bool:
    """Interaction-based convenience check for slash commands: call this
    first (before any role check) in each command's callback.

        if not await check_enabled_or_reply(interaction, "mute"):
            return

    Sends the "this command is disabled" ephemeral message itself and
    returns False if the command has been turned off on the dashboard for
    this guild; returns True otherwise (including when db is unavailable).
    """
    db = getattr(interaction.client, "db", None)
    guild = getattr(interaction, "guild", None)
    if db is None or guild is None:
        return True
    if not is_command_enabled(db, guild.id, command_name):
        await interaction.response.send_message(
            f"❌ `/{command_name}` is disabled in this server.", ephemeral=True
        )
        return False
    return True


def is_enabled_ctx(guild, db, command_name: str) -> bool:
    """Same check as check_enabled_or_reply, but for the prefix-command
    (`!command`) form, which has a discord.ext.commands.Context instead of
    an Interaction. Returns True/False only — callers handle messaging,
    matching the convention used by cogs/stats.py's check_command_perm_ctx.
    """
    if guild is None or db is None:
        return True
    return is_command_enabled(db, guild.id, command_name)


class _FakeResponse:
    """Stands in for interaction.response inside a bridged text command."""
    def __init__(self, ctx, parent):
        self._ctx = ctx
        self._parent = parent
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content=None, *, embed=None, embeds=None, ephemeral=False, view=None, **kwargs):
        self._done = True
        msg = await self._ctx.send(content=content, embed=embed, embeds=embeds or [], view=view)
        self._parent._original_message = msg
        return msg

    async def defer(self, *args, **kwargs):
        self._done = True
        # A real deferred interaction has no visible message until a
        # followup/edit — but a text command needs *something* for
        # original_response()/edit_original_response() to target, since
        # there's no interaction token to defer. Send a lightweight
        # placeholder so callbacks that defer-then-edit still work
        # unmodified.
        if self._parent._original_message is None:
            self._parent._original_message = await self._ctx.send("⏳ Working...")


class _FakeFollowup:
    def __init__(self, ctx, parent):
        self._ctx = ctx
        self._parent = parent

    async def send(self, content=None, *, embed=None, embeds=None, ephemeral=False, view=None, **kwargs):
        msg = await self._ctx.send(content=content, embed=embed, embeds=embeds or [], view=view)
        if self._parent._original_message is None:
            self._parent._original_message = msg
        return msg


class TextBridgeInteraction:
    """Adapter that makes a discord.ext.commands.Context look enough like
    a discord.Interaction that an existing slash-command callback (written
    against `interaction.response.send_message(...)`, `interaction.user`,
    `interaction.guild`, `interaction.client`, etc.) can be called directly
    from a text-command invocation without being rewritten.

    This is intentionally minimal — it covers the interaction surface
    actually used across these cogs (response/followup/user/guild/channel/
    client/command/original_response/edit_original_response), not the full
    discord.py Interaction API. If a cog's callback uses something else
    (interaction.data, interaction.message, etc.), extend this class
    rather than special casing it in the cog.
    """
    def __init__(self, ctx, app_command=None):
        self._ctx = ctx
        self._original_message = None
        self.response = _FakeResponse(ctx, self)
        self.followup = _FakeFollowup(ctx, self)
        self.user = ctx.author
        self.guild = ctx.guild
        self.channel = ctx.channel
        self.client = ctx.bot
        self.command = app_command  # so `interaction.command.qualified_name` still works
        # Backs app_commands.checks.has_permissions(...) — real Discord
        # interactions get this precomputed by the API as channel-specific
        # effective permissions for the invoking user; the closest local
        # equivalent is asking the channel directly.
        self.permissions = ctx.channel.permissions_for(ctx.author) if ctx.guild else discord.Permissions.all()

    @property
    def guild_id(self):
        return self.guild.id if self.guild else None

    async def original_response(self):
        if self._original_message is None:
            raise RuntimeError("No response has been sent yet for this text-bridged command.")
        return self._original_message

    async def edit_original_response(self, content=None, *, embed=None, embeds=None, view=None, **kwargs):
        if self._original_message is None:
            self._original_message = await self._ctx.send(content=content, embed=embed, embeds=embeds or [], view=view)
        else:
            edit_kwargs = {}
            if content is not None:
                edit_kwargs["content"] = content
            if embed is not None:
                edit_kwargs["embed"] = embed
            if embeds is not None:
                edit_kwargs["embeds"] = embeds
            if view is not None:
                edit_kwargs["view"] = view
            await self._original_message.edit(**edit_kwargs)
        return self._original_message


async def run_app_command_checks(interaction, app_command) -> bool:
    """Runs an app_commands.Command's registered checks (admin_only(),
    mod_only(), etc. from cogs/config.py, plus any others) against a
    TextBridgeInteraction. Returns True if all checks pass. If a check
    raises app_commands.CheckFailure, sends its message the same way
    on_tree_error does for slash commands, then returns False."""
    import discord
    for check in getattr(app_command, "checks", []):
        try:
            result = check(interaction)
            if hasattr(result, "__await__"):
                result = await result
            if not result:
                return False
        except discord.app_commands.CheckFailure as e:
            msg = str(e) or "❌ You can't use this command."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            return False
    return True


class CommandDisabledError(Exception):
    """Raise/catch this in cogs to give a clean 'this command is disabled'
    message instead of a generic error."""
    def __init__(self, command_name: str):
        self.command_name = command_name
        super().__init__(f"Command '{command_name}' is disabled in this server.")


async def enforce_command_enabled(db, guild_id: int, command_name: str):
    """Convenience for slash commands: call at the top of a command
    callback (or in an app_commands.check) and it'll raise
    CommandDisabledError if the command has been turned off for the guild.

        @app_commands.check
        async def _enabled_check(interaction):
            await enforce_command_enabled(db, interaction.guild_id, "mute")
            return True
    """
    if not is_command_enabled(db, guild_id, command_name):
        raise CommandDisabledError(command_name)
