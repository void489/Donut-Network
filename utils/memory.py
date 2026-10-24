"""
Long-term memory for the AI (chat + dashboard agent).

What it remembers
  * a rolling LOG of every exchange: who said what, in which channel, who the bot
    replied to, and which message each reply belongs to (ai_messages, kept 60 days)
  * a PROFILE per person (ai_memory): a running summary, the TOPICS discussed with
    them (with when), and durable facts - rewritten by the LLM every few messages
  * for the dashboard agent, each staff member's saved conversation, so it picks up
    where you left off after a refresh

Everything is scoped per server. Synchronous (pymongo): call from executors in async code.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from utils import llm
from utils.legacy_db import get_db

logger = logging.getLogger(__name__)

MSG = "ai_messages"
PROFILE = "ai_memory"
SESSION = "ai_agent_sessions"

SUMMARIZE_EVERY = 6        # user messages between profile refreshes
LOG_TTL_DAYS = 60
MAX_TOPICS = 12
MAX_FACTS = 20
_indexed = False


def _ensure_indexes():
    global _indexed
    if _indexed:
        return
    try:
        db = get_db()
        db[MSG].create_index("ts", expireAfterSeconds=LOG_TTL_DAYS * 86400)
        db[MSG].create_index([("guild_id", 1), ("channel_id", 1), ("ts", -1)])
        db[MSG].create_index([("guild_id", 1), ("user_id", 1), ("ts", -1)])
        db[MSG].create_index([("guild_id", 1), ("message_id", 1)])
        db[PROFILE].create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        db[SESSION].create_index([("guild_id", 1), ("actor_id", 1)], unique=True)
        _indexed = True
    except Exception as e:  # noqa: BLE001
        logger.warning("memory: index creation failed: %s", e)


def _now():
    return datetime.now(timezone.utc)


def _ago(ts) -> str:
    if not ts:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = int((_now() - ts).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 129600:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# --------------------------------------------------------------------------
# Message log
# --------------------------------------------------------------------------
def log_message(guild_id, channel_id, user_id, user_name, role, content, *,
                reply_to_user_id=None, reply_to_name=None, message_id=None,
                replying_to_message_id=None, scope="chat"):
    """role: 'user' (a person talking to the bot) or 'assistant' (the bot's reply)."""
    _ensure_indexes()
    get_db()[MSG].insert_one({
        "guild_id": str(guild_id), "channel_id": str(channel_id),
        "user_id": str(user_id), "user_name": user_name, "role": role,
        "content": (content or "")[:2000],
        "reply_to_user_id": str(reply_to_user_id) if reply_to_user_id else None,
        "reply_to_name": reply_to_name,
        "message_id": str(message_id) if message_id else None,
        "replying_to_message_id": str(replying_to_message_id) if replying_to_message_id else None,
        "scope": scope, "ts": _now(),
    })


def recent_channel(guild_id, channel_id, limit=14, scope="chat") -> list:
    rows = list(get_db()[MSG].find(
        {"guild_id": str(guild_id), "channel_id": str(channel_id), "scope": scope}
    ).sort("ts", -1).limit(limit))
    return list(reversed(rows))


def find_by_message_id(guild_id, message_id):
    return get_db()[MSG].find_one({"guild_id": str(guild_id), "message_id": str(message_id)})


def _speaker(row, bot_name="Bot") -> str:
    if row["role"] == "assistant":
        to = row.get("reply_to_name")
        return f"{bot_name} (replying to {to})" if to else bot_name
    return row.get("user_name") or row["user_id"]


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------
def get_profile(guild_id, user_id) -> dict:
    _ensure_indexes()
    return get_db()[PROFILE].find_one({"guild_id": str(guild_id), "user_id": str(user_id)}) or {}


def format_profile(p: dict, name: str = None) -> str:
    if not p:
        return ""
    lines = [f"What you remember about {name or p.get('user_name') or p.get('user_id')}:"]
    if p.get("summary"):
        lines.append(f"- Summary: {p['summary']}")
    if p.get("current_topic"):
        lines.append(f"- Most recent topic: {p['current_topic']} ({_ago(p.get('last_seen'))})")
    topics = sorted(p.get("topics") or [], key=lambda t: t.get("last_ts") or datetime.min, reverse=True)[:6]
    if topics:
        lines.append("- Topics discussed: " + "; ".join(
            f"{t['topic']} ({_ago(t.get('last_ts'))}){': ' + t['note'] if t.get('note') else ''}" for t in topics))
    if p.get("facts"):
        lines.append("- Facts: " + "; ".join(p["facts"][-8:]))
    if p.get("message_count"):
        lines.append(f"- Messages exchanged: {p['message_count']}")
    return "\n".join(lines)


def build_context(guild_id, channel_id, user_id, user_name, bot_name="Bot",
                  replying_to_message_id=None, scope="chat") -> str:
    """Everything the model should know before answering this person."""
    parts = []
    prof = format_profile(get_profile(guild_id, user_id), user_name)
    if prof:
        parts.append(prof)

    if replying_to_message_id:
        ref = find_by_message_id(guild_id, replying_to_message_id)
        if ref:
            who = ("you (to " + (ref.get("reply_to_name") or "someone") + ")") if ref["role"] == "assistant" else ref.get("user_name")
            parts.append(f'This message is a reply to a message from {who}: "{ref["content"][:300]}"')

    rows = recent_channel(guild_id, channel_id, 8, scope)
    if rows:
        parts.append("Recent conversation in this channel (oldest first):\n" + "\n".join(
            f"[{_ago(r['ts'])}] {_speaker(r, bot_name)}: {r['content'][:220]}" for r in rows))

    other = [r for r in get_db()[MSG].find(
        {"guild_id": str(guild_id), "user_id": str(user_id), "scope": scope, "channel_id": {"$ne": str(channel_id)}}
    ).sort("ts", -1).limit(3)]
    if other:
        parts.append("Earlier with this person in other channels:\n" + "\n".join(
            f"[{_ago(r['ts'])}] {r['content'][:160]}" for r in reversed(other)))
    return "\n\n".join(parts)


_JSON_RE = re.compile(r"\{.*\}", re.S)


def refresh_profile(guild_id, user_id, user_name, scope="chat", force=False):
    """Re-summarise what we know about this person. Safe to call often - it only
    does LLM work every SUMMARIZE_EVERY messages (or when forced)."""
    _ensure_indexes()
    db = get_db()
    key = {"guild_id": str(guild_id), "user_id": str(user_id)}
    prof = db[PROFILE].find_one_and_update(
        key, {"$inc": {"pending": 1, "message_count": 1},
              "$set": {"user_name": user_name, "last_seen": _now()}},
        upsert=True, return_document=True)
    if not force and (prof.get("pending", 0) < SUMMARIZE_EVERY) and (prof.get("summary") or prof.get("message_count", 0) < 3):
        return
    if not llm.is_configured():
        return
    rows = list(db[MSG].find({**key, "scope": scope}).sort("ts", -1).limit(14))
    # Include the bot's replies to this person for context.
    replies = list(db[MSG].find({"guild_id": str(guild_id), "reply_to_user_id": str(user_id),
                                 "scope": scope}).sort("ts", -1).limit(8))
    convo = sorted(rows + replies, key=lambda r: r["ts"])
    transcript = "\n".join(
        f"{'BOT' if r['role'] == 'assistant' else (r.get('user_name') or 'USER')}: {r['content'][:300]}" for r in convo)
    known = {"summary": prof.get("summary", ""), "topics": [
        {"topic": t["topic"], "note": t.get("note", "")} for t in prof.get("topics", [])],
        "facts": prof.get("facts", [])}
    prompt = (
        "You maintain long-term memory about one person the bot talks to. Update it from the new "
        "conversation. Reply with ONLY JSON: "
        '{"summary": "2-3 sentences on who they are / what they want", '
        '"current_topic": "what the latest exchange was about (short)", '
        '"topics": [{"topic": "short name", "note": "one line"}], '
        '"facts": ["durable facts like IGN, role, preferences - not chit-chat"]}. '
        "Keep every earlier topic/fact that's still true, add new ones, drop nothing important. "
        "No speculation; only what's in the text.\n\n"
        f"CURRENT MEMORY: {json.dumps(known)}\n\nNEW CONVERSATION:\n{transcript}")
    try:
        text = llm.simple_chat([{"role": "user", "content": prompt}], temperature=0.1, max_tokens=700)
        m = _JSON_RE.search(text)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("memory: profile refresh failed: %s", e)
        return
    now = _now()
    topics = {t["topic"].lower(): t for t in prof.get("topics", []) if t.get("topic")}
    current = (data.get("current_topic") or "").strip()
    for t in data.get("topics") or []:
        name = str(t.get("topic") or "").strip()
        if not name:
            continue
        old = topics.get(name.lower(), {})
        topics[name.lower()] = {"topic": name, "note": str(t.get("note") or old.get("note") or "")[:200],
                                "last_ts": now if name.lower() == current.lower() else old.get("last_ts", now)}
    if current and current.lower() not in topics:
        topics[current.lower()] = {"topic": current, "note": "", "last_ts": now}
    topic_list = sorted(topics.values(), key=lambda t: t.get("last_ts") or now, reverse=True)[:MAX_TOPICS]
    facts = []
    for f in (prof.get("facts") or []) + [str(x)[:160] for x in (data.get("facts") or [])]:
        if f and f not in facts:
            facts.append(f)
    db[PROFILE].update_one(key, {"$set": {
        "summary": str(data.get("summary") or prof.get("summary") or "")[:700],
        "current_topic": current or prof.get("current_topic", ""),
        "topics": topic_list, "facts": facts[-MAX_FACTS:], "pending": 0, "updated_at": now}})


def add_note(guild_id, user_id, note, topic=None, user_name=None):
    """An explicit note ('remember that ...')."""
    _ensure_indexes()
    now = _now()
    upd = {"$addToSet": {"facts": note[:200]}, "$set": {"last_seen": now}}
    if user_name:
        upd["$set"]["user_name"] = user_name
    get_db()[PROFILE].update_one({"guild_id": str(guild_id), "user_id": str(user_id)}, upd, upsert=True)
    if topic:
        db = get_db()[PROFILE]
        key = {"guild_id": str(guild_id), "user_id": str(user_id)}
        db.update_one({**key, "topics.topic": {"$ne": topic}},
                      {"$push": {"topics": {"topic": topic[:80], "note": note[:200], "last_ts": now}}})


def search(guild_id, query, limit=10) -> list:
    _ensure_indexes()
    db = get_db()
    rx = {"$regex": re.escape(query), "$options": "i"}
    g = str(guild_id)
    out = [{"type": "message", "who": _speaker(r), "when": _ago(r["ts"]), "text": r["content"][:200]}
           for r in db[MSG].find({"guild_id": g, "content": rx}).sort("ts", -1).limit(limit)]
    for p in db[PROFILE].find({"guild_id": g, "$or": [{"summary": rx}, {"facts": rx}, {"topics.topic": rx},
                                                         {"topics.note": rx}]}).limit(limit):
        out.append({"type": "profile", "user": p.get("user_name") or p["user_id"], "user_id": p["user_id"],
                    "summary": p.get("summary", "")[:200]})
    return out[:limit * 2]


def list_profiles(guild_id, limit=30) -> list:
    return list(get_db()[PROFILE].find({"guild_id": str(guild_id)}).sort("last_seen", -1).limit(limit))


def forget_user(guild_id, user_id) -> None:
    db = get_db()
    db[PROFILE].delete_one({"guild_id": str(guild_id), "user_id": str(user_id)})
    db[MSG].delete_many({"guild_id": str(guild_id), "$or": [
        {"user_id": str(user_id)}, {"reply_to_user_id": str(user_id)}]})


# --------------------------------------------------------------------------
# Dashboard agent sessions (so a refresh doesn't lose the conversation)
# --------------------------------------------------------------------------
def load_session(guild_id, actor_id) -> list:
    _ensure_indexes()
    doc = get_db()[SESSION].find_one({"guild_id": str(guild_id), "actor_id": str(actor_id)})
    return (doc or {}).get("messages", [])


def save_session(guild_id, actor_id, messages: list) -> None:
    _ensure_indexes()
    get_db()[SESSION].update_one(
        {"guild_id": str(guild_id), "actor_id": str(actor_id)},
        {"$set": {"messages": messages[-40:], "updated_at": _now()}}, upsert=True)


def clear_session(guild_id, actor_id) -> None:
    get_db()[SESSION].delete_one({"guild_id": str(guild_id), "actor_id": str(actor_id)})
