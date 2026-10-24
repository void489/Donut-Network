const express = require('express');
const mineflayer = require('mineflayer');
const { Authflow, Titles } = require('prismarine-auth');
const path = require('path');
const fs = require('fs');

// mongodb is optional at the require level: if it's not installed (e.g. not
// yet added to mc-bot/package.json), the whole process must NOT crash on
// boot — that would take down every live Minecraft session with a hard
// "connection refused" on port 3001. Fall back to no persistence instead.
let MongoClient = null;
try {
    ({ MongoClient } = require('mongodb'));
} catch (e) {
    console.warn("⚠️  'mongodb' package not installed — run `npm install mongodb` in mc-bot/ "
        + "to enable schedule/session persistence across restarts. Continuing without it.");
}

const app = express();
app.use(express.json());

const PORT = process.env.MC_BOT_PORT || 3001;

// Last-resort safety net: Node's default behavior is to crash the whole
// process on an uncaught error, which for this service means every live
// Minecraft session and the Flask dashboard's proxy calls to it (port 3001)
// go down at once. Log and keep running instead — the bot.py supervisor's
// restart-with-backoff is the intended recovery path for genuinely fatal
// crashes, not a random unhandled promise rejection in one code path.
process.on('uncaughtException', (err) => {
    console.error('❌ [uncaughtException] MC bot service hit an unhandled error (continuing):', err);
});
process.on('unhandledRejection', (reason) => {
    console.error('❌ [unhandledRejection] MC bot service hit an unhandled rejection (continuing):', reason);
});

// In-memory state storage
const states = {};
const schedules = {}; // sessionId -> array of schedule objects

// Best-effort sessionId -> discordId map, used only by the debug/admin
// /sessions/:discordId endpoint below. Mongo (mc_sessions) remains the
// source of truth for which sessions *exist* for a Discord user; this map
// only helps answer which of those are *currently* live in this process.
const sessionOwners = {}; // sessionId -> discordId

// Ensure cache directory exists for auth tokens
const cacheDir = path.join(__dirname, 'cache');
if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
}

// Each session gets its OWN cache subdirectory, rather than everyone
// sharing one flat `cache/` folder and us guessing which filenames belong
// to which session by string-matching the session ID as a filename prefix.
// That prefix-matching was fragile: if prismarine-auth ever named a cache
// file differently than expected, backupAuthCache would silently match
// zero files and back up nothing — no error, just an empty no-op — which
// is exactly consistent with logins that "worked" but never actually
// survived a restart. A per-session folder removes the guesswork: whatever
// files land in it belong to that session, full stop, regardless of what
// prismarine-auth (or any future auth library) names them internally.
function sessionCacheDir(sessionId) {
    const dir = path.join(cacheDir, sessionId);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    return dir;
}

// ─── Mongo (persistence for restart-proofing) ──────────────────────────────
// Schedules used to live only in the `schedules` in-memory object, and no
// connection was ever restored automatically — a crash or redeploy of this
// process silently dropped every Auto Command and every live Minecraft
// session. Mongo's `mc_schedules` collection is now the source of truth for
// schedules (this process's in-memory copy is just a fast cache), and on
// boot we replay both the schedules and any auto-reconnect-enabled sessions
// from `mc_sessions` (shared with the Flask dashboard, see db.py).
let mongoDb = null;

async function connectMongo() {
    if (!MongoClient) return null; // 'mongodb' package missing — logged at require time above
    const uri = process.env.MONGO_URI;
    if (!uri) {
        console.warn('⚠️  MONGO_URI not set — schedules and auto-reconnect will NOT survive a restart.');
        return null;
    }
    try {
        const client = new MongoClient(uri, { serverSelectionTimeoutMS: 5000 });
        await client.connect();
        await client.db('discord_bot').command({ ping: 1 });
        console.log('✅ [mongo] MC bot connected to MongoDB.');
        const database = client.db('discord_bot');
        await database.collection('mc_schedules').createIndex({ session_id: 1, command: 1 }, { unique: true });
        await database.collection('mc_auth_cache').createIndex({ session_id: 1, filename: 1 }, { unique: true });
        return database;
    } catch (e) {
        console.error(`❌ [mongo] Failed to connect: ${e.message}`);
        return null;
    }
}

async function loadAllSchedulesFromDb() {
    if (!mongoDb) return;
    try {
        const docs = await mongoDb.collection('mc_schedules').find({}).toArray();
        for (const doc of docs) {
            if (!schedules[doc.session_id]) schedules[doc.session_id] = [];
            schedules[doc.session_id].push({
                command: doc.command,
                interval: doc.interval,
                enabled: doc.enabled,
                // Stagger first runs on boot instead of firing every schedule
                // at once the moment its session reconnects.
                nextRun: Date.now() + Math.min(doc.interval, 15000)
            });
        }
        console.log(`✅ [mongo] Restored ${docs.length} scheduled command(s) across ${Object.keys(schedules).length} session(s).`);
    } catch (e) {
        console.error(`❌ [mongo] Failed to load schedules: ${e.message}`);
    }
}

async function persistSchedule(sessionId, schedule) {
    if (!mongoDb) return;
    try {
        await mongoDb.collection('mc_schedules').updateOne(
            { session_id: sessionId, command: schedule.command },
            { $set: { interval: schedule.interval, enabled: schedule.enabled, updated_at: new Date() } },
            { upsert: true }
        );
    } catch (e) {
        console.error(`❌ [mongo] Failed to persist schedule: ${e.message}`);
    }
}

async function persistScheduleEnabled(sessionId, command, enabled) {
    if (!mongoDb) return;
    try {
        await mongoDb.collection('mc_schedules').updateOne(
            { session_id: sessionId, command },
            { $set: { enabled, updated_at: new Date() } }
        );
    } catch (e) {
        console.error(`❌ [mongo] Failed to update schedule: ${e.message}`);
    }
}

async function deletePersistedSchedule(sessionId, command) {
    if (!mongoDb) return;
    try {
        await mongoDb.collection('mc_schedules').deleteOne({ session_id: sessionId, command });
    } catch (e) {
        console.error(`❌ [mongo] Failed to delete schedule: ${e.message}`);
    }
}

// ─── Auth-cache backup/restore ──────────────────────────────────────────────
// prismarine-auth's cached Microsoft/Xbox tokens live only as files under
// each session's cache subdirectory. That's fine on a host with a persistent
// disk, but if you ever move hosting providers (or redeploy somewhere
// without a persistent volume) that directory comes up empty and every
// linked account needs a fresh Microsoft login. Mirroring those files into
// Mongo means they can be restored on any machine that has the MONGO_URI,
// regardless of what happens to local disk.
async function backupAuthCache(sessionId) {
    if (!mongoDb) return;
    try {
        const dir = sessionCacheDir(sessionId);
        const files = fs.readdirSync(dir);
        console.log(`[auth-cache] Backing up ${sessionId}: found ${files.length} file(s) in ${dir}${files.length ? ' -> ' + files.join(', ') : ''}`);
        for (const f of files) {
            const content = fs.readFileSync(path.join(dir, f));
            await mongoDb.collection('mc_auth_cache').updateOne(
                { session_id: sessionId, filename: f },
                { $set: { content: content.toString('base64'), updated_at: new Date() } },
                { upsert: true }
            );
        }
    } catch (e) {
        console.error(`❌ [mongo] Failed to back up auth cache for ${sessionId}: ${e.message}`);
    }
}

async function restoreAuthCache(sessionId) {
    if (!mongoDb) return false;
    try {
        const docs = await mongoDb.collection('mc_auth_cache').find({ session_id: sessionId }).toArray();
        console.log(`[auth-cache] Restoring ${sessionId}: found ${docs.length} backed-up file(s) in Mongo.`);
        const dir = sessionCacheDir(sessionId);
        for (const doc of docs) {
            const filePath = path.join(dir, doc.filename);
            // Don't clobber a still-present local file with a possibly
            // older Mongo copy — only restore what's actually missing.
            if (!fs.existsSync(filePath)) {
                fs.writeFileSync(filePath, Buffer.from(doc.content, 'base64'));
            }
        }
        if (docs.length > 0) {
            console.log(`✅ [mongo] Restored ${docs.length} auth-cache file(s) for ${sessionId}.`);
        }
        return docs.length > 0;
    } catch (e) {
        console.error(`❌ [mongo] Failed to restore auth cache for ${sessionId}: ${e.message}`);
        return false;
    }
}

async function deletePersistedAuthCache(sessionId) {
    if (!mongoDb) return;
    try {
        await mongoDb.collection('mc_auth_cache').deleteMany({ session_id: sessionId });
    } catch (e) {
        console.error(`❌ [mongo] Failed to delete auth cache for ${sessionId}: ${e.message}`);
    }
}

// ─── Auto-reconnect (restores live sessions after a crash/redeploy) ────────
function withTimeout(promise, ms, timeoutValue) {
    return Promise.race([
        promise,
        new Promise(resolve => setTimeout(() => resolve(timeoutValue), ms))
    ]);
}

async function autoReconnectAll() {
    if (!mongoDb) return;

    // Two separate concerns that used to be wrongly coupled together:
    //   1. Restoring the *Microsoft/Xbox login* (so "Account Linked" comes
    //      back after a restart) — this should happen for EVERY session
    //      that has a backed-up auth cache.
    //   2. Restoring the *live server connection* — this happens for every
    //      session that has a saved last_server. Auto-reconnect is always
    //      on (no per-session toggle), so simply having connected to a
    //      server before is enough to rejoin it automatically here too.
    // Previously (2) was gated behind an auto_reconnect flag that had to be
    // switched on manually, so a linked-but-never-toggled account never got
    // its login OR its server connection restored on restart.

    let cachedSessionIds = [];
    try {
        cachedSessionIds = await mongoDb.collection('mc_auth_cache').distinct('session_id');
    } catch (e) {
        console.error(`❌ [restore] Failed to list sessions with saved logins: ${e.message}`);
    }

    let reconnectDocs = [];
    try {
        reconnectDocs = await mongoDb.collection('mc_sessions')
            .find({ last_server: { $ne: null } })
            .toArray();
    } catch (e) {
        console.error(`❌ [auto-reconnect] Failed to query sessions: ${e.message}`);
    }
    const reconnectBySessionId = new Map(reconnectDocs.map(d => [d.session_id, d]));

    // Union of "has a saved login" and "has a server to rejoin" — the latter
    // implies the former, but a session could in theory have a last_server
    // without a cache entry (e.g. cache row cleanup), so cover both.
    const allSessionIds = Array.from(new Set([...cachedSessionIds, ...reconnectBySessionId.keys()]));

    if (allSessionIds.length === 0) {
        console.log('ℹ️  [restore] No sessions with a saved login to restore.');
        return;
    }

    // Need discord_id for each session (for sessionOwners bookkeeping) even
    // for the login-only ones, so pull those docs too.
    let sessionDocs = [];
    try {
        sessionDocs = await mongoDb.collection('mc_sessions')
            .find({ session_id: { $in: allSessionIds } })
            .toArray();
    } catch (e) {
        console.error(`❌ [restore] Failed to load session records: ${e.message}`);
    }
    const sessionDocById = new Map(sessionDocs.map(d => [d.session_id, d]));

    console.log(`🔄 [restore] Restoring login for ${allSessionIds.length} session(s)...`);
    for (const sessionId of allSessionIds) {
        const sessionDoc = sessionDocById.get(sessionId);
        if (sessionDoc) sessionOwners[sessionId] = sessionDoc.discord_id;

        try {
            // The cached Microsoft token usually silently refreshes here. If
            // it's actually expired/revoked, startLogin() will instead sit
            // waiting for someone to complete a fresh device-code flow — cap
            // how long we wait so one stale session can't stall every other
            // session's restore. The Xbox Live -> XSTS -> Minecraft services
            // chain can legitimately take a while on a cold start, so this
            // is generous rather than tight.
            const loginResult = await withTimeout(
                startLogin(sessionId),
                60000,
                { ok: false, error: 'TIMEOUT', startedWithCache: null }
            );
            if (!loginResult.ok) {
                if (loginResult.error === 'TIMEOUT') {
                    // We don't know from a bare timeout whether this session
                    // had a cache to restore from (startLogin never got to
                    // return). Check on disk/Mongo directly to tell the two
                    // very different cases apart in the log.
                    const stillHasCache = (fs.existsSync(path.join(cacheDir, sessionId)) && fs.readdirSync(path.join(cacheDir, sessionId)).length > 0)
                        || (mongoDb && await mongoDb.collection('mc_auth_cache').countDocuments({ session_id: sessionId }) > 0);
                    console.warn(stillHasCache
                        ? `⚠️  [restore] ${sessionId} has a saved login but it didn't silently refresh within 60s — Microsoft's auth service may be slow, or the saved token has actually expired. A manual login will be needed if this keeps happening.`
                        : `⚠️  [restore] ${sessionId} has no saved login cache (Mongo or disk) — a manual login is required; this is expected for a session that was never fully linked before.`);
                } else {
                    console.warn(`⚠️  [restore] Login failed for ${sessionId}: ${loginResult.error}`);
                }
                continue;
            }
            console.log(`✅ [restore] Account re-linked for ${sessionId}`);

            const reconnectDoc = reconnectBySessionId.get(sessionId);
            if (reconnectDoc) {
                const connectResult = await connectToServer(sessionId, reconnectDoc.last_server, reconnectDoc.last_version);
                if (connectResult.ok) {
                    console.log(`✅ [auto-reconnect] ${sessionId} -> ${reconnectDoc.last_server}`);
                } else {
                    console.warn(`⚠️  [auto-reconnect] Connect failed for ${sessionId}: ${connectResult.error}`);
                }
            }
        } catch (e) {
            console.error(`❌ [restore] Unexpected error for ${sessionId}: ${e.message}`);
        }
        // Stagger so we don't hammer Microsoft's auth service with a burst
        // of simultaneous logins when many sessions restore at once.
        await new Promise(r => setTimeout(r, 3000));
    }
}

// ─── Auth-gate server list ─────────────────────────────────────────────────
// Some servers (DonutSMP, by default) kick new connections and ask the
// player to authorize via a Discord bot DM before letting them in. Which
// servers do this is admin-configurable from the dashboard (Admin ->
// Auth-Gate Servers), stored in Mongo's mc_auth_gate_servers collection.
// Flask is the source of truth; this process just polls Flask's internal
// endpoint and keeps an in-memory cache so the per-connection check below
// stays synchronous and fast. Requires Node 18+ for global fetch().
const FLASK_INTERNAL_URL = process.env.FLASK_INTERNAL_URL || 'http://127.0.0.1:5000';
const AUTH_GATE_REFRESH_MS = 2 * 60 * 1000; // 2 minutes
let gatedServerDomains = [];

async function refreshGatedServerDomains() {
    try {
        const res = await fetch(`${FLASK_INTERNAL_URL}/internal/mc-auth-servers`, { signal: AbortSignal.timeout(5000) });
        const data = await res.json();
        if (Array.isArray(data.domains)) {
            gatedServerDomains = data.domains.map(d => String(d).toLowerCase());
        }
    } catch (e) {
        // Non-fatal: keep using whatever list we already had cached. Only
        // log once in a while so a persistently-down Flask doesn't spam logs.
        console.warn(`[auth-gate] Could not refresh server list from Flask: ${e.message}`);
    }
}

refreshGatedServerDomains();
setInterval(refreshGatedServerDomains, AUTH_GATE_REFRESH_MS);

function isGatedServer(server) {
    const host = String(server || '').split(':')[0].trim().toLowerCase();
    if (!host) return false;
    return gatedServerDomains.some(domain => host === domain || host.endsWith(`.${domain}`));
}

function getState(sessionId) {
    if (!states[sessionId]) {
        states[sessionId] = {
            status: 'disconnected',
            mcUsername: null,
            ownsJava: false,
            server: null,
            version: 'latest',
            error: null,
            bot: null,
            chatMessages: [],
            chatIdCounter: 0,
            requires_auth: false,
            health: 20,
            maxHealth: 20,
            hunger: 20,
            level: 0,
            xp: 0,
            position: { x: 0, y: 0, z: 0 },
            code: null,
            url: null,
            // Tracks the bot instance actually "owned" by this session right
            // now (as opposed to `bot`, which several event handlers null
            // out for other reasons). Only cleanupBot() and connectToServer()
            // ever reassign this — event handlers just read it to tell a
            // live disconnect apart from a stale event fired by an instance
            // that's already been replaced/torn down.
            activeBot: null,
            reconnectTimer: null,
            reconnectAttempts: 0,
            // The GUI panel currently open on the server (ender chest, a
            // /home menu, a shop, etc.), if any. null when nothing's open.
            openWindow: null,
            windowAutoCloseTimer: null,
            windowUpdateListener: null
        };
    }
    return states[sessionId];
}

function cleanupBot(sessionId) {
    const state = getState(sessionId);
    if (state.bot) {
        try {
            state.bot.quit();
        } catch (e) {}
        state.bot = null;
    }
    state.activeBot = null;
    if (state.reconnectTimer) {
        clearTimeout(state.reconnectTimer);
        state.reconnectTimer = null;
    }
    state.reconnectAttempts = 0;
    if (state.windowAutoCloseTimer) {
        clearTimeout(state.windowAutoCloseTimer);
        state.windowAutoCloseTimer = null;
    }
    if (state.windowUpdateListener) {
        try { state.windowUpdateListener.window.removeListener('updateSlot', state.windowUpdateListener.handler); } catch (e) {}
        state.windowUpdateListener = null;
    }
    state.openWindow = null;
    if (state.authBackupTimer) {
        clearInterval(state.authBackupTimer);
        state.authBackupTimer = null;
    }
    if (!state.mcUsername) {
        state.status = 'disconnected';
    } else {
        state.status = 'idle'; // Logged in but not connected to a server
    }
    state.server = null;
    state.requires_auth = false;
}

// ─── Live auto-reconnect (rejoins mid-session drops, not just process restarts) ─
// Auto-reconnect is always on — there's no per-session toggle. Whenever a
// connected session drops unexpectedly (kick, network blip, server restart)
// this rejoins the same server automatically. It deliberately does NOT fire
// after an explicit user action (/logout, /full-logout, or starting a fresh
// /connect elsewhere) — those go through cleanupBot(), which clears
// `state.server` and `state.activeBot` synchronously, so there's nothing
// left here to reconnect to by the time this would run.
function scheduleReconnect(sessionId) {
    const state = getState(sessionId);
    if (state.reconnectTimer) return; // already scheduled, don't stack timers
    if (!state.server) return; // nothing to rejoin (manual logout, never connected)

    const attempt = (state.reconnectAttempts || 0) + 1;
    state.reconnectAttempts = attempt;
    const delay = Math.min(5000 * Math.pow(2, attempt - 1), 60000); // 5s,10s,20s,40s,60s cap

    console.log(`🔁 [auto-reconnect] ${sessionId} disconnected from ${state.server} — retrying in ${Math.round(delay / 1000)}s (attempt ${attempt})...`);

    state.reconnectTimer = setTimeout(async () => {
        const s = getState(sessionId);
        s.reconnectTimer = null;
        if (!s.server) return; // logged out / cleared while we were waiting
        if (s.activeBot) return; // something else already reconnected it

        try {
            const result = await connectToServer(sessionId, s.server, s.version);
            if (!result.ok) {
                console.warn(`⚠️  [auto-reconnect] ${sessionId} retry failed: ${result.error}`);
                scheduleReconnect(sessionId);
            }
            // On success, connectToServer's own 'spawn' handler resets
            // reconnectAttempts back to 0.
        } catch (e) {
            console.error(`❌ [auto-reconnect] ${sessionId} retry threw: ${e.message}`);
            scheduleReconnect(sessionId);
        }
    }, delay);
}

// ─── AUTHENTICATION ──────────────────────────────────────────────────────
async function startLogin(sessionId) {
    const state = getState(sessionId);
    state.status = 'awaiting_auth';
    state.error = null;
    state.code = null;
    state.url = null;

    console.log(`[startLogin] Starting auth for ${sessionId}`);

    // If local disk lost this session's cached token (fresh host, wiped
    // volume, etc.) but we still have a Mongo-backed copy, restore it first
    // so Authflow can pick up the existing cache instead of forcing a brand
    // new device-code login.
    const hadCache = await restoreAuthCache(sessionId);
    const hadLocalCache = fs.readdirSync(sessionCacheDir(sessionId)).length > 0;
    const startedWithCache = hadCache || hadLocalCache;

    try {
        // The options object with onMsaCode callback
        const flow = new Authflow(sessionId, sessionCacheDir(sessionId), {
            authTitle: Titles.MinecraftNintendoSwitch,
            deviceType: 'Nintendo',
            flow: 'live'
        }, (codeInfo) => {
            console.log(`[startLogin] 🔑 Device code callback triggered! codeInfo:`, codeInfo);
            // Use snake_case property names (this is the 'live' flow's response shape)
            state.code = codeInfo.user_code;
            state.url = codeInfo.verification_uri;
            console.log(`[startLogin] state.code = ${state.code}, state.url = ${state.url}`);
        });

        console.log('[startLogin] Authflow created, now calling getMinecraftJavaToken()...');
        const token = await flow.getMinecraftJavaToken({ fetchProfile: true, fetchEntitlements: true });
        console.log('[startLogin] getMinecraftJavaToken() returned:', token ? 'yes' : 'null');

        if (token && token.profile && token.profile.name) {
            state.mcUsername = token.profile.name;
            console.log(`[startLogin] Profile name: ${state.mcUsername}`);
        } else {
            state.mcUsername = `Player_${sessionId.slice(-4)}`;
            console.log(`[startLogin] No profile, using fallback: ${state.mcUsername}`);
        }

        // Real Java ownership check, based on actual entitlements rather than
        // guessing from whether a username happened to resolve.
        const items = token?.entitlements?.items || [];
        state.ownsJava = items.some(i => i.name === 'game_minecraft' || i.name === 'product_minecraft');
        console.log(`[startLogin] ownsJava: ${state.ownsJava}`);

        state.status = 'idle';
        state.code = null;   // clear after login
        state.url = null;
        console.log('[startLogin] Login successful, state set to idle.');

        // Mirror whatever token files this login just wrote/refreshed into
        // Mongo, so they can be restored on any other host.
        await backupAuthCache(sessionId);

        return { ok: true, startedWithCache };
    } catch (e) {
        console.error(`[startLogin] ❌ Error during auth:`, e);
        state.status = 'error';
        state.code = null;   // don't leave a stale device code on screen
        state.url = null;
        // prismarine-auth surfaces raw upstream HTTP failures as e.g.
        // "502 Bad Gateway <html>...". These are almost always a transient
        // blip from Microsoft/Xbox's auth services, not a real problem.
        const httpFail = /^\d{3}\s/.test(e.message);
        state.error = httpFail
            ? `Microsoft's auth service had a hiccup (${e.message.slice(0, 3)}). Please try logging in again.`
            : e.message;
        return { ok: false, error: state.error, startedWithCache };
    }
}

// ─── Window/GUI auto-close ─────────────────────────────────────────────────
// Some commands (ender chest, shops, kits, custom plugin menus, etc.) open a
// server-side GUI window that the bot has no way to click through. Turns a
// mineflayer Window into a short human-readable label for the chat
// notification below (e.g. "Ender Chest" instead of "minecraft:generic_9x3").
function describeWindow(window) {
    if (!window) return 'a menu';
    if (window.title) {
        // window.title is occasionally a raw JSON chat-component string
        // rather than plain text on some servers/protocol versions — fall
        // back to the type label below if it doesn't look like real text.
        const t = String(window.title).trim();
        if (t && !t.startsWith('{') && !t.startsWith('[')) return t;
    }
    const typeLabels = {
        'minecraft:generic_9x1': 'Container',
        'minecraft:generic_9x2': 'Container',
        'minecraft:generic_9x3': 'Chest',
        'minecraft:generic_9x4': 'Container',
        'minecraft:generic_9x5': 'Container',
        'minecraft:generic_9x6': 'Large Chest',
        'minecraft:crafting': 'Crafting Table',
        'minecraft:anvil': 'Anvil',
        'minecraft:enchantment': 'Enchanting Table',
        'minecraft:furnace': 'Furnace',
        'minecraft:merchant': 'Trading',
        'minecraft:shulker_box': 'Shulker Box',
        'minecraft:hopper': 'Hopper',
        'minecraft:beacon': 'Beacon',
        'minecraft:brewing_stand': 'Brewing Stand',
        'minecraft:loom': 'Loom',
        'minecraft:stonecutter': 'Stonecutter',
        'minecraft:grindstone': 'Grindstone',
        'minecraft:smithing': 'Smithing Table',
        'minecraft:lectern': 'Lectern',
    };
    return typeLabels[window.type] || window.type || 'a menu';
}

// Pulls the server-assigned custom name (e.g. "Home 1") off an item's NBT,
// if one is set. Servers set this via the "display.Name" NBT tag — that's
// still how mineflayer surfaces it even on 1.20.5+ servers that use the
// newer data-components format internally.
// The value is a JSON text component string (e.g. '{"text":"Home 1"}' or
// '{"extra":[{"text":"Home 1"}],"text":""}'), not plain text, so it needs
// to be parsed and flattened rather than used as-is.
// Flatten a plain JSON text component (or plain string) into plain text.
// Used for the LEGACY nbt.display.Name format, where the tag's string value
// is itself a JSON-encoded text component, e.g. '{"text":"Home 1"}'.
function flattenTextComponent(component) {
    if (typeof component === 'string') return component;
    if (!component || typeof component !== 'object') return '';
    let out = component.text || '';
    if (Array.isArray(component.extra)) {
        out += component.extra.map(flattenTextComponent).join('');
    }
    return out;
}

// Flatten an NBT-ENCODED text component into plain text. CONFIRMED shape
// from live logs:
//   { type: "compound", value: {
//       text: { type: "string", value: "" },
//       extra: { type: "list", value: { type: "compound", value: [
//           { text: { type: "string", value: "Home 1" }, color: {...}, ... }
//       ] } }
//   } }
// Note: the top-level tag and the "text"/"extra" fields are wrapped in
// {type, value}, but each ITEM inside the extra list is NOT re-wrapped —
// its fields (text, color, etc.) sit directly on the object.
function parseCompoundFields(fields) {
    if (!fields) return '';
    let out = fields.text?.value || '';
    if (fields.extra) out += parseTag(fields.extra);
    return out;
}

function parseTag(tag) {
    if (!tag) return '';
    if (tag.type === 'string') return tag.value || '';
    if (tag.type === 'compound') return parseCompoundFields(tag.value);
    if (tag.type === 'list') {
        const items = tag.value?.value;
        if (Array.isArray(items)) return items.map(parseCompoundFields).join('');
    }
    return '';
}

function flattenNbtTextComponent(tag) {
    return parseTag(tag);
}

function getCustomName(item) {
    try {
        // Legacy format: NBT tag, "display.Name" holds a JSON-STRING text component.
        const nbtNameTag = item?.nbt?.value?.display?.value?.Name?.value;
        if (nbtNameTag) {
            const flat = flattenTextComponent(JSON.parse(nbtNameTag));
            console.log(`[getCustomName] ${item.name} via legacy nbt ->`, JSON.stringify(flat));
            if (flat) return flat;
        }

        // Newer format (1.20.5+ data components): item.components is an array
        // of { type, data }. The 'custom_name' entry's data is itself an
        // NBT-ENCODED text component (not a JSON string).
        if (Array.isArray(item?.components)) {
            console.log(`[getCustomName] ${item.name} component types:`, item.components.map(c => c.type));
            const customNameComponent = item.components.find(
                c => c.type === 'custom_name' || c.type === 'minecraft:custom_name'
            );
            if (customNameComponent) {
                console.log(`[getCustomName] ${item.name} RAW custom_name data (compact):`, JSON.stringify(customNameComponent.data));
                const flat = flattenNbtTextComponent(customNameComponent.data);
                console.log(`[getCustomName] ${item.name} via components ->`, JSON.stringify(flat));
                if (flat) return flat;
            }
        }

        return null;
    } catch (e) {
        console.log('[getCustomName] parse error:', e.message);
        return null;
    }
}

// Turns a live mineflayer Window into a plain-JSON snapshot the frontend can
// render and click against. Only the window's OWN slots are included (i.e.
// up to window.inventoryStart) — the player's own inventory is appended
// after that in window.slots, and isn't part of the "panel" the user asked
// to see/click.
function buildWindowSnapshot(window) {
    const end = typeof window.inventoryStart === 'number' ? window.inventoryStart : window.slots.length;
    const items = [];
    for (let i = 0; i < end; i++) {
        const item = window.slots[i];
        if (!item) continue;
        const resolvedName = getCustomName(item) || item.displayName || item.name;
        console.log(`[buildWindowSnapshot] slot ${i} name=${item.name} -> displayName="${resolvedName}"`);
        items.push({
            slot: i,
            name: item.name,
            displayName: resolvedName,
            count: item.count
        });
    }
    return {
        id: window.id,
        type: window.type,
        title: describeWindow(window),
        items,
        updatedAt: Date.now()
    };
}

// ─── BOT CREATION & SERVER CONNECTION ────────────────────────────────────
async function connectToServer(sessionId, server, version) {
    const state = getState(sessionId);
    
    if (!state.mcUsername) {
        return { ok: false, error: 'Not logged in. Please link Microsoft account first.' };
    }

    cleanupBot(sessionId); // Destroy any existing bot instance

    state.server = server;
    state.version = version || 'latest';
    state.status = 'connecting';
    state.requires_auth = false;
    state.error = null;

    try {
        const bot = mineflayer.createBot({
            host: server,
            port: 25565,
            username: sessionId, 
            // mineflayer expects an actual version string (e.g. "1.20.4") or
            // no `version` field at all to auto-negotiate with the server.
            // The literal string "latest" isn't a real protocol version.
            ...(state.version && state.version !== 'latest' ? { version: state.version } : {}),
            auth: 'microsoft',
            profilesFolder: sessionCacheDir(sessionId),
            hideErrors: false
        });
        
        state.bot = bot;
        state.activeBot = bot;

        bot.on('spawn', () => {
            state.status = 'ready';
            state.error = null;
            // A successful spawn means we're back — reset the reconnect
            // backoff so the *next* unexpected drop starts fresh at 5s
            // instead of inheriting a long delay from a previous outage.
            state.reconnectAttempts = 0;
            state.health = bot.health;
            state.maxHealth = bot.game?.maxHealth || 20;
            state.hunger = bot.food;
            state.level = bot.experience?.level || 0;
            state.xp = bot.experience?.points || 0;
            state.position = bot.entity.position;

            // mineflayer does its OWN Microsoft auth internally (via
            // `auth: 'microsoft'` + `profilesFolder: sessionCacheDir(sessionId)` above), and
            // that silently refreshes/rotates the on-disk token files —
            // completely separate from the startLogin() flow above. If we
            // only ever mirrored the cache to Mongo right after an explicit
            // login, that Mongo copy would go stale the moment a rotated
            // refresh token replaced it on disk, and a later restart would
            // restore the STALE (often already-invalidated) token instead
            // of the current one — forcing a fresh device-code login even
            // though the account was "linked" a minute earlier. Re-mirror
            // on every successful connect so Mongo always has what's
            // actually valid right now.
            backupAuthCache(sessionId).catch(() => {});

            // Also refresh the Mongo copy periodically while connected,
            // since a long-lived session can silently rotate its token
            // again hours later without ever reconnecting.
            if (state.authBackupTimer) clearInterval(state.authBackupTimer);
            state.authBackupTimer = setInterval(() => {
                backupAuthCache(sessionId).catch(() => {});
            }, 15 * 60 * 1000);
        });

        bot.on('health', () => {
            state.health = bot.health;
            state.hunger = bot.food;
            if (state.status === 'ready' && bot.health <= 0) {
                state.status = 'connecting'; // Respawning
                setTimeout(() => {
                    if (state.bot) state.status = 'ready';
                }, 2000);
            }
        });

        bot.on('experience', () => {
            state.level = bot.experience?.level || 0;
            state.xp = bot.experience?.points || 0;
        });

        bot.on('move', () => {
            if (bot.entity) {
                state.position = bot.entity.position;
            }
        });

        // Some servers kick new/unrecognized connections and ask the player
        // to authorize via a Discord bot DM before letting them in. Which
        // servers do this is admin-configurable (see isGatedServer above) —
        // other servers' kick/chat messages that happen to mention "Discord"
        // or "verify" for unrelated reasons should NOT trip this flow, or
        // players on those servers would get stuck waiting on an
        // "I Authorized" button that does nothing.
        const isGated = isGatedServer(server);

        bot.on('messagestr', (message) => {
            if (isGated && message.includes('Discord') && (message.includes('authorize') || message.includes('verify') || message.includes('link'))) {
                state.status = 'awaiting_discord_auth';
                state.requires_auth = true;
            }
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: message,
                type: 'system'
            });
        });

        bot.on('chat', (username, message) => {
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: `<${username}> ${message}`,
                type: 'chat'
            });
        });

        // Fires for ANY window the server opens on the bot — whether it was
        // triggered by a /chat command (e.g. a plugin's /enderchest or
        // /home opening a GUI), a right-click-style server action, or
        // anything else. Rather than the bot getting silently stuck in it,
        // snapshot its contents so the dashboard can show + click them, the
        // same way clicking that slot in-game would.
        bot.on('windowOpen', (window) => {
            if (state.activeBot !== bot) return; // stale event from an already-replaced instance
            state.openWindow = buildWindowSnapshot(window);
            console.log(`[window] ${sessionId} opened "${state.openWindow.title}" (${state.openWindow.items.length} item(s)).`);

            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: `📦 A panel opened: ${state.openWindow.title}`,
                type: 'system'
            });

            // Some menus (large chests, shops with lots of items) stream
            // their contents in over a couple of follow-up packets rather
            // than being complete the instant this event fires — keep the
            // snapshot live as slots fill in or change from clicks.
            if (state.windowUpdateListener) {
                try { state.windowUpdateListener.window.removeListener('updateSlot', state.windowUpdateListener.handler); } catch (e) {}
            }
            const onUpdateSlot = () => {
                if (state.activeBot === bot && bot.currentWindow === window) {
                    state.openWindow = buildWindowSnapshot(window);
                }
            };
            window.on('updateSlot', onUpdateSlot);
            state.windowUpdateListener = { window, handler: onUpdateSlot };

            // Safety net: if it's left open and untouched, close it after a
            // while so the bot doesn't sit stuck in a menu forever if the
            // user never comes back to it.
            if (state.windowAutoCloseTimer) clearTimeout(state.windowAutoCloseTimer);
            state.windowAutoCloseTimer = setTimeout(() => {
                if (state.activeBot === bot && bot.currentWindow === window) {
                    console.log(`[window] ${sessionId} panel left open 3 min unattended — closing automatically.`);
                    try { bot.closeWindow(window); } catch (e) {}
                }
            }, 3 * 60 * 1000);
        });

        bot.on('windowClose', (window) => {
            if (state.activeBot !== bot) return; // stale event from an already-replaced instance
            if (state.windowUpdateListener && state.windowUpdateListener.window === window) {
                try { window.removeListener('updateSlot', state.windowUpdateListener.handler); } catch (e) {}
                state.windowUpdateListener = null;
            }
            if (state.windowAutoCloseTimer) {
                clearTimeout(state.windowAutoCloseTimer);
                state.windowAutoCloseTimer = null;
            }
            state.openWindow = null;
        });

        bot.on('kicked', (reason) => {
            if (state.activeBot !== bot) return; // stale event from an already-replaced instance
            const reasonStr = reason.toString();
            state.error = `Kicked: ${reasonStr}`;

            if (isGated && (reasonStr.includes('Discord') || reasonStr.includes('authorize') || reasonStr.includes('verify'))) {
                // Auto-reconnect is deliberately skipped here — the server
                // is holding the connection until the player authorizes via
                // Discord DM, which only a human can do. Reconnecting on a
                // loop wouldn't get past that gate.
                state.status = 'awaiting_discord_auth';
                state.requires_auth = true;
            } else {
                state.status = 'idle';
                state.bot = null;
                // 'end' fires right after this and is where the actual
                // reconnect gets scheduled, so nothing further to do here.
            }
        });

        bot.on('error', (err) => {
            if (state.activeBot !== bot) return; // stale event from an already-replaced instance
            state.status = 'error';
            state.error = err.message;
            state.bot = null;
        });

        bot.on('end', () => {
            if (state.activeBot !== bot) return; // stale event from an already-replaced instance
            const wasAwaitingAuth = state.status === 'awaiting_discord_auth';
            if (!wasAwaitingAuth && state.status !== 'error') {
                state.status = 'idle';
            }
            state.bot = null;
            state.activeBot = null;

            // Always-on auto-reconnect: any unexpected drop (kick, network
            // blip, server restart) rejoins the same server automatically.
            // Skipped only when the server is holding on a Discord-auth gate
            // that needs a human, or when there's no saved server to rejoin
            // (e.g. this followed an explicit /logout, which already
            // cleared state.server via cleanupBot()).
            if (!wasAwaitingAuth) {
                scheduleReconnect(sessionId);
            }
        });

        return { ok: true, status: 'connecting' };

    } catch (e) {
        state.status = 'error';
        state.error = e.message;
        return { ok: false, error: e.message };
    }
}

// Disconnect and log back into the saved server (used by the stats scraper when
// a /stats GUI or /ping keeps failing). Returns as soon as the new connection
// has been started; the caller waits for status 'ready'.
async function rejoinServer(sessionId) {
    const state = getState(sessionId);
    const server = state.server;
    const version = state.version;
    if (!server) return { ok: false, error: 'No saved server to rejoin.' };
    console.log(`🔁 [rejoin] ${sessionId} disconnecting and rejoining ${server}...`);
    return connectToServer(sessionId, server, version); // cleanupBot() inside quits the old bot
}

// ─── SCHEDULED COMMANDS ──────────────────────────────────────────────────
function loadSchedulesForUser(sessionId) {
    if (!schedules[sessionId]) schedules[sessionId] = [];
}

function checkSchedules() {
    const now = Date.now();
    for (const sessionId in schedules) {
        const userSchedules = schedules[sessionId];
        const state = getState(sessionId);

        userSchedules.forEach(s => {
            if (s.enabled && state.status === 'ready' && state.bot) {
                if (now >= s.nextRun) {
                    try {
                        state.bot.chat(s.command);
                        s.nextRun = now + s.interval;
                    } catch (e) {}
                }
            }
        });
    }
}
setInterval(checkSchedules, 5000);

// ─── API ROUTES ──────────────────────────────────────────────────────────

app.get('/status/:sessionId', (req, res) => {
    const state = getState(req.params.sessionId);
    const response = {
        status: state.status,
        mcUsername: state.mcUsername,
        ownsJava: state.ownsJava,
        server: state.server,
        version: state.version,
        error: state.error,
        requires_auth: state.requires_auth,
        health: state.health,
        maxHealth: state.maxHealth,
        hunger: state.hunger,
        xpLevel: state.level,
        xpProgress: state.xp,
        position: state.position,
        ping: state.bot?.player?.ping || 0,
        code: state.code || null,
        url: state.url || null
    };
    console.log(`[status] Returning for ${req.params.sessionId}: status=${response.status}, code=${response.code}, url=${response.url}`);
    res.json(response);
});

app.post('/start-login/:sessionId', (req, res) => {
    const sessionId = req.params.sessionId;
    const { discordId } = req.body || {};
    if (discordId) sessionOwners[sessionId] = discordId;

    // Don't await this: startLogin() blocks until the user actually completes
    // the Microsoft device-code flow (which can take minutes), but the caller
    // (the Flask dashboard) only waits ~10s for a response. Kick the flow off
    // in the background and respond immediately; the frontend polls
    // /status/:sessionId to pick up the code/url once onMsaCode fires, and
    // to see the final success/error state once the flow resolves.
    startLogin(sessionId).catch(e => {
        console.error(`[start-login] Unhandled error for ${sessionId}:`, e);
        const state = getState(sessionId);
        state.status = 'error';
        state.error = e.message;
    });

    res.json({ ok: true, status: 'started' });
});

app.post('/connect/:sessionId', async (req, res) => {
    const { server, version, discordId } = req.body;
    if (!server) return res.status(400).json({ ok: false, error: 'Missing server IP' });
    if (discordId) sessionOwners[req.params.sessionId] = discordId;

    const result = await connectToServer(req.params.sessionId, server, version);
    res.json(result);
});

// ─── DEBUG / ADMIN: which sessions are live for a given Discord user ─────
// Mongo's mc_sessions collection is the source of truth for which sessions
// *exist*; this just reports which of those are currently live/connected in
// this Node process, for debugging and admin visibility.
app.get('/sessions/:discordId', (req, res) => {
    const discordId = req.params.discordId;
    const live = Object.entries(sessionOwners)
        .filter(([, owner]) => owner === discordId)
        .map(([sessionId]) => {
            const state = getState(sessionId);
            return {
                sessionId,
                status: state.status,
                mcUsername: state.mcUsername,
                server: state.server,
            };
        });
    res.json({ ok: true, sessions: live });
});

app.post('/reconnect/:sessionId', async (req, res) => {
    const state = getState(req.params.sessionId);
    if (state.server) {
        const result = await connectToServer(req.params.sessionId, state.server, state.version);
        res.json(result);
    } else {
        res.json({ ok: false, error: 'No previous server to reconnect to.' });
    }
});

app.post('/logout/:sessionId', (req, res) => {
    cleanupBot(req.params.sessionId);
    res.json({ ok: true });
});

app.post('/full-logout/:sessionId', async (req, res) => {
    cleanupBot(req.params.sessionId);
    states[req.params.sessionId] = {
        status: 'disconnected',
        mcUsername: null,
        ownsJava: false,
        server: null,
        version: 'latest',
        error: null,
        bot: null,
        chatMessages: [],
        chatIdCounter: 0,
        code: null,
        url: null
    };
    // This route means "wipe the saved token" (see app.py's /mc-full-logout
    // docstring), so remove the local cache files AND the Mongo-backed copy
    // — otherwise a later restart would just restore the very token this
    // call was meant to throw away.
    try {
        fs.rmSync(path.join(cacheDir, req.params.sessionId), { recursive: true, force: true });
    } catch (e) {}
    await deletePersistedAuthCache(req.params.sessionId);
    try {
        if (mongoDb) await mongoDb.collection('mc_schedules').deleteMany({ session_id: req.params.sessionId });
    } catch (e) {
        console.error(`❌ [mongo] Failed to delete schedules for ${req.params.sessionId}: ${e.message}`);
    }
    delete schedules[req.params.sessionId];
    res.json({ ok: true });
});

app.get('/chat/:sessionId', (req, res) => {
    const state = getState(req.params.sessionId);
    const after = parseInt(req.query.after) || 0;
    const messages = state.chatMessages.filter(m => m.id > after);
    res.json({ ok: true, messages });
});

app.post('/chat/:sessionId', (req, res) => {
    const state = getState(req.params.sessionId);
    const { message } = req.body;
    if (state.bot && state.status === 'ready') {
        try {
            state.bot.chat(message);
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: `> ${message}`,
                type: 'sent'
            });
            res.json({ ok: true });
        } catch (e) {
            res.json({ ok: false, error: e.message });
        }
    } else {
        res.json({ ok: false, error: 'Bot is not connected.' });
    }
});

// ─── WINDOW/PANEL ROUTES ─────────────────────────────────────────────────
// Lets the dashboard show whatever GUI panel the server just opened on the
// bot (ender chest, /home menu, shops, etc.) and click items in it directly
// — sends the exact same click packet the real client sends, so it behaves
// identically to physically clicking that slot in-game.
app.get('/window/:sessionId', (req, res) => {
    const state = getState(req.params.sessionId);
    res.json({ ok: true, window: state.openWindow || null });
});

app.post('/window/:sessionId/click', async (req, res) => {
    const sessionId = req.params.sessionId;
    const state = getState(sessionId);
    const { slot, button } = req.body || {};

    if (!state.bot) {
        return res.json({ ok: false, error: 'Bot is not connected.' });
    }
    if (typeof slot !== 'number' || !Number.isInteger(slot) || slot < 0) {
        return res.json({ ok: false, error: 'Missing or invalid slot.' });
    }
    // Make sure the panel the dashboard is looking at is still the one
    // that's actually open — it could have closed (or a new one opened)
    // between the last poll and this click.
    const currentWindow = state.bot.currentWindow;
    if (!currentWindow || !state.openWindow || currentWindow.id !== state.openWindow.id) {
        return res.json({ ok: false, error: 'That panel is no longer open.' });
    }

    const mouseButton = button === 'right' ? 1 : 0; // 0 = left click, 1 = right click
    try {
        await state.bot.clickWindow(slot, mouseButton, 0);
        // Refresh the snapshot right after the click so a fast-following
        // poll sees the result (e.g. a stack count changing) rather than
        // waiting on the next 'updateSlot' event.
        if (state.bot.currentWindow === currentWindow) {
            state.openWindow = buildWindowSnapshot(currentWindow);
        }
        res.json({ ok: true });
    } catch (e) {
        res.json({ ok: false, error: e.message });
    }
});

app.post('/window/:sessionId/close', (req, res) => {
    const state = getState(req.params.sessionId);
    if (state.bot && state.bot.currentWindow) {
        try {
            state.bot.closeWindow(state.bot.currentWindow);
        } catch (e) {
            return res.json({ ok: false, error: e.message });
        }
    }
    state.openWindow = null;
    res.json({ ok: true });
});

// ─── SCHEDULE ROUTES ─────────────────────────────────────────────────────
app.get('/schedule/:sessionId', (req, res) => {
    loadSchedulesForUser(req.params.sessionId);
    res.json({ ok: true, commands: schedules[req.params.sessionId] });
});

app.post('/schedule/:sessionId', async (req, res) => {
    loadSchedulesForUser(req.params.sessionId);
    const { command, interval } = req.body;

    schedules[req.params.sessionId] = schedules[req.params.sessionId].filter(s => s.command !== command);

    const newSchedule = {
        command,
        interval,
        enabled: true,
        nextRun: Date.now() + interval
    };

    schedules[req.params.sessionId].push(newSchedule);
    await persistSchedule(req.params.sessionId, newSchedule);
    res.json({ ok: true });
});

app.post('/schedule/disable/:sessionId', async (req, res) => {
    loadSchedulesForUser(req.params.sessionId);
    const { command } = req.body;
    const sched = schedules[req.params.sessionId].find(s => s.command === command);
    if (sched) {
        sched.enabled = false;
        await persistScheduleEnabled(req.params.sessionId, command, false);
        res.json({ ok: true });
    } else {
        res.json({ ok: false, error: 'Schedule not found' });
    }
});

app.post('/schedule/delete/:sessionId', async (req, res) => {
    loadSchedulesForUser(req.params.sessionId);
    const { command } = req.body;
    schedules[req.params.sessionId] = schedules[req.params.sessionId].filter(s => s.command !== command);
    await deletePersistedSchedule(req.params.sessionId, command);
    res.json({ ok: true });
});

// ─── START SERVER ────────────────────────────────────────────────────────
// ─── STATS FEATURES ──────────────────────────────────────────────────────
// Wrapped in try/catch so a missing file can never stop the bot from booting.
try {
    require('./debug-stats')(app, getState, states);
} catch (e) {
    console.warn(`⚠️ debug-stats not loaded: ${e.message}`);
}
try {
    require('./stats-scraper')(app, getState, rejoinServer);
} catch (e) {
    console.warn(`⚠️ stats-scraper not loaded: ${e.message}`);
}
try {
    require('./follow-watch')(app, getState);
} catch (e) {
    console.warn(`⚠️ follow-watch not loaded: ${e.message}`);
}

// Start listening immediately so health checks / Flask's readiness probe
// don't wait on Mongo or on every session's Microsoft login to finish —
// those happen in the background and update state as they complete.
app.listen(PORT, () => {
    console.log(`✅ MC Bot Service running on port ${PORT}`);
});

(async () => {
    mongoDb = await connectMongo();
    if (mongoDb) {
        await loadAllSchedulesFromDb();
        await autoReconnectAll();
    }
})();