"""
Helpers for the two collections the bot still uses: per-guild settings (AI chat
on/off etc.) and the command-permission framework (utils/permissions.py).
"""
from database.mongo import guild_settings_col, command_permissions_col

# ---------------------------------------------------------------------------
# GUILD SETTINGS
# ---------------------------------------------------------------------------

DEFAULT_GUILD_SETTINGS = {
    "prefix": "/",
}


async def get_guild_settings(guild_id: int) -> dict:
    doc = await guild_settings_col().find_one({"_id": str(guild_id)})
    if not doc:
        doc = {"_id": str(guild_id), **DEFAULT_GUILD_SETTINGS}
        await guild_settings_col().insert_one(doc)
    return doc


async def update_guild_settings(guild_id: int, update: dict) -> None:
    await guild_settings_col().update_one(
        {"_id": str(guild_id)}, {"$set": update}, upsert=True
    )


async def get_prefix(guild_id: int) -> str:
    settings = await get_guild_settings(guild_id)
    return settings.get("prefix", "/")


# ---------------------------------------------------------------------------
# COMMAND PERMISSIONS
# ---------------------------------------------------------------------------

DEFAULT_PUBLIC_COMMANDS = {"stats", "pay"}


def default_command_category(command_name: str) -> str:
    return "public" if command_name in DEFAULT_PUBLIC_COMMANDS else "staff"


async def register_command(guild_id: int, command_name: str, cog_name: str,
                           description: str = "", usage: str = "") -> None:
    key = f"{guild_id}:{command_name}"
    existing = await command_permissions_col().find_one({"_id": key})
    if existing:
        # Back-fill fields added in later versions without touching saved settings.
        patch = {}
        if description and existing.get("description") != description:
            patch["description"] = description
        if usage != existing.get("usage", ""):
            patch["usage"] = usage
        if "category" not in existing:
            patch["category"] = default_command_category(command_name)
        if "everyone" not in existing:
            patch["everyone"] = False
        if patch:
            await command_permissions_col().update_one({"_id": key}, {"$set": patch})
        return
    await command_permissions_col().insert_one({
        "_id": key,
        "guild_id": str(guild_id),
        "command": command_name,
        "cog": cog_name,
        "description": description,
        "usage": usage,
        "enabled": False,
        "allowed_roles": [],
        "everyone": False,
        "category": default_command_category(command_name),
        "admin_only": True,
    })


async def get_command_permission(guild_id: int, command_name: str) -> dict:
    key = f"{guild_id}:{command_name}"
    doc = await command_permissions_col().find_one({"_id": key})
    if not doc:
        return {"enabled": False, "allowed_roles": [], "admin_only": True, "everyone": False,
                "category": default_command_category(command_name)}
    doc.setdefault("everyone", False)
    doc.setdefault("category", default_command_category(command_name))
    return doc
