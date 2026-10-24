# app.py - admin panel only (servers, your MC sessions, bot servers, stats channels)

import base64
import io
import os
import re
import time
import uuid
from functools import wraps
from flask import Flask, render_template, redirect, request, session, jsonify, send_file
import requests
from werkzeug.security import check_password_hash
from db import get_bot_token, get_db, test_mongodb
from bson.objectid import ObjectId
from datetime import datetime, timedelta
import logging


logger = logging.getLogger(__name__)

app = Flask(__name__)
flask_secret = os.getenv("FLASK_SECRET")
if not flask_secret:
    raise RuntimeError("FLASK_SECRET environment variable must be set!")
app.secret_key = flask_secret
# .litematic uploads for the per-server build service (Manage page)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024

# Use secure cookies only in production (HTTPS). On localhost (HTTP) this would
# silently destroy sessions.
_is_production = (
    os.getenv("RAILWAY_ENVIRONMENT") is not None
    or os.getenv("FLASK_ENV") == "production"
    or os.getenv("DYNO") is not None  # Heroku
)
app.config['SESSION_COOKIE_SECURE'] = _is_production
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_REFRESH_EACH_REQUEST'] = True

# Discord OAuth2 Config
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
BOT_TOKEN = get_bot_token()

# ── Admin panel auth (separate from Discord OAuth) ───────────────────────────
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")


def require_admin(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect("/admin/login")
        return view_func(*args, **kwargs)
    return wrapped

_DISCORD_BOT = None


def set_discord_bot(bot_instance):
    global _DISCORD_BOT
    _DISCORD_BOT = bot_instance


# Connect to MongoDB (shared singleton with the bot)
db = get_db()

if db is not None:
    logger.info("🔍 Testing MongoDB connection...")
    if test_mongodb():
        logger.info("✅ MongoDB test passed!")
    else:
        logger.error("❌ MongoDB test failed!")
else:
    logger.error("❌ Could not connect to MongoDB at startup!")


# ── Bot guild cache ──────────────────────────────────────────────────────────
_bot_guild_ids_cache = {"ids": set(), "ts": 0}
_BOT_GUILDS_CACHE_TTL = 60  # seconds


def get_bot_guild_ids():
    """Return the set of guild IDs the bot is currently a member of."""
    now = time.time()
    if _bot_guild_ids_cache["ids"] and (now - _bot_guild_ids_cache["ts"] < _BOT_GUILDS_CACHE_TTL):
        return _bot_guild_ids_cache["ids"]

    if not BOT_TOKEN:
        return _bot_guild_ids_cache["ids"]

    headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    ids = set()
    after = None
    try:
        while True:
            params = {"limit": 200}
            if after:
                params["after"] = after
            resp = requests.get(
                "https://discord.com/api/v10/users/@me/guilds",
                headers=headers, params=params, timeout=10,
            )
            if not resp.ok:
                break
            page = resp.json()
            if not page:
                break
            ids.update(int(g["id"]) for g in page)
            if len(page) < 200:
                break
            after = page[-1]["id"]
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch bot guild list: {e}")
        if _bot_guild_ids_cache["ids"]:
            return _bot_guild_ids_cache["ids"]

    _bot_guild_ids_cache["ids"] = ids
    _bot_guild_ids_cache["ts"] = now
    return ids


def bot_in_guild(guild_id) -> bool:
    return int(guild_id) in get_bot_guild_ids()


def get_bot_guilds_detailed():
    """Full guild objects (id, name, icon, approximate_member_count, owner)
    for every server the bot is in — used by /admin/bot-servers. Separate
    from get_bot_guild_ids() (which just caches bare IDs for the
    lightweight bot_in_guild() checks used all over the dashboard) since
    this pulls more data per page and isn't needed nearly as often."""
    if not BOT_TOKEN:
        return []
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    guilds = []
    after = None
    try:
        while True:
            params = {"limit": 200, "with_counts": "true"}
            if after:
                params["after"] = after
            resp = requests.get(
                "https://discord.com/api/v10/users/@me/guilds",
                headers=headers, params=params, timeout=10,
            )
            if not resp.ok:
                logger.warning(f"⚠️ get_bot_guilds_detailed: {resp.status_code} {resp.text[:200]}")
                break
            page = resp.json()
            if not page:
                break
            guilds.extend(page)
            if len(page) < 200:
                break
            after = page[-1]["id"]
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch detailed bot guild list: {e}")
    return sorted(guilds, key=lambda g: (g.get("name") or "").lower())


# ── Owner-only: the only account that exists here ───────────────────────────
# No plans, no limits. Only the bot owner (and the stats worker account, which
# /stats and /pay need) are allowed in the admin panel; everyone else's data is
# removed with the "Remove all other users" button on the Overview page.
try:
    from personality import OWNER_USER_ID
except Exception:  # pragma: no cover
    OWNER_USER_ID = os.getenv("BOT_OWNER_ID", "1113217456803172462")
OWNER_ID = str(OWNER_USER_ID)
WORKER_ID = str(os.getenv("STATS_WORKER_DISCORD_ID") or "")
ALLOWED_USER_IDS = {i for i in (OWNER_ID, WORKER_ID) if i}


@app.before_request
def _only_owner_user_pages():
    """/admin/users/<discord_id>/... only ever works for the owner (or worker) ID."""
    parts = request.path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "admin" and parts[1] == "users":
        if parts[2] not in ALLOWED_USER_IDS:
            return ("Not found", 404)


def _other_user_counts() -> dict:
    """How much data belongs to anyone other than the owner/worker."""
    out = {"users": 0, "sessions": 0}
    if db is None:
        return out
    ids = set()
    try:
        for coll in ("mc_sessions", "user_plan", "usage_daily", "user_settings"):
            for doc in db[coll].find({"discord_id": {"$nin": list(ALLOWED_USER_IDS)}}, {"discord_id": 1}):
                if doc.get("discord_id"):
                    ids.add(str(doc["discord_id"]))
        out["sessions"] = db["mc_sessions"].count_documents({"discord_id": {"$nin": list(ALLOWED_USER_IDS)}})
    except Exception as e:
        logger.warning(f"other-user count failed: {e}")
    out["users"] = len(ids)
    return out


# ── Admin: Discord identity lookup (username/avatar), short-lived cache ─────
_discord_user_cache = {}  # discord_id -> (data, expires_at)
_DISCORD_USER_CACHE_TTL = 300  # seconds


def _admin_discord_user_lookup(discord_id: str) -> dict:
    """Best-effort {username, avatar_url} for any Discord user ID via the
    bot token — used purely for display in the admin panel, so failures
    just fall back to showing the bare ID. Cached briefly since the same
    handful of admin pages can request the same IDs repeatedly."""
    now = time.time()
    cached = _discord_user_cache.get(discord_id)
    if cached and cached[1] > now:
        return cached[0]

    data = {"username": discord_id, "avatar_url": None}
    if BOT_TOKEN:
        try:
            r = requests.get(
                f"https://discord.com/api/v10/users/{discord_id}",
                headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"},
                timeout=5,
            )
            if r.ok:
                u = r.json()
                username = u.get("global_name") or u.get("username") or discord_id
                avatar_hash = u.get("avatar")
                avatar_url = (
                    f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png?size=64"
                    if avatar_hash else
                    f"https://cdn.discordapp.com/embed/avatars/{int(discord_id) % 5 if discord_id.isdigit() else 0}.png"
                )
                data = {"username": username, "avatar_url": avatar_url}
        except Exception:
            pass

    _discord_user_cache[discord_id] = (data, now + _DISCORD_USER_CACHE_TTL)
    return data


# ── Admin: MC session overview helpers ───────────────────────────────────────

def _mc_linked_session_ids() -> set:
    """Session IDs with a saved Microsoft auth-token cache. This is read
    straight out of Mongo's mc_auth_cache collection (shared with the Node
    MC bot service — see index.js's backupAuthCache/restoreAuthCache), so
    it reflects "has a linked account" even if the Node process is
    currently offline and can't answer a live status check."""
    if db is None:
        return set()
    try:
        return set(db["mc_auth_cache"].distinct("session_id"))
    except Exception:
        return set()


def get_admin_mc_overview() -> dict:
    """Cheap, DB-only per-discord_id summary: {discord_id: {sessions, linked}}.
    Deliberately does NOT hit the Node MC bot service (no live status) — this
    is used on pages listing many users at once, where N live-status calls
    would be slow. See admin_mc_live_status() for the on-demand live version."""
    if db is None:
        return {}
    linked_ids = _mc_linked_session_ids()
    by_user = {}
    try:
        for doc in db["mc_sessions"].find({}, {"discord_id": 1, "session_id": 1}):
            did = str(doc.get("discord_id"))
            entry = by_user.setdefault(did, {"sessions": 0, "linked": 0})
            entry["sessions"] += 1
            if doc.get("session_id") in linked_ids:
                entry["linked"] += 1
    except Exception:
        pass
    return by_user


def _mc_live_status(session_id: str, timeout: int = 3):
    """Live status for one session from the Node MC bot service, or None if
    unreachable. Best-effort — callers should treat None as 'unknown'."""
    try:
        r = requests.get(f"{MC_BOT_URL}/status/{session_id}", timeout=timeout)
        return _safe_json(r)
    except Exception:
        return None

@app.route("/")
def index():
    return redirect("/admin")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect("/admin")

    if request.method == "POST":
        if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
            logger.error("❌ ADMIN_USERNAME / ADMIN_PASSWORD_HASH not set in environment.")
            return render_template("admin_login.html", error=True)

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.clear()
            session["is_admin"] = True
            session.permanent = True
            return redirect("/admin")

        return render_template("admin_login.html", error=True)

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect("/admin/login")


@app.route("/admin")
@require_admin
def admin_panel():
    return render_template(
        "admin_panel.html",
        bot_online=(_DISCORD_BOT is not None and _DISCORD_BOT.is_ready() if _DISCORD_BOT else False),
        guild_count=len(get_bot_guild_ids()),
        owner_treatment=get_owner_treatment(),
        owner_saved=request.args.get("owner_saved") == "1",
        owner_err=request.args.get("owner_err") == "1",
        others=_other_user_counts(),
        owner_id=OWNER_ID,
        purged=request.args.get("purged"),
    )


def get_owner_treatment() -> bool:
    """Global tick: does the bot owner get special treatment from the AI? Default ON."""
    if db is None:
        return True
    try:
        doc = db["bot_global_settings"].find_one({"_id": "owner_treatment"})
        return bool(doc.get("enabled", True)) if doc else True
    except Exception as e:
        logger.warning(f"owner_treatment read failed: {e}")
        return True


@app.route("/admin/owner-treatment", methods=["POST"])
@require_admin
def admin_owner_treatment():
    if db is None:
        return redirect("/admin?owner_err=1")
    enabled = request.form.get("owner_treatment") == "on"
    db["bot_global_settings"].update_one(
        {"_id": "owner_treatment"},
        {"$set": {"enabled": enabled, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return redirect("/admin?owner_saved=1")


@app.route("/admin/servers", methods=["GET", "POST"])
@require_admin
def admin_servers():
    if db is None:
        return render_template("admin_servers.html", servers=[], error="Database unavailable.")

    if request.method == "POST":
        domain = (request.form.get("domain") or "").strip().lower()
        # Strip protocol/path/port if someone pastes a full address.
        domain = domain.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
        if not domain:
            return redirect("/admin/servers?error=empty")
        try:
            db["mc_auth_gate_servers"].update_one(
                {"domain": domain},
                {"$set": {"domain": domain, "added_at": datetime.utcnow()}},
                upsert=True,
            )
        except Exception as e:
            logger.error(f"Failed to add gated server {domain}: {e}")
            return redirect("/admin/servers?error=save")
        return redirect("/admin/servers?saved=1")

    servers = list(db["mc_auth_gate_servers"].find().sort("domain", 1))
    return render_template(
        "admin_servers.html",
        servers=servers,
        saved=request.args.get("saved") == "1",
        error=request.args.get("error"),
    )


@app.route("/admin/servers/<server_id>/delete", methods=["POST"])
@require_admin
def admin_servers_delete(server_id):
    if db is not None:
        try:
            db["mc_auth_gate_servers"].delete_one({"_id": ObjectId(server_id)})
        except Exception as e:
            logger.error(f"Failed to delete gated server {server_id}: {e}")
    return redirect("/admin/servers")


@app.route("/internal/mc-auth-servers")
def internal_mc_auth_servers():
    """Read-only list of domains that trigger the 'awaiting Discord
    authorization' kick flow (see index.js). Polled periodically by the Node
    mc-bot process running alongside this app — not meant for browser use,
    but carries no sensitive data (just a list of server hostnames) so it's
    left unauthenticated for simplicity."""
    if db is None:
        return jsonify({"domains": []})
    domains = [d["domain"] for d in db["mc_auth_gate_servers"].find({}, {"domain": 1})]
    return jsonify({"domains": domains})


@app.route("/admin/users")
@require_admin
def admin_users():
    # Only one user exists: go straight to the owner's sessions.
    return redirect(f"/admin/users/{OWNER_ID}")


@app.route("/admin/mc/live-status")
@require_admin
def admin_mc_live_status():
    """On-demand (lazy AJAX) live status for every MC session in the DB,
    grouped by discord_id — separate from /admin/users itself since this
    hits the Node bot service once per session and can take a moment with
    many sessions. The users page calls this after its own render to fill
    in Online badges without blocking the initial page load."""
    if db is None:
        return jsonify({})
    out = {}
    try:
        docs = list(db["mc_sessions"].find({"discord_id": {"$in": list(ALLOWED_USER_IDS)}}, {"discord_id": 1, "session_id": 1}))
    except Exception:
        docs = []
    for doc in docs:
        did = str(doc.get("discord_id"))
        entry = out.setdefault(did, {"online": 0, "servers": []})
        live = _mc_live_status(doc["session_id"], timeout=2)
        if live and live.get("status") not in (None, "disconnected", "error"):
            entry["online"] += 1
            if live.get("server"):
                entry["servers"].append(live["server"])
    return jsonify(out)


# ─────────────────────────────────────────────────────────────────────────
# Admin: per-user MC session management
#
# Lets an admin see exactly what a user's dashboard support ticket usually
# can't convey over chat — which sessions exist, whether each has a Microsoft
# account linked, whether it's online right now and on what server — and act
# on their behalf (start the login device-code flow, reconnect, manage Auto
# Commands) when the user themselves is stuck. Every route here mirrors a
# user-facing /mc-* route one-for-one, just re-scoped to @require_admin +
# an explicit discord_id path segment instead of _require_owned_session().
# ─────────────────────────────────────────────────────────────────────────

def _require_admin_mc_session(discord_id: str, session_id: str):
    """Admin equivalent of _require_owned_session(): confirms the session
    exists and actually belongs to this discord_id (catches copy/paste
    mistakes in the admin UI), without requiring the admin to *be* that
    Discord user. Returns (doc, None) or (None, (json_response, status))."""
    doc = _get_session_doc(session_id)
    if not doc:
        return None, (jsonify({"ok": False, "error": "Session not found"}), 404)
    if str(doc.get("discord_id")) != str(discord_id):
        return None, (jsonify({"ok": False, "error": "Session does not belong to this user"}), 403)
    return doc, None


@app.route("/admin/users/<discord_id>")
@require_admin
def admin_user_detail(discord_id):
    """Management view for the owner: every MC
    session with live status, and controls to log them in, reconnect,
    connect to a server, or manage Auto Commands on their behalf."""
    if db is None:
        return "<h1>Error: Database connection not available!</h1>", 500

    identity = _admin_discord_user_lookup(discord_id)
    docs = _list_user_sessions(discord_id)
    linked_ids = _mc_linked_session_ids()
    sessions_out = []
    for doc in docs:
        live = _mc_live_status(doc["session_id"], timeout=4)
        entry = _serialize_session(doc, live)
        entry["linked"] = doc["session_id"] in linked_ids
        entry["ping"] = (live or {}).get("ping", 0)
        entry["requires_auth"] = (live or {}).get("requires_auth", False)
        entry["code"] = (live or {}).get("code")
        entry["url"] = (live or {}).get("url")
        entry["health"] = (live or {}).get("health")
        entry["hunger"] = (live or {}).get("hunger")
        entry["live_reachable"] = live is not None
        sessions_out.append(entry)

    # Newest first so a freshly created session for a stuck user is visible
    # without scrolling.
    sessions_out.sort(key=lambda s: s.get("created_at") or "", reverse=True)

    return render_template(
        "admin_user_detail.html",
        discord_id=discord_id,
        identity=identity,
        sessions=sessions_out,
        session_count=len(sessions_out),
    )


@app.route("/admin/users/<discord_id>/mc-sessions/create", methods=["POST"])
@require_admin
def admin_mc_sessions_create(discord_id):
    """Create a new MC session (no limits)."""
    if db is None:
        return jsonify({"ok": False, "error": "Database unavailable"}), 503

    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()[:64] or None

    doc = {
        "session_id": str(uuid.uuid4()),
        "discord_id": str(discord_id),
        "mc_username": None,
        "created_at": datetime.utcnow(),
        "last_server": None,
        "last_version": "latest",
        "auto_reconnect": True,
        "label": label,
        "created_by_admin": True,
    }
    db["mc_sessions"].insert_one(doc)
    return jsonify({"ok": True, "session": _serialize_session(doc)})


@app.route("/admin/users/<discord_id>/mc-sessions/<session_id>/delete", methods=["POST"])
@require_admin
def admin_mc_sessions_delete(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        requests.post(f"{MC_BOT_URL}/full-logout/{session_id}", timeout=10)
    except Exception:
        pass
    db["mc_sessions"].delete_one({"session_id": session_id})
    return jsonify({"ok": True})


@app.route("/admin/users/<discord_id>/mc-status/<session_id>")
@require_admin
def admin_mc_status(discord_id, session_id):
    doc, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.get(f"{MC_BOT_URL}/status/{session_id}", timeout=5)
        data = _safe_json(r)
    except Exception as e:
        return jsonify({"status": "error", "error": f"MC bot unreachable: {e}"}), 503
    if isinstance(data, dict):
        data["last_server"] = doc.get("last_server")
        data["last_version"] = doc.get("last_version", "latest")
        data["linked"] = session_id in _mc_linked_session_ids()
    return jsonify(data)


@app.route("/admin/users/<discord_id>/mc-start-login/<session_id>", methods=["POST"])
@require_admin
def admin_mc_start_login(discord_id, session_id):
    """Kicks off the Microsoft device-code flow for this session. The admin
    relays the returned code + URL to the user (or completes it themselves
    if they're doing the login as a hands-on service) — poll
    /admin/users/<discord_id>/mc-status/<session_id> for the code/url to
    show, exactly like the user-facing login screen does."""
    doc, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.post(
            f"{MC_BOT_URL}/start-login/{session_id}",
            json={"discordId": doc["discord_id"]},
            timeout=10,
        )
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-reconnect/<session_id>", methods=["POST"])
@require_admin
def admin_mc_reconnect(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.post(f"{MC_BOT_URL}/reconnect/{session_id}", timeout=10)
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-logout/<session_id>", methods=["POST"])
@require_admin
def admin_mc_logout(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.post(f"{MC_BOT_URL}/logout/{session_id}", timeout=10)
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-full-logout/<session_id>", methods=["POST"])
@require_admin
def admin_mc_full_logout(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.post(f"{MC_BOT_URL}/full-logout/{session_id}", timeout=15)
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-connect/<session_id>", methods=["POST"])
@require_admin
def admin_mc_connect(discord_id, session_id):
    doc, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    server = data.get("server")
    version = data.get("version", "latest")
    if not server:
        return jsonify({"ok": False, "error": "Missing server"}), 400

    try:
        r = requests.post(
            f"{MC_BOT_URL}/connect/{session_id}",
            json={"server": server, "version": version, "discordId": doc["discord_id"]},
            timeout=15,
        )
        resp = _safe_json(r)
        if not resp.get("requires_auth") and (resp.get("ok") or resp.get("status") == "ready"):
            db["mc_sessions"].update_one(
                {"session_id": session_id},
                {"$set": {"last_server": server, "last_version": version, "auto_reconnect": True}},
            )
        return jsonify(resp)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-schedule/<session_id>", methods=["GET"])
@require_admin
def admin_mc_get_schedule(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    try:
        r = requests.get(f"{MC_BOT_URL}/schedule/{session_id}", timeout=5)
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-schedule/<session_id>", methods=["POST"])
@require_admin
def admin_mc_add_schedule(discord_id, session_id):
    """Add an auto command to a session (no limits)."""
    doc, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    command = data.get("command")
    interval = data.get("interval")
    if not command or not interval:
        return jsonify({"ok": False, "error": "Command and interval required"}), 400

    try:
        r = requests.post(
            f"{MC_BOT_URL}/schedule/{session_id}",
            json={"command": command, "interval": interval},
            timeout=10,
        )
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-schedule/disable/<session_id>", methods=["POST"])
@require_admin
def admin_mc_disable_schedule(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"ok": False, "error": "Command required"}), 400
    try:
        r = requests.post(
            f"{MC_BOT_URL}/schedule/disable/{session_id}",
            json={"command": command},
            timeout=10,
        )
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-schedule/delete/<session_id>", methods=["POST"])
@require_admin
def admin_mc_delete_schedule(discord_id, session_id):
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if not command:
        return jsonify({"ok": False, "error": "Command required"}), 400
    try:
        r = requests.post(
            f"{MC_BOT_URL}/schedule/delete/{session_id}",
            json={"command": command},
            timeout=10,
        )
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-chat/<session_id>")
@require_admin
def admin_mc_chat_get(discord_id, session_id):
    """Poll for new in-game chat/console output for this session — same
    feed the user sees on their own dashboard, just readable by an admin
    without needing to be logged in as that Discord user."""
    _, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err
    after = request.args.get("after", 0)
    try:
        r = requests.get(f"{MC_BOT_URL}/chat/{session_id}", params={"after": after}, timeout=5)
        return jsonify(_safe_json(r))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/users/<discord_id>/mc-chat/<session_id>", methods=["POST"])
@require_admin
def admin_mc_chat_post(discord_id, session_id):
    """Send a chat message or run a command in this session's console (no limits)."""
    doc, err = _require_admin_mc_session(discord_id, session_id)
    if err:
        return err

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Message required"}), 400
    is_command = message.startswith("/")

    try:
        r = requests.post(f"{MC_BOT_URL}/chat/{session_id}", json=data, timeout=10)
        result = _safe_json(r)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


@app.route("/admin/bot-servers")
@require_admin
def admin_bot_servers():
    guilds = get_bot_guilds_detailed()
    ai = {}
    if db is not None:
        try:
            ids = [str(g["id"]) for g in guilds]
            for d in db["guild_settings"].find({"_id": {"$in": ids}}, {"ai_chat": 1}):
                ai[d["_id"]] = d.get("ai_chat") or {}
        except Exception as e:
            logger.warning(f"ai_chat settings read failed: {e}")
    for g in guilds:
        cfg = ai.get(str(g["id"]), {})
        g["ai_enabled"] = bool(cfg.get("enabled", False))        # default OFF
        g["ai_search"] = bool(cfg.get("search_enabled", False))  # default OFF
    return render_template("admin_bot_servers.html", guilds=guilds)


@app.route("/admin/bot-servers/<guild_id>/ai", methods=["POST"])
@require_admin
def admin_bot_servers_ai(guild_id):
    """Per-server AI chat + web search switches (the only way to change them)."""
    if db is None:
        return redirect("/admin/bot-servers?error=db")
    db["guild_settings"].update_one(
        {"_id": str(guild_id)},
        {"$set": {
            "ai_chat.enabled": request.form.get("ai_enabled") == "on",
            "ai_chat.search_enabled": request.form.get("ai_search") == "on",
        }},
        upsert=True,
    )
    return redirect("/admin/bot-servers?ai_saved=1")


@app.route("/admin/bot-servers/<guild_id>/invite", methods=["POST"])
@require_admin
def admin_bot_servers_invite(guild_id):
    """Tries to mint a permanent, unlimited-use invite in the first channel
    the bot has Create Invite permission in. Returns JSON so the page can
    show the link inline without a full reload — mirrors the pending-action
    pattern already used on the AI Agent page."""
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "Bot token not configured."}), 500
    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}

    chans_res = requests.get(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels", headers=bot_headers, timeout=10
    )
    if not chans_res.ok:
        return jsonify({"ok": False, "error": f"Couldn't list channels ({chans_res.status_code})."}), 400

    text_channels = [c for c in chans_res.json() if c.get("type") == 0]
    last_error = "No text channels found."
    for channel in text_channels:
        try:
            inv_res = requests.post(
                f"https://discord.com/api/v10/channels/{channel['id']}/invites",
                headers={**bot_headers, "Content-Type": "application/json"},
                json={"max_age": 0, "max_uses": 0, "unique": False},
                timeout=10,
            )
        except Exception as e:
            last_error = str(e)
            continue
        if inv_res.ok:
            code = inv_res.json().get("code")
            return jsonify({"ok": True, "invite_url": f"https://discord.gg/{code}"})
        last_error = f"{inv_res.status_code}: {inv_res.text[:150]}"

    return jsonify({"ok": False, "error": f"Couldn't create an invite in any channel ({last_error}). "
                                            f"The bot likely lacks Create Invite permission here."}), 400


@app.route("/admin/bot-servers/<guild_id>/leave", methods=["POST"])
@require_admin
def admin_bot_servers_leave(guild_id):
    if not BOT_TOKEN:
        return redirect("/admin/bot-servers?error=notoken")
    bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}
    try:
        resp = requests.delete(
            f"https://discord.com/api/v10/users/@me/guilds/{guild_id}", headers=bot_headers, timeout=10
        )
        if not resp.ok and resp.status_code != 204:
            logger.warning(f"⚠️ Failed to leave guild {guild_id}: {resp.status_code} {resp.text[:200]}")
            return redirect("/admin/bot-servers?error=leave")
    except Exception as e:
        logger.warning(f"⚠️ Failed to leave guild {guild_id}: {e}")
        return redirect("/admin/bot-servers?error=leave")

    # Bust the lightweight ID cache too, so bot_in_guild() reflects the
    # departure immediately instead of waiting up to 60s.
    _bot_guild_ids_cache["ids"].discard(int(guild_id))
    return redirect("/admin/bot-servers?left=1")


# ── Per-server Manage page: build-service ticket roles + builds ──────────────
# Admin-only (require_admin). Reached from the "Manage" button on Bot Servers.
#   build_settings {_id: guild_id, ping_role_id, allow_role_ids: [..],
#                   manager_role_id, category_id ("<id>" or "__new__:<name>")}
#   builds {_id, guild_id, name, price (int), price_text, file_id (GridFS),
#           filename, size, created_at}
# The bot reads the same collections (cogs/builds.py) for /build panel.
DISCORD_API = "https://discord.com/api/v10"


def _discord_get(path):
    if not BOT_TOKEN:
        return None
    try:
        r = requests.get(f"{DISCORD_API}{path}",
                         headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"},
                         timeout=10)
        return r.json() if r.ok else None
    except Exception as e:
        logger.warning(f"Discord GET {path} failed: {e}")
        return None


def _discord_patch(path, payload):
    """PATCH to the Discord API as the bot. Returns (ok, data_or_error_text)."""
    if not BOT_TOKEN:
        return False, "No bot token configured."
    try:
        r = requests.patch(f"{DISCORD_API}{path}", json=payload, timeout=15,
                           headers={"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"})
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.ok:
            return True, data
        msg = data.get("message") or f"HTTP {r.status_code}"
        for k, v in (data.get("errors") or {}).items():       # e.g. avatar: {_errors: [{message}]}
            try:
                msg += f" ({k}: {v['_errors'][0]['message']})"
            except Exception:
                pass
        return False, msg
    except Exception as e:
        logger.warning(f"Discord PATCH {path} failed: {e}")
        return False, "Couldn't reach Discord."


BOT_PROFILE_LIMITS = {"avatar": 2 * 1024 * 1024, "banner": 5 * 1024 * 1024}


def _bot_profile_view(guild_id):
    """Current per-server profile of the bot (nick, bio, avatar, banner + CDN preview URLs)."""
    m = _discord_get(f"/guilds/{guild_id}/members/@me") or {}
    uid = (m.get("user") or {}).get("id") or ""
    def cdn(kind, h):
        if not (h and uid):
            return ""
        ext = "gif" if h.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/guilds/{guild_id}/users/{uid}/{kind}/{h}.{ext}?size=512"
    return {"nick": m.get("nick") or "", "bio": m.get("bio") or "",
            "avatar_url": cdn("avatars", m.get("avatar")), "banner_url": cdn("banners", m.get("banner"))}


def _parse_price(text):
    """'10k' -> 10000, '1.5m' -> 1500000, '2b', '1,000'. None if invalid."""
    m = re.fullmatch(r"\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmbt])?\s*", str(text).lower())
    if not m:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[m.group(2) or ""]
    val = round(float(m.group(1).replace(",", "")) * mult)
    return int(val) if val > 0 else None


def _fmt_price(n):
    n = float(n or 0)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"${n / div:,.2f}{suf}"
    return f"${n:,.0f}"


PANEL_IMAGE_LIMITS = {"banner": 5 * 1024 * 1024, "logo": 2 * 1024 * 1024}


def _panel_fs():
    import gridfs
    return gridfs.GridFS(db, collection="panel_images")


def _sniff_image(data: bytes):
    """(extension, mimetype) from the file's real bytes, or None if it isn't png / jpg / gif / webp."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _builds_fs():
    import gridfs
    return gridfs.GridFS(db, collection="build_files")


# ── Builder tracker (collection builder_ledger) ──────────────────────────────
#   builder_ledger {guild_id, builder_id, builder_name, build_name, worth (gross, no fee),
#                   received (after fee), rating (1-5 | None), paid (bool), source, ticket_id, created_at}
# The bot creates entries automatically (cogs/builds.py); here they can be added / edited / removed by hand.
PAYOUT_FEE_PERCENT = 15


def _parse_amount(text, allow_zero=True):
    """'250k' / '1.5m' / '0' -> int. Empty -> 0. None if invalid."""
    text = str(text or "").strip()
    if text in ("", "0"):
        return 0 if allow_zero else None
    return _parse_price(text)


def _member_label(m):
    u = m.get("user") or {}
    return m.get("nick") or u.get("global_name") or u.get("username") or ""


def _guild_members(guild_id, max_pages=5):
    """Members via the REST API (needs the bot's Server Members intent). [] if unavailable."""
    out, after = [], "0"
    for _ in range(max_pages):
        page = _discord_get(f"/guilds/{guild_id}/members?limit=1000&after={after}")
        if not page:
            break
        out.extend(page)
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]
    return out


def _tracker_data(guild_id, settings, builder_igns):
    allow = set(settings.get("allow_role_ids") or [])
    names = {}
    ids = set()
    if allow:
        for m in _guild_members(guild_id):
            u = m.get("user") or {}
            if u.get("bot"):
                continue
            if allow & set(m.get("roles") or []):
                ids.add(u["id"])
                names[u["id"]] = _member_label(m)
    entries = list(db["builder_ledger"].find({"guild_id": guild_id}).sort("created_at", -1))
    by_builder = {}
    for e in entries:
        e["id"] = str(e["_id"])
        by_builder.setdefault(e["builder_id"], []).append(e)
        ids.add(e["builder_id"])
        if e.get("builder_name"):
            names.setdefault(e["builder_id"], e["builder_name"])
    for b in builder_igns:
        ids.add(b["user_id"])
        if b.get("user_name"):
            names.setdefault(b["user_id"], b["user_name"])
    lookups = 0
    builders = []
    for uid in ids:
        if not names.get(uid) and lookups < 20:
            lookups += 1
            names[uid] = _member_label(_discord_get(f"/guilds/{guild_id}/members/{uid}") or {})
        rows = by_builder.get(uid, [])
        rated = [r["rating"] for r in rows if r.get("rating")]
        paid_rows = [r for r in rows if r.get("paid")]
        for r in rows:
            r["worth_fmt"] = _fmt_price(r.get("worth"))
            r["received_fmt"] = _fmt_price(r.get("received"))
            r["worth_raw"] = int(r.get("worth") or 0)
            r["received_raw"] = int(r.get("received") or 0)
            r["when"] = r["created_at"].strftime("%Y-%m-%d") if hasattr(r.get("created_at"), "strftime") else ""
        builders.append({
            "id": uid, "name": names.get(uid) or "Unknown user", "entries": rows,
            "builds_done": len(rows),
            "worth": _fmt_price(sum(int(r.get("worth") or 0) for r in rows)),
            "received": _fmt_price(sum(int(r.get("received") or 0) for r in paid_rows)),
            "pending": sum(1 for r in rows if not r.get("paid")),
            "rating": f"{sum(rated) / len(rated):.2f}" if rated else "—",
            "rating_n": len(rated),
        })
    builders.sort(key=lambda b: (-b["builds_done"], b["name"].lower()))
    return {"builders": builders, "members_ok": bool(allow)}


def _ledger_post(guild_id, base, form_type):
    f = request.form
    if form_type == "ledger_save":
        uid = f.get("user_id", "").strip()
        if not uid.isdigit():
            return redirect(f"{base}?error=ledger_user#tracker")
        name = f.get("build_name", "").strip()[:80]
        worth = _parse_amount(f.get("worth"))
        received = _parse_amount(f.get("received"))
        if not name or worth is None or received is None:
            return redirect(f"{base}?error=ledger_form#tracker")
        rating_txt = f.get("rating", "").strip()
        rating = None
        if rating_txt:
            if not rating_txt.isdigit() or not 1 <= int(rating_txt) <= 5:
                return redirect(f"{base}?error=ledger_form#tracker")
            rating = int(rating_txt)
        fields = {"build_name": name, "worth": worth, "received": received, "rating": rating,
                  "paid": f.get("paid") == "on", "builder_id": uid}
        entry_id = f.get("entry_id", "").strip()
        if entry_id:
            try:
                res = db["builder_ledger"].update_one({"_id": ObjectId(entry_id), "guild_id": guild_id},
                                                      {"$set": {**fields, "edited_by": "dashboard"}})
            except Exception:
                res = None
            if not res or not res.matched_count:
                return redirect(f"{base}?error=ledger_missing#tracker")
            return redirect(f"{base}?saved=ledger#tracker")
        m = _discord_get(f"/guilds/{guild_id}/members/{uid}")
        bname = _member_label(m) if m else ""
        db["builder_ledger"].insert_one({**fields, "guild_id": guild_id, "builder_name": bname,
                                         "source": "manual", "created_at": datetime.utcnow()})
        return redirect(f"{base}?saved=ledger#tracker")

    if form_type == "ledger_delete":
        try:
            db["builder_ledger"].delete_one({"_id": ObjectId(f.get("entry_id", "")), "guild_id": guild_id})
        except Exception:
            pass
        return redirect(f"{base}?saved=ledger_deleted#tracker")

    if form_type == "ledger_clear":
        uid = f.get("user_id", "").strip()
        if uid.isdigit():
            db["builder_ledger"].delete_many({"guild_id": guild_id, "builder_id": uid})
        return redirect(f"{base}?saved=ledger_deleted#tracker")

    if form_type == "ledger_reset_all":
        # Wipes build stats for this server: tracker entries, payments received, builder payouts, reviews.
        # Builder IGNs, builds catalog, panels, settings, quotes, open tickets and live payments are kept.
        if f.get("confirm", "").strip() != "RESET":
            return redirect(f"{base}?error=reset_confirm#tracker")
        gid = {"guild_id": guild_id}
        not_live = {"guild_id": guild_id, "status": {"$ne": "tracking"}}
        removed = 0
        removed += db["builder_ledger"].delete_many(gid).deleted_count
        removed += db["build_payments"].delete_many(not_live).deleted_count
        removed += db["build_payouts"].delete_many(not_live).deleted_count
        removed += db["build_reviews"].delete_many(gid).deleted_count
        # finished tickets: stop "Import past finished builds" re-adding them and cancel pending auto reviews
        db["build_tickets"].update_many(
            {"guild_id": guild_id, "finished": True, "status": {"$ne": "open"}}, {"$set": {"ledger_reset": True}})
        db["build_tickets"].update_many(
            {"guild_id": guild_id, "review_status": "pending"}, {"$set": {"review_status": "reset"}})
        return redirect(f"{base}?saved=builds_reset&n={removed}#tracker")

    # ledger_backfill: import finished builds from before the tracker existed (safe to run again)
    _fee = (db["build_settings"].find_one({"_id": guild_id}) or {}).get("fee_percent")
    fee_pct_ledger = int(_fee) if _fee not in (None, "") else PAYOUT_FEE_PERCENT
    added = 0
    for t in db["build_tickets"].find({"guild_id": guild_id, "finished": True, "claimed_by": {"$ne": None},
                                       "ledger_reset": {"$ne": True}}):
        price = int(t.get("payout_price") or 0)
        name = t.get("payout_build_name") or ""
        if not price:
            if t.get("build_id"):
                try:
                    b = db["builds"].find_one({"_id": ObjectId(t["build_id"])})
                except Exception:
                    b = None
                if b:
                    price, name = int(b.get("price") or 0), name or b.get("name", "")
            elif t.get("quote_price"):
                price, name = int(t["quote_price"]), name or "Custom Build"
        res = db["builder_ledger"].update_one(
            {"guild_id": guild_id, "ticket_id": str(t["_id"])},
            {"$setOnInsert": {
                "builder_id": t["claimed_by"], "builder_name": "", "build_name": name or "Build",
                "worth": price, "received": int(t.get("payout_amount") or price * (100 - fee_pct_ledger) // 100),
                "rating": t.get("review_rating"), "paid": t.get("payout_status") == "paid",
                "source": "backfill", "created_at": t.get("finished_at") or datetime.utcnow()}},
            upsert=True)
        if res.upserted_id:
            added += 1
    return redirect(f"{base}?saved=ledger_backfill&n={added}#tracker")


@app.route("/admin/bot-servers/<guild_id>/manage", methods=["GET", "POST"])
@require_admin
def admin_bot_server_manage(guild_id):
    if not guild_id.isdigit():
        return ("Not found", 404)
    base = f"/admin/bot-servers/{guild_id}/manage"
    if db is None:
        return redirect("/admin/bot-servers?error=db")

    roles = _discord_get(f"/guilds/{guild_id}/roles")
    channels = _discord_get(f"/guilds/{guild_id}/channels")
    if roles is None or channels is None:
        return redirect("/admin/bot-servers?error=manage")   # bot isn't in that server / can't see it
    role_ids = {r["id"] for r in roles}
    categories = sorted((c for c in channels if c.get("type") == 4), key=lambda c: c.get("position", 0))
    text_channels = sorted((c for c in channels if c.get("type") in (0, 5)), key=lambda c: c.get("position", 0))
    text_ids = {c["id"] for c in text_channels}

    if request.method == "POST":
        form_type = request.form.get("form_type")

        if form_type == "save_roles":
            ping = request.form.get("ping_role_id", "")
            manager = request.form.get("manager_role_id", "")
            allow = [r for r in request.form.getlist("allow_roles") if r in role_ids]
            cat_name = request.form.get("category_name", "").strip()[:100]
            receiver_ign = request.form.get("receiver_ign", "").strip()
            log_ch = request.form.get("payment_log_channel_id", "")
            if log_ch not in text_ids:
                log_ch = ""
            review_ch = request.form.get("review_channel_id", "")
            if review_ch not in text_ids:
                review_ch = ""
            payout_ch = request.form.get("builder_payout_log_channel_id", "")
            if payout_ch not in text_ids:
                payout_ch = ""
            fin_name = request.form.get("finished_category_name", "").strip()[:100]
            fin_id = ""
            if fin_name:
                fmatch = next((c for c in categories if c["name"].strip().lower() == fin_name.lower()), None)
                fin_id = fmatch["id"] if fmatch else ""
            if receiver_ign and not re.fullmatch(r"\.?[A-Za-z0-9_]{1,24}", receiver_ign):
                return redirect(f"{base}?error=ign")
            p_title = request.form.get("panel_title", "").strip()[:80]
            p_intro = request.form.get("panel_intro", "").strip()[:400]
            p_color = request.form.get("panel_color", "").strip()
            if p_color:
                if not re.fullmatch(r"#?[0-9a-fA-F]{6}", p_color):
                    return redirect(f"{base}?error=panel_color")
                p_color = "#" + p_color.lstrip("#").lower()
            # banner / logo uploads (kept when the file box is left empty; replaced / removed otherwise)
            img_updates, old_ids = {}, []
            old_settings = db["build_settings"].find_one({"_id": guild_id}) or {}
            for kind in ("banner", "logo"):
                up = request.files.get(f"panel_{kind}")
                if up and up.filename:
                    data = up.read(PANEL_IMAGE_LIMITS[kind] + 1)
                    if len(data) > PANEL_IMAGE_LIMITS[kind]:
                        return redirect(f"{base}?error=panel_image_size")
                    sniff = _sniff_image(data)
                    if not sniff:
                        return redirect(f"{base}?error=panel_image_type")
                    img_updates[kind] = (data, sniff)
                elif request.form.get(f"remove_{kind}") == "on":
                    img_updates[kind] = None
            ppb_txt = request.form.get("price_per_block", "").strip()
            ppb = _parse_amount(ppb_txt)          # '' / '0' -> 0 (digouts off), '2.5k' -> 2500
            if ppb is None:
                return redirect(f"{base}?error=ppb")
            fee_txt = request.form.get("fee_percent", "").strip().rstrip("%").strip()
            if fee_txt == "":
                fee_pct = PAYOUT_FEE_PERCENT
            else:
                try:
                    fee_pct = float(fee_txt)
                except ValueError:
                    return redirect(f"{base}?error=fee")
                if not 0 <= fee_pct <= 100 or fee_pct != int(fee_pct):
                    return redirect(f"{base}?error=fee")
                fee_pct = int(fee_pct)
            lim_txt = request.form.get("max_tickets_per_user", "").strip()
            if lim_txt == "":
                max_tickets = 2
            else:
                try:
                    max_tickets = int(lim_txt)
                except ValueError:
                    return redirect(f"{base}?error=ticket_limit")
                if not 1 <= max_tickets <= 50:
                    return redirect(f"{base}?error=ticket_limit")
            cat_id = ""
            if cat_name:
                match = next((c for c in categories if c["name"].strip().lower() == cat_name.lower()), None)
                cat_id = match["id"] if match else ""   # missing -> the bot creates it by name
            db["build_settings"].update_one(
                {"_id": guild_id},
                {"$set": {
                    "ping_role_id": ping if ping in role_ids else "",
                    "allow_role_ids": allow,
                    "manager_role_id": manager if manager in role_ids else "",
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "receiver_ign": receiver_ign,
                    "payment_log_channel_id": log_ch,
                    "review_channel_id": review_ch,
                    "builder_payout_log_channel_id": payout_ch,
                    "finished_category_id": fin_id,
                    "finished_category_name": fin_name,
                    "price_per_block": ppb,
                    "fee_percent": fee_pct,
                    "max_tickets_per_user": max_tickets,
                    "panel_title": p_title,
                    "panel_intro": p_intro,
                    "panel_color": p_color,
                }}, upsert=True)
            try:
                if int(float(old_settings.get("owner_fee_percent") or 0)) > fee_pct:
                    db["build_settings"].update_one({"_id": guild_id}, {"$set": {"owner_fee_percent": fee_pct}})
            except (TypeError, ValueError):
                pass
            for kind, upd in img_updates.items():
                old_id = old_settings.get(f"panel_{kind}_file_id")
                if upd is None:
                    db["build_settings"].update_one({"_id": guild_id}, {"$unset": {
                        f"panel_{kind}_file_id": "", f"panel_{kind}_name": ""}})
                else:
                    data, (ext, mime) = upd
                    fid = _panel_fs().put(data, filename=f"{kind}.{ext}", guild_id=guild_id, content_type=mime)
                    db["build_settings"].update_one({"_id": guild_id}, {"$set": {
                        f"panel_{kind}_file_id": str(fid), f"panel_{kind}_name": f"{kind}.{ext}"}})
                if old_id:
                    try:
                        _panel_fs().delete(ObjectId(old_id))
                    except Exception:
                        pass
            return redirect(f"{base}?saved=roles")

        if form_type == "save_owner":
            owner_ign = request.form.get("owner_ign", "").strip()
            if owner_ign and not re.fullmatch(r"\.?[A-Za-z0-9_]{1,24}", owner_ign):
                return redirect(f"{base}?error=owner_ign")
            cur = db["build_settings"].find_one({"_id": guild_id}) or {}
            cur_fee = cur.get("fee_percent")
            cur_fee = PAYOUT_FEE_PERCENT if cur_fee in (None, "") else int(cur_fee)
            fee_txt = request.form.get("owner_fee_percent", "").strip().rstrip("%").strip()
            if fee_txt == "":
                owner_fee = 0                       # empty = no owner fee
            else:
                try:
                    owner_fee = float(fee_txt)
                except ValueError:
                    return redirect(f"{base}?error=owner_fee")
                # the owner is paid out of the server fee: never below 1, never above the fee %
                if owner_fee != int(owner_fee) or not 1 <= owner_fee <= cur_fee:
                    return redirect(f"{base}?error=owner_fee")
                owner_fee = int(owner_fee)
            if owner_fee and not owner_ign:
                return redirect(f"{base}?error=owner_ign_missing")
            db["build_settings"].update_one(
                {"_id": guild_id}, {"$set": {"owner_ign": owner_ign, "owner_fee_percent": owner_fee}}, upsert=True)
            return redirect(f"{base}?saved=owner")

        if form_type == "save_bot_profile":
            payload = {}
            nick = request.form.get("bot_nick", "").strip()[:32]
            bio = request.form.get("bot_bio", "").strip()[:400]
            # only send what was actually edited (Discord may not echo the bio back, so never wipe it blindly)
            if nick != request.form.get("orig_bot_nick", "").strip():
                payload["nick"] = nick or None      # empty = back to the bot's default name
            if bio != request.form.get("orig_bot_bio", "").strip():
                payload["bio"] = bio or None
            for kind in ("avatar", "banner"):
                up = request.files.get(f"bot_{kind}")
                if up and up.filename:
                    data = up.read(BOT_PROFILE_LIMITS[kind] + 1)
                    if len(data) > BOT_PROFILE_LIMITS[kind]:
                        return redirect(f"{base}?error=bot_image_size")
                    sniff = _sniff_image(data)
                    if not sniff:
                        return redirect(f"{base}?error=panel_image_type")
                    payload[kind] = f"data:{sniff[1]};base64,{base64.b64encode(data).decode()}"
                elif request.form.get(f"remove_bot_{kind}") == "on":
                    payload[kind] = None
            if not payload:
                return redirect(f"{base}?saved=bot_profile")
            ok, res = _discord_patch(f"/guilds/{guild_id}/members/@me", payload)
            if not ok:
                from urllib.parse import quote
                return redirect(f"{base}?error=bot_profile&msg={quote(str(res)[:200])}")
            return redirect(f"{base}?saved=bot_profile")

        if form_type == "save_build":
            name = request.form.get("name", "").strip()[:80]
            price_text = request.form.get("price", "").strip()
            price = _parse_price(price_text)
            build_id = request.form.get("build_id", "").strip()
            upload = request.files.get("file")
            has_file = bool(upload and upload.filename)
            if not name:
                return redirect(f"{base}?error=name")
            if price is None:
                return redirect(f"{base}?error=price")
            if has_file and not upload.filename.lower().endswith(".litematic"):
                return redirect(f"{base}?error=filetype")
            if not build_id and not has_file:
                return redirect(f"{base}?error=nofile")

            fs = _builds_fs()
            fields = {"name": name, "price": price, "price_text": price_text}
            old = None
            if build_id:
                try:
                    old = db["builds"].find_one({"_id": ObjectId(build_id), "guild_id": guild_id})
                except Exception:
                    old = None
                if old is None:
                    return redirect(f"{base}?error=notfound")
            if has_file:
                data = upload.read()
                if not data:
                    return redirect(f"{base}?error=nofile")
                fields["file_id"] = fs.put(data, filename=upload.filename, guild_id=guild_id)
                fields["filename"] = os.path.basename(upload.filename)
                fields["size"] = len(data)
            if old:
                db["builds"].update_one({"_id": old["_id"]}, {"$set": fields})
                if has_file and old.get("file_id"):
                    try:
                        fs.delete(old["file_id"])
                    except Exception:
                        pass
                return redirect(f"{base}?saved=build")
            db["builds"].insert_one({**fields, "guild_id": guild_id, "created_at": datetime.utcnow()})
            return redirect(f"{base}?saved=build")

        if form_type == "save_builder_ign":
            uid = request.form.get("user_id", "").strip()
            b_ign = request.form.get("builder_ign", "").strip()
            if not uid.isdigit() or not re.fullmatch(r"\.?[A-Za-z0-9_]{1,24}", b_ign):
                return redirect(f"{base}?error=bignform")
            member = _discord_get(f"/guilds/{guild_id}/members/{uid}")
            if not member:
                return redirect(f"{base}?error=bmember")
            ign_l = b_ign.lower().lstrip(".")
            if db["builder_igns"].find_one({"guild_id": guild_id, "ign_lower": ign_l, "user_id": {"$ne": uid}}):
                return redirect(f"{base}?error=bigntaken")
            if db["build_tickets"].find_one({"guild_id": guild_id, "claimed_by": uid,
                                             "payout_status": "tracking", "status": "open"}):
                return redirect(f"{base}?error=btracking")
            u = member.get("user") or {}
            name = member.get("nick") or u.get("global_name") or u.get("username") or uid
            now = datetime.utcnow()
            flt = {"guild_id": guild_id, "user_id": uid}
            existing = db["builder_igns"].find_one(flt)
            upd = {"$set": {"ign": b_ign, "ign_lower": ign_l, "user_name": name, "verified": False,
                            "source": "dashboard", "set_by": "dashboard", "verified_at": now},
                   "$setOnInsert": {"created_at": now}}
            if existing and existing.get("ign") and existing["ign"] != b_ign:
                upd["$push"] = {"previous_igns": {"ign": existing["ign"], "changed_at": now, "by": "dashboard"}}
            db["builder_igns"].update_one(flt, upd, upsert=True)
            return redirect(f"{base}?saved=bign")

        if form_type == "remove_builder_ign":
            uid = request.form.get("user_id", "").strip()
            if not uid.isdigit():
                return redirect(base)
            if db["build_tickets"].find_one({"guild_id": guild_id, "claimed_by": uid,
                                             "payout_status": "tracking", "status": "open"}):
                return redirect(f"{base}?error=btracking")
            db["builder_igns"].delete_one({"guild_id": guild_id, "user_id": uid})
            return redirect(f"{base}?saved=bign_removed")

        if form_type in ("ledger_save", "ledger_delete", "ledger_clear", "ledger_backfill", "ledger_reset_all"):
            return _ledger_post(guild_id, base, form_type)

        if form_type == "delete_build":
            try:
                b = db["builds"].find_one_and_delete({"_id": ObjectId(request.form.get("build_id", "")),
                                                      "guild_id": guild_id})
            except Exception:
                b = None
            if b and b.get("file_id"):
                try:
                    _builds_fs().delete(b["file_id"])
                except Exception:
                    pass
            return redirect(f"{base}?saved=deleted")

        return redirect(base)

    settings = db["build_settings"].find_one({"_id": guild_id}) or {}
    cat_raw = settings.get("category_id") or ""
    cat_name = settings.get("category_name") or ""
    if not cat_name:
        if cat_raw.startswith("__new__:"):
            cat_name = cat_raw[8:]
        else:
            cat_name = next((c["name"] for c in categories if c["id"] == cat_raw), "")
    builds = list(db["builds"].find({"guild_id": guild_id}).sort("created_at", 1))
    for b in builds:
        b["id"] = str(b["_id"])
        b["price_fmt"] = _fmt_price(b.get("price"))
    builder_igns = list(db["builder_igns"].find({"guild_id": guild_id}).sort("ign_lower", 1))
    lookups = 0
    for b in builder_igns:
        b["_id"] = str(b["_id"])
        if not b.get("user_name") and lookups < 25:       # older rows saved before names were stored
            lookups += 1
            m = _discord_get(f"/guilds/{guild_id}/members/{b['user_id']}") or {}
            mu = m.get("user") or {}
            b["user_name"] = m.get("nick") or mu.get("global_name") or mu.get("username") or ""
        b["updated"] = b["verified_at"].strftime("%Y-%m-%d") if hasattr(b.get("verified_at"), "strftime") else ""
    guild_info = _discord_get(f"/guilds/{guild_id}") or {"id": guild_id, "name": guild_id}
    roles = sorted((r for r in roles if r["id"] != guild_id), key=lambda r: -r.get("position", 0))
    return render_template(
        "admin_bot_server_manage.html", guild=guild_info, roles=roles, categories=categories, text_channels=text_channels,
        settings=settings, category_name=cat_name, builds=builds, builder_igns=builder_igns,
        bot_profile=_bot_profile_view(guild_id),
        tracker=_tracker_data(guild_id, settings, builder_igns))


# ── Verified IGN cache (collection verified_igns) ────────────────────────────
# The bot adds an IGN here after verifying it in-game (source "bot") and never looks it up again.
# IGNs added by hand here (source "admin") count as verified too.
_IGN_RE = re.compile(r"\.?[A-Za-z0-9_]{1,24}")


@app.route("/admin/verified-igns", methods=["GET", "POST"])
@require_admin
def admin_verified_igns():
    if db is None:
        return redirect("/admin?error=db")
    col = db["verified_igns"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            raw = re.split(r"[\s,;]+", request.form.get("igns", "").strip())
            added = bad = 0
            now = datetime.utcnow()
            for ign in dict.fromkeys(x for x in raw if x):        # unique, keep order
                if not _IGN_RE.fullmatch(ign):
                    bad += 1
                    continue
                res = col.update_one(
                    {"ign_lower": ign.lower()},
                    {"$set": {"ign": ign, "source": "admin", "added_by": "dashboard", "added_at": now},
                     "$setOnInsert": {"uses": 0}}, upsert=True)
                added += 1
            return redirect(f"/admin/verified-igns?saved=added&n={added}&bad={bad}")
        if action == "delete":
            try:
                col.delete_one({"_id": ObjectId(request.form.get("id", ""))})
            except Exception:
                pass
            return redirect("/admin/verified-igns?saved=deleted")
        if action == "delete_bot":
            n = col.delete_many({"source": "bot"}).deleted_count
            return redirect(f"/admin/verified-igns?saved=cleared&n={n}")
        return redirect("/admin/verified-igns")

    q = request.args.get("q", "").strip().lower()
    flt = {"ign_lower": {"$regex": re.escape(q)}} if q else {}
    total = col.count_documents({})
    matched = col.count_documents(flt) if q else total
    rows = list(col.find(flt).sort("ign_lower", 1).limit(500))
    for r in rows:
        r["id"] = str(r["_id"])
        r["added"] = r["added_at"].strftime("%Y-%m-%d") if hasattr(r.get("added_at"), "strftime") else "—"
        lu = r.get("last_used_at")
        r["last_used"] = lu.strftime("%Y-%m-%d") if hasattr(lu, "strftime") else "never"
    stats = {"total": total, "bot": col.count_documents({"source": "bot"}),
             "admin": col.count_documents({"source": "admin"})}
    return render_template("admin_verified_igns.html", rows=rows, q=q, stats=stats, matched=matched)


@app.route("/admin/bot-servers/<guild_id>/payments")
@require_admin
def admin_bot_server_payments(guild_id):
    """Payment logs for the build service: every Start Pay attempt and how it ended."""
    if not guild_id.isdigit():
        return ("Not found", 404)
    if db is None:
        return redirect("/admin/bot-servers?error=db")
    guild_info = _discord_get(f"/guilds/{guild_id}") or {"id": guild_id, "name": guild_id}
    docs = list(db["build_payments"].find({"guild_id": guild_id}).sort("started_at", -1).limit(500))

    def naive(dt):
        return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt

    now = datetime.utcnow()
    stats = {"paid": 0, "paid_total": 0, "not_detected": 0, "cancelled": 0, "other": 0}
    rows = []
    for d in docs:
        status = d.get("status", "tracking")
        started = naive(d.get("started_at"))
        # still "tracking" long after the 5-minute window = the bot restarted mid-watch
        if status == "tracking" and started and (now - started) > timedelta(minutes=7):
            status = "interrupted"
        if status == "paid":
            stats["paid"] += 1
            stats["paid_total"] += int(d.get("amount") or 0)
        elif status == "not_detected":
            stats["not_detected"] += 1
        elif status == "cancelled":
            stats["cancelled"] += 1
        else:
            stats["other"] += 1
        rows.append({
            "status": status,
            "when": started.strftime("%Y-%m-%d %H:%M") if started else "—",
            "build": d.get("build_name", "—"),
            "payer": d.get("payer", "—"),
            "receiver": d.get("receiver", "—"),
            "amount": _fmt_price(d.get("amount")),
            "customer": d.get("opener_name") or d.get("opener_id") or "—",
            "claimed_by": d.get("claimed_by_name") or "",
            "channel": d.get("channel_name") or "",
            "message": d.get("server_message") or "",
            "id": str(d["_id"]),
        })
    flt = request.args.get("status", "")
    if flt in ("paid", "not_detected", "cancelled"):
        rows = [r for r in rows if r["status"] == flt]
    elif flt == "other":
        rows = [r for r in rows if r["status"] not in ("paid", "not_detected", "cancelled")]
    stats["paid_total_fmt"] = _fmt_price(stats["paid_total"])
    return render_template("admin_bot_server_payments.html", guild=guild_info, rows=rows, stats=stats, flt=flt,
                           total=len(docs))


@app.route("/admin/bot-servers/<guild_id>/payments/<payment_id>/delete", methods=["POST"])
@require_admin
def admin_bot_server_payment_delete(guild_id, payment_id):
    """Remove a payment log entry and everything that came from it (tracker entry, builder payouts,
    review, recap totals). If other paid payments remain on the same ticket, only the entry itself goes."""
    if db is None or not guild_id.isdigit():
        return ("Not found", 404)
    back = f"/admin/bot-servers/{guild_id}/payments"
    try:
        pay = db["build_payments"].find_one({"_id": ObjectId(payment_id), "guild_id": guild_id})
    except Exception:
        pay = None
    if not pay:
        return redirect(f"{back}?error=notfound")
    db["build_payments"].delete_one({"_id": pay["_id"]})
    cascaded = 0
    ch = pay.get("channel_id")
    if ch and pay.get("status") == "paid":
        still_paid = db["build_payments"].count_documents(
            {"guild_id": guild_id, "channel_id": ch, "status": "paid"})
        if not still_paid:
            tickets = list(db["build_tickets"].find({"guild_id": guild_id, "channel_id": ch}, {"_id": 1}))
            tids = [str(t["_id"]) for t in tickets]
            if tids:
                cascaded += db["builder_ledger"].delete_many(
                    {"guild_id": guild_id, "ticket_id": {"$in": tids}}).deleted_count
                # stop "Import past finished builds" from re-adding it
                db["build_tickets"].update_many({"guild_id": guild_id, "channel_id": ch},
                                                {"$set": {"ledger_reset": True}})
            cascaded += db["build_payouts"].delete_many({"guild_id": guild_id, "channel_id": ch}).deleted_count
            cascaded += db["build_reviews"].delete_many({"guild_id": guild_id, "channel_id": ch}).deleted_count
    return redirect(f"{back}?deleted=1&n={cascaded}")


@app.template_filter("money")
def _money_filter(n):
    return _fmt_price(n)


@app.route("/admin/bot-servers/<guild_id>/dashboard")
@require_admin
def admin_bot_server_dashboard(guild_id):
    """Everything about the build service in one place: money, fee split, owner, pipeline, tickets, refunds, builders."""
    if not guild_id.isdigit():
        return ("Not found", 404)
    if db is None:
        return redirect("/admin/bot-servers?error=db")
    guild_info = _discord_get(f"/guilds/{guild_id}") or {"id": guild_id, "name": guild_id}
    lookups = {"n": 0}

    def resolve(uid):
        if lookups["n"] >= 25:          # keep the page fast: Discord is only asked for names the bot never stored
            return ""
        lookups["n"] += 1
        return _member_label(_discord_get(f"/guilds/{guild_id}/members/{uid}") or {})

    data, error = None, ""
    try:
        from utils.build_dashboard import build_dashboard
        data = build_dashboard(db, guild_id, resolve)
    except Exception as e:
        logger.exception("build dashboard failed")
        error = f"{type(e).__name__}: {e}"
    return render_template("admin_bot_server_dashboard.html", guild=guild_info, data=data, error=error)


@app.route("/admin/bot-servers/<guild_id>/builds/<build_id>/download")
@require_admin
def admin_bot_server_build_download(guild_id, build_id):
    if db is None or not guild_id.isdigit():
        return ("Not found", 404)
    try:
        b = db["builds"].find_one({"_id": ObjectId(build_id), "guild_id": guild_id})
        data = _builds_fs().get(b["file_id"]).read()
    except Exception:
        return ("Not found", 404)
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=b.get("filename") or f"{b['name']}.litematic")


@app.route("/admin/bot-servers/<guild_id>/panel-image/<kind>")
@require_admin
def admin_panel_image(guild_id, kind):
    """Preview of the uploaded panel banner / logo (dashboard only)."""
    if db is None or not guild_id.isdigit() or kind not in ("banner", "logo"):
        return ("Not found", 404)
    try:
        st = db["build_settings"].find_one({"_id": guild_id}) or {}
        f = _panel_fs().get(ObjectId(st[f"panel_{kind}_file_id"]))
        data = f.read()
        mime = getattr(f, "content_type", None) or "application/octet-stream"
    except Exception:
        return ("Not found", 404)
    return send_file(io.BytesIO(data), mimetype=mime, max_age=0)


@app.route("/admin/purge-other-users", methods=["POST"])
@require_admin
def admin_purge_other_users():
    """Delete everything that belongs to anyone except the owner/worker account:
    their MC sessions (logged out first), schedules, saved login caches, and any
    leftover plan/usage/settings rows. Also drops the old plan config entirely."""
    if db is None:
        return redirect("/admin?purged=dberr")
    keep = list(ALLOWED_USER_IDS)
    removed_sessions = 0
    try:
        others = list(db["mc_sessions"].find({"discord_id": {"$nin": keep}}, {"session_id": 1}))
        sids = [d["session_id"] for d in others if d.get("session_id")]
        for sid in sids:
            try:
                requests.post(f"{MC_BOT_URL}/full-logout/{sid}", timeout=10)
            except Exception:
                pass
        if sids:
            db["mc_schedules"].delete_many({"session_id": {"$in": sids}})
            db["mc_auth_cache"].delete_many({"session_id": {"$in": sids}})
        removed_sessions = db["mc_sessions"].delete_many({"discord_id": {"$nin": keep}}).deleted_count
        for coll in ("user_plan", "usage_daily", "user_settings"):
            db[coll].delete_many({"discord_id": {"$nin": keep}})
        # plans/limits no longer exist
        for coll in ("plan_config", "user_plan", "usage_daily"):
            db[coll].drop()
    except Exception as e:
        logger.error(f"purge other users failed: {e}")
        return redirect("/admin?purged=err")
    logger.info(f"Purged other users: {removed_sessions} session(s) removed.")
    return redirect(f"/admin?purged={removed_sessions}")


# ── Stats channels (where /stats and !stats may be used) ─────────────────────
# Same global document the bot reads on every /stats call, so changes apply
# instantly. bot_global_settings {_id: "stats_channel", channels: [
#   {channel_id, guild_id, guild_name, channel_name, invite_url, set_by}, ...]}
# No channels listed = /stats works everywhere.
from urllib.parse import urlencode as _urlencode


def _sc_redirect(msg=None, err=None):
    q = _urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return redirect("/admin/stats-channels" + (f"?{q}" if q else ""))


_STATS_DOC = {"_id": "stats_channel"}
_STATS_ENTRY_KEYS = ("channel_id", "guild_id", "guild_name", "channel_name", "invite_url", "set_by", "no_advertise")


def _load_stats_entries() -> list:
    if db is None:
        return []
    try:
        doc = db["bot_global_settings"].find_one(_STATS_DOC)
    except Exception as e:
        logger.warning(f"stats channels read failed: {e}")
        return []
    if not doc:
        return []
    if isinstance(doc.get("channels"), list):
        return [e for e in doc["channels"] if e.get("channel_id")]
    if doc.get("channel_id"):  # old single-channel format
        return [{k: doc.get(k) for k in _STATS_ENTRY_KEYS}]
    return []


def _save_stats_entries(entries: list):
    coll = db["bot_global_settings"]
    if not entries:
        coll.delete_one(_STATS_DOC)
        return
    coll.update_one(
        _STATS_DOC,
        {"$set": {"channels": entries}, "$unset": {k: "" for k in _STATS_ENTRY_KEYS}},
        upsert=True,
    )


def _discord_headers():
    return {"Authorization": f"Bot {BOT_TOKEN}", "User-Agent": "DashboardBot/1.0"}


@app.route("/admin/stats-channels")
@require_admin
def admin_stats_channels():
    return render_template(
        "admin_stats_channels.html",
        entries=_load_stats_entries(),
        guilds=get_bot_guilds_detailed(),
        msg=request.args.get("msg"),
        err=request.args.get("err"),
    )


@app.route("/admin/stats-channels/guild/<guild_id>/channels")
@require_admin
def admin_stats_channels_list(guild_id):
    """Text channels of one server, for the dropdown on the Stats Channels page."""
    if not BOT_TOKEN:
        return jsonify({"ok": False, "error": "Bot token not configured."}), 500
    try:
        r = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                         headers=_discord_headers(), timeout=10)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if not r.ok:
        return jsonify({"ok": False, "error": f"Couldn't list channels ({r.status_code})."}), 400
    chans = sorted((c for c in r.json() if c.get("type") in (0, 5)), key=lambda c: c.get("position", 0))
    allowed = {str(e["channel_id"]) for e in _load_stats_entries()}
    return jsonify({"ok": True, "channels": [{"id": c["id"], "name": c["name"], "allowed": c["id"] in allowed}
                                             for c in chans]})


@app.route("/admin/stats-channels/guild/<guild_id>/save", methods=["POST"])
@require_admin
def admin_stats_channels_save(guild_id):
    """Tick list: the ticked channels of this server become its allowed stats channels."""
    if db is None:
        return _sc_redirect(err="Database unavailable.")
    if not BOT_TOKEN or not guild_id.isdigit():
        return _sc_redirect(err="Bot token not configured.")
    try:
        r = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                         headers=_discord_headers(), timeout=10)
        gd = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}", headers=_discord_headers(), timeout=10)
    except Exception as e:
        return _sc_redirect(err=f"Discord request failed: {e}")
    if not r.ok:
        return _sc_redirect(err=f"Couldn't list channels ({r.status_code}).")
    by_id = {c["id"]: c for c in r.json() if c.get("type") in (0, 5)}
    ticked = [c for c in request.form.getlist("channel_ids") if c in by_id]
    guild_name = gd.json().get("name") if gd.ok else None
    old = {str(e["channel_id"]): e for e in _load_stats_entries()}
    others = [e for e in old.values() if str(e.get("guild_id")) != guild_id]
    mine = [{"channel_id": int(cid), "guild_id": int(guild_id), "guild_name": guild_name,
             "channel_name": by_id[cid]["name"], "invite_url": (old.get(cid) or {}).get("invite_url"),
             "no_advertise": bool((old.get(cid) or {}).get("no_advertise")), "set_by": "admin-panel"} for cid in ticked]
    _save_stats_entries(others + mine)
    if not mine and not others:
        return _sc_redirect(msg="Nothing ticked: /stats works everywhere.")
    return _sc_redirect(msg=f"Saved {len(mine)} channel(s) for {guild_name or 'that server'}.")


@app.route("/admin/stats-channels/add", methods=["POST"])
@require_admin
def admin_stats_channels_add():
    if db is None:
        return _sc_redirect(err="Database unavailable.")
    if not BOT_TOKEN:
        return _sc_redirect(err="Bot token not configured.")
    guild_id = (request.form.get("guild_id") or "").strip()
    channel_id = (request.form.get("channel_id_manual") or request.form.get("channel_id") or "").strip()
    if not channel_id.isdigit():
        return _sc_redirect(err="Pick a channel or paste a channel ID.")

    try:
        ch = requests.get(f"https://discord.com/api/v10/channels/{channel_id}",
                          headers=_discord_headers(), timeout=10)
    except Exception as e:
        return _sc_redirect(err=f"Discord request failed: {e}")
    if not ch.ok or ch.json().get("type") not in (0, 5, 11, 12):
        return _sc_redirect(err="That isn't a text channel the bot can see.")
    if not guild_id.isdigit() or request.form.get("channel_id_manual"):
        guild_id = str(ch.json().get("guild_id") or "")
    if not guild_id.isdigit() or str(ch.json().get("guild_id")) != guild_id:
        return _sc_redirect(err="That channel isn't in that server.")
    try:
        gd = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}",
                          headers=_discord_headers(), timeout=10)
    except Exception:
        gd = None
    ch = ch.json()
    guild_name = gd.json().get("name") if (gd is not None and gd.ok) else None

    invite_url = None      # invites are no longer used: the bot doesn't point people at the stats channels

    entry = {
        "channel_id": int(channel_id), "guild_id": int(guild_id), "guild_name": guild_name,
        "channel_name": ch.get("name"), "invite_url": invite_url, "set_by": "admin-panel",
    }
    entries = [e for e in _load_stats_entries() if int(e["channel_id"]) != int(channel_id)] + [entry]
    _save_stats_entries(entries)
    return _sc_redirect(msg=f"Added #{ch.get('name')}")


@app.route("/admin/stats-channels/<channel_id>/advertise", methods=["POST"])
@require_admin
def admin_stats_channels_advertise(channel_id):
    """The 'Don't advertise' tick: when set, /stats never tells people about this channel."""
    if db is None:
        return jsonify({"ok": False, "error": "Database unavailable."}), 500
    hide = request.form.get("no_advertise") in ("1", "on", "true")
    entries = _load_stats_entries()
    found = False
    for e in entries:
        if str(e["channel_id"]) == str(channel_id):
            e["no_advertise"] = hide
            found = True
    if not found:
        return jsonify({"ok": False, "error": "Channel not in the list."}), 404
    _save_stats_entries(entries)
    return jsonify({"ok": True, "no_advertise": hide})


@app.route("/admin/stats-channels/<channel_id>/remove", methods=["POST"])
@require_admin
def admin_stats_channels_remove(channel_id):
    if db is None:
        return _sc_redirect(err="Database unavailable.")
    entries = _load_stats_entries()
    kept = [e for e in entries if str(e["channel_id"]) != str(channel_id)]
    _save_stats_entries(kept)
    return _sc_redirect(msg="Removed.")


@app.route("/admin/stats-channels/clear", methods=["POST"])
@require_admin
def admin_stats_channels_clear():
    if db is None:
        return _sc_redirect(err="Database unavailable.")
    _save_stats_entries([])
    return _sc_redirect(msg="All stats channels removed: /stats works everywhere.")


MC_BOT_URL = os.getenv("MC_BOT_URL", "http://127.0.0.1:3001")


def _safe_json(r):
    """Safely parse JSON from a requests.Response object."""
    try:
        return r.json()
    except Exception:
        return {"status": "error", "error": f"Invalid response from MC bot (HTTP {r.status_code})"}


def _get_session_doc(session_id: str):
    """Fetch a mc_sessions doc by session_id, or None."""
    if db is None or not session_id:
        return None
    return db["mc_sessions"].find_one({"session_id": session_id})


def _list_user_sessions(discord_id: str):
    if db is None or not discord_id:
        return []
    return list(db["mc_sessions"].find({"discord_id": str(discord_id)}))


def _serialize_session(doc, live_status=None):
    out = {
        "session_id": doc.get("session_id"),
        "label": doc.get("label") or doc.get("mc_username") or "Session",
        "mc_username": doc.get("mc_username"),
        "last_server": doc.get("last_server"),
        "last_version": doc.get("last_version", "latest"),
        "auto_reconnect": True,  # always on — no per-session toggle
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }
    if live_status:
        out["status"] = live_status.get("status")
        out["server"] = live_status.get("server") or doc.get("last_server")
        out["mc_username"] = live_status.get("mcUsername") or out["mc_username"]
    else:
        out["status"] = "disconnected"
        out["server"] = doc.get("last_server")
    return out



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
