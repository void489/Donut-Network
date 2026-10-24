"""Motor (async) MongoDB client connection, shared across the bot."""
import motor.motor_asyncio
from config import settings

_client = None
_db = None


def get_client():
    global _client
    if _client is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGO_URI)
    return _client


def get_db():
    global _db
    if _db is None:
        _db = get_client()[settings.MONGO_DB_NAME]
    return _db


# Collection shortcuts
def guild_settings_col():
    return get_db()["guild_settings"]


def command_permissions_col():
    return get_db()["command_permissions"]
