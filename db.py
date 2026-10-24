"""Shared MongoDB connection used by the bot and Flask dashboard."""

import os
import logging
import threading
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_client: MongoClient | None = None
_db = None


_lock = threading.Lock()


def get_db():
    """Return the shared discord_bot database handle (lazy singleton, thread-safe)."""
    global _client, _db

    with _lock:
        if _db is not None:
            try:
                _client.admin.command('ping')
                return _db
            except Exception as e:
                logger.warning(f"Existing MongoDB connection lost: {e}")
                _client = None
                _db = None

    mongo_uri = os.getenv("MONGO_URI")
    if not mongo_uri:
        logger.error("❌ MONGO_URI environment variable not set!")
        return None

    try:
        logger.info(f"🔄 Connecting to MongoDB...")
        _client = MongoClient(
            mongo_uri,
            serverSelectionTimeoutMS=5000,  # 5 second timeout
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
        
        # Test the connection
        _client.admin.command('ping')
        logger.info("✅ MongoDB connection successful!")
        
        _db = _client["discord_bot"]
        
        # Verify we can access the database
        collections = _db.list_collection_names()
        logger.info(f"📚 Available collections: {collections}")
        
        # Create collections if they don't exist
        if "command_perms" not in collections:
            logger.info("📝 Creating 'command_perms' collection")
            _db.create_collection("command_perms")

        if "mc_sessions" not in collections:
            logger.info("📝 Creating 'mc_sessions' collection")
            _db.create_collection("mc_sessions")

        if "mc_auth_gate_servers" not in collections:
            logger.info("📝 Creating 'mc_auth_gate_servers' collection")
            _db.create_collection("mc_auth_gate_servers")
            # Seed with the server this flow was originally built for, so
            # behavior doesn't change on first deploy of this collection.
            _db["mc_auth_gate_servers"].insert_one({"domain": "donutsmp.net"})

        # Note: "mc_schedules" (persisted Auto Commands) is created and
        # indexed by the Node MC-bot process itself (mc-bot/index.js), not
        # here, since only that process reads/writes it.

        # Create index for faster lookups
        _db["command_perms"].create_index([("guild_id", 1), ("command_name", 1)], unique=True)
        _db["mc_sessions"].create_index([("discord_id", 1)])
        _db["mc_sessions"].create_index([("session_id", 1)], unique=True)
        _db["mc_auth_gate_servers"].create_index([("domain", 1)], unique=True)
        
        return _db
        
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error(f"❌ Failed to connect to MongoDB: {e}")
        _client = None
        _db = None
        return None
    except Exception as e:
        logger.error(f"❌ Unexpected error connecting to MongoDB: {e}")
        _client = None
        _db = None
        return None


def get_bot_token() -> str | None:
    """Accept either env var name so bot and dashboard stay in sync."""
    token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
    if not token:
        logger.error("❌ No Discord bot token found in environment variables!")
    else:
        logger.info("✅ Discord bot token found")
    return token


def test_mongodb():
    """Test MongoDB connection and basic operations."""
    db = get_db()
    if db is None:
        logger.error("❌ Could not get database connection")
        return False
    
    try:
        # Test insert
        test_result = db["command_perms"].update_one(
            {"guild_id": 999999, "command_name": "_test_"},
            {"$set": {"roles": ["test_role"]}},
            upsert=True
        )
        logger.info(f"✅ Test upsert successful: {test_result.raw_result}")
        
        # Test find
        test_doc = db["command_perms"].find_one({"guild_id": 999999, "command_name": "_test_"})
        logger.info(f"✅ Test find successful: {test_doc}")
        
        # Test delete
        delete_result = db["command_perms"].delete_one({"guild_id": 999999, "command_name": "_test_"})
        logger.info(f"✅ Test delete successful: {delete_result.raw_result}")
        
        return True
    except Exception as e:
        logger.error(f"❌ MongoDB test failed: {e}")
        return False


