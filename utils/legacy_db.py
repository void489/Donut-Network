"""Synchronous pymongo handle for cogs that keep simple per-guild documents
(afk, invites, automod). Uses the same database as the rest of the bot."""
import threading

from pymongo import MongoClient

from config import settings

_client = None
_db = None
_lock = threading.Lock()


def get_db():
    global _client, _db
    with _lock:
        if _db is None:
            _client = MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=5000)
            _db = _client[settings.MONGO_DB_NAME]
        return _db
