// stats-scraper.js
//
// Looks up a player's DonutSMP stats by driving a real, logged-in Minecraft
// account: types `/stats <user>`, waits for the stats chest GUI, reads the
// values out of each item's lore, closes the GUI, returns JSON.
//
// Layout (verified from a real dump of the server's GUI):
//   emerald -> money        amethyst_shard -> shards     diamond_sword -> kills
//   skeleton_skull -> deaths  clock -> playtime          stone -> blocks placed
//   cobblestone -> blocks broken   zombie_head -> mobs killed
//   Value = first lore line, e.g. "420K", "35", "45d 19h ".
//   NOTE: the GUI has NO shop spent / shop earned / online status.
//
// Route:  GET|POST /stats-lookup/:sessionId?user=<ign>
// Hook up in index.js (before app.listen):
//   try { require('./stats-scraper')(app, getState); }
//   catch (e) { console.warn('stats-scraper not loaded: ' + e.message); }
//
// Env knobs (all optional):
//   STATS_CACHE_SECONDS=45   STATS_MIN_GAP_MS=1500   STATS_OPEN_TIMEOUT_MS=6000

const CACHE_TTL_MS = (Number(process.env.STATS_CACHE_SECONDS) || 45) * 1000;
const MIN_GAP_MS = Number(process.env.STATS_MIN_GAP_MS) || 1500;
const OPEN_TIMEOUT_MS = Number(process.env.STATS_OPEN_TIMEOUT_MS) || 6000;
const FILL_TIMEOUT_MS = 2500;
const PING_TIMEOUT_MS = Number(process.env.STATS_PING_TIMEOUT_MS) || 4000;
const MAX_QUEUE = 12;
const LOOKUP_ATTEMPTS = Number(process.env.STATS_LOOKUP_ATTEMPTS) || 3;
const RETRY_GAP_MS = 1500;
const RETRY_CODES = new Set(['no_gui', 'incomplete', 'title_mismatch']);

const ITEM_TO_STAT = {
    emerald: 'money',
    amethyst_shard: 'shards',
    diamond_sword: 'kills',
    skeleton_skull: 'deaths',
    clock: 'playtime',
    stone: 'blocks_placed',
    cobblestone: 'blocks_broken',
    zombie_head: 'mobs_killed',
};
const LABEL_TO_STAT = {
    'money': 'money',
    'shards': 'shards',
    'kills': 'kills',
    'deaths': 'deaths',
    'playtime': 'playtime',
    'blocks placed': 'blocks_placed',
    'blocks broken': 'blocks_broken',
    'mobs killed': 'mobs_killed',
};
const EXPECTED_STATS = Object.keys(LABEL_TO_STAT).length; // 8

// ── dashboard console logging ────────────────────────────────────────────
// Everything this file does on the account shows up in the session's console
// (same feed as the chat): commands we send appear as "> /command", and short
// notes about what happened appear as system lines prefixed with 🔧.
function pushConsole(state, text, type) {
    try {
        if (state && Array.isArray(state.chatMessages)) {
            state.chatMessages.push({ id: ++state.chatIdCounter, text, type: type || 'system' });
            if (state.chatMessages.length > 2000) state.chatMessages.splice(0, state.chatMessages.length - 1500); // keep memory bounded
        }
    } catch (e) { /* never let logging break a lookup */ }
}
function note(state, text) { pushConsole(state, `🔧 ${text}`, 'system'); }
function sendLogged(state, command) {
    pushConsole(state, `> ${command}`, 'sent'); // logged BEFORE sending so replies appear after it
    state.bot.chat(command);
}

const hooks = require('./rejoin-hooks');

// ── rejoin: if a lookup keeps failing, disconnect and log back in, then retry ──
const REJOIN_ENABLED = process.env.STATS_REJOIN_ENABLED !== '0';
const REJOIN_COOLDOWN_MS = Number(process.env.STATS_REJOIN_COOLDOWN_MS) || 45000; // never rejoin more often than this
const REJOIN_TIMEOUT_MS = Number(process.env.STATS_REJOIN_TIMEOUT_MS) || 60000;   // max wait for 'ready' after rejoining
const REJOIN_SETTLE_MS = Number(process.env.STATS_REJOIN_SETTLE_MS) || 4000;      // let the server finish loading us in
let rejoinFn = null;                 // set by registerStatsScraper (from index.js)
const sidByState = new WeakMap();    // state object -> sessionId
const lastRejoin = new Map();        // sessionId -> timestamp

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ── text helpers ─────────────────────────────────────────────────────────

// The server writes labels/names in small-caps and uses Cyrillic lookalikes.
// Fold them to plain ASCII so comparisons work.
const FOLD = {
    'ᴀ': 'a', 'ʙ': 'b', 'ᴄ': 'c', 'ᴅ': 'd', 'ᴇ': 'e', 'ꜰ': 'f', 'ɢ': 'g', 'ʜ': 'h',
    'ɪ': 'i', 'ᴊ': 'j', 'ᴋ': 'k', 'ʟ': 'l', 'ᴍ': 'm', 'ɴ': 'n', 'ᴏ': 'o', 'ᴘ': 'p',
    'ǫ': 'q', 'ʀ': 'r', 'ѕ': 's', 'ꜱ': 's', 'ᴛ': 't', 'ᴜ': 'u', 'ᴠ': 'v', 'ᴡ': 'w', 'х': 'x',
    'ʏ': 'y', 'ᴢ': 'z',
    // Cyrillic lookalikes
    'а': 'a', 'е': 'e', 'о': 'o', 'р': 'p', 'с': 'c', 'у': 'y', 'і': 'i', 'ј': 'j',
};
function normalizeText(s) {
    return String(s || '')
        .normalize('NFKC')
        .replace(/[\u0400-\u04FF\u1D00-\u1DBF\u0250-\u02AF\uA700-\uA7FF\u01EB]/g, ch => FOLD[ch] || ch)
        .toLowerCase()
        .replace(/§[0-9a-fk-or]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
}

// prismarine-nbt { type, value } wrappers -> plain JS
function simplifyNbt(node) {
    if (node === null || node === undefined || typeof node !== 'object') return node;
    if (Array.isArray(node)) return node.map(simplifyNbt);
    const keys = Object.keys(node);
    if (keys.includes('type') && keys.includes('value') && keys.length <= 3) {
        const t = node.type, v = node.value;
        if (t === 'list' && v && Array.isArray(v.value)) return v.value.map(simplifyNbt);
        return simplifyNbt(v);
    }
    const out = {};
    for (const k of keys) out[k] = simplifyNbt(node[k]);
    return out;
}

// Chat component (string / JSON string / object / array) -> plain text
function flattenText(x) {
    if (x == null) return '';
    if (typeof x === 'string') {
        const s = x.trim();
        if (s.startsWith('{') || s.startsWith('[')) {
            try { return flattenText(JSON.parse(s)); } catch (e) { return x; }
        }
        return x;
    }
    if (Array.isArray(x)) return x.map(flattenText).join('');
    if (typeof x === 'object') {
        let s = typeof x.text === 'string' ? x.text : '';
        if (Array.isArray(x.extra)) s += x.extra.map(flattenText).join('');
        return s;
    }
    return String(x);
}

function getComponent(item, type) {
    if (!Array.isArray(item?.components)) return null;
    const c = item.components.find(c => c.type === type || c.type === `minecraft:${type}`);
    return c ? simplifyNbt(c.data) : null;
}

function getLabel(item) {
    try {
        const cn = getComponent(item, 'custom_name');
        if (cn) return flattenText(cn);
        const legacy = item?.nbt?.value?.display?.value?.Name?.value;
        if (legacy) return flattenText(legacy);
    } catch (e) { /* ignore */ }
    return item.displayName || item.name || '';
}

function getLoreLines(item) {
    const lines = [];
    try {
        const lore = getComponent(item, 'lore');
        if (lore) (Array.isArray(lore) ? lore : [lore]).forEach(l => lines.push(flattenText(l)));
        const legacy = item?.nbt?.value?.display?.value?.Lore?.value?.value;
        if (!lines.length && Array.isArray(legacy)) legacy.forEach(l => lines.push(flattenText(l)));
    } catch (e) { /* ignore */ }
    return lines.map(l => l.replace(/§[0-9a-fk-or]/gi, '').trim()).filter(Boolean);
}

// ── value parsing ────────────────────────────────────────────────────────

// "420K" -> 420000, "2.8K" -> 2800, "1.5M" -> 1500000, "35" -> 35
function parseAbbrev(raw) {
    const m = String(raw).replace(/[,\s$]/g, '').match(/^(-?\d+(?:\.\d+)?)([KMBT])?$/i);
    if (!m) return null;
    const mult = { K: 1e3, M: 1e6, B: 1e9, T: 1e12 }[(m[2] || '').toUpperCase()] || 1;
    return Math.round(parseFloat(m[1]) * mult);
}

// "45d 19h " -> milliseconds (accepts d/h/m/s parts in any combination)
function parsePlaytimeMs(raw) {
    const re = /(\d+)\s*([dhms])/gi;
    let m, total = 0, found = false;
    while ((m = re.exec(String(raw))) !== null) {
        found = true;
        total += Number(m[1]) * { d: 86400e3, h: 3600e3, m: 60e3, s: 1e3 }[m[2].toLowerCase()];
    }
    return found ? total : null;
}

function parseStatValue(stat, raw) {
    return stat === 'playtime' ? parsePlaytimeMs(raw) : parseAbbrev(raw);
}

// ── reading the window ───────────────────────────────────────────────────

function readWindow(window) {
    const end = typeof window.inventoryStart === 'number' ? window.inventoryStart : window.slots.length;
    const stats = {};
    const unknown = [];
    for (let i = 0; i < end; i++) {
        const item = window.slots[i];
        if (!item) continue;
        const label = normalizeText(getLabel(item));
        const stat = LABEL_TO_STAT[label] || ITEM_TO_STAT[item.name];
        const lore = getLoreLines(item);
        if (!stat) { unknown.push({ slot: i, item: item.name, label, lore }); continue; }
        const raw = lore[0];
        if (raw === undefined) continue; // not filled in yet
        stats[stat] = { raw, value: parseStatValue(stat, raw), slot: i };
    }
    return { stats, unknown };
}

function waitForWindow(bot, timeoutMs) {
    return new Promise((resolve, reject) => {
        const onOpen = (w) => { clearTimeout(timer); resolve(w); };
        const timer = setTimeout(() => {
            bot.removeListener('windowOpen', onOpen);
            reject(new Error(`No window opened within ${timeoutMs}ms`));
        }, timeoutMs);
        bot.once('windowOpen', onOpen);
    });
}

// Resolves {window} when the stats GUI opens, or {notFound:true} when the
// server answers "User does not exist" in chat (that's exactly what it prints).
function waitForWindowOrNotFound(bot, timeoutMs) {
    return new Promise((resolve, reject) => {
        const cleanup = () => {
            clearTimeout(timer);
            bot.removeListener('windowOpen', onOpen);
            bot.removeListener('messagestr', onMsg);
        };
        const onOpen = (w) => { cleanup(); resolve({ window: w }); };
        const onMsg = (m) => {
            if (normalizeText(m).includes('user does not exist')) { cleanup(); resolve({ notFound: true }); }
        };
        const timer = setTimeout(() => {
            cleanup();
            reject(new Error(`No window opened within ${timeoutMs}ms`));
        }, timeoutMs);
        bot.on('windowOpen', onOpen);
        bot.on('messagestr', onMsg);
    });
}

// Runs /ping <user>. "<name>'s ping is 29ms" -> true (online),
// "This user is offline or does not exist" -> false (offline; existence was
// already confirmed by /stats). No reply in time -> null (unknown).
function checkOnline(state, user, timeoutMs) {
    const bot = state.bot;
    return new Promise((resolve) => {
        const want = normalizeText(user).replace(/^\./, '');
        const seen = [];
        const done = (v, why) => {
            clearTimeout(timer);
            bot.removeListener('messagestr', onMsg);
            if (v === null) {
                console.warn(`[stats-scraper] /ping ${user}: ${why}. Chat seen during ping: ${JSON.stringify(seen)}`);
            } else {
                console.log(`[stats-scraper] /ping ${user}: ${v ? 'ONLINE' : 'OFFLINE'} (${why})`);
            }
            resolve(v);
        };
        const onMsg = (m) => {
            seen.push(String(m).slice(0, 120));
            const t = normalizeText(m);
            if (/ping is \d+\s*ms/.test(t) && t.includes(want)) return done(true, `reply: ${String(m).slice(0, 80)}`);
            if (t.includes('offline or does not exist')) return done(false, `reply: ${String(m).slice(0, 80)}`);
        };
        const timer = setTimeout(() => done(null, `no recognised reply within ${timeoutMs}ms (command may be rate-limited/blocked or reply format differs)`), timeoutMs);
        bot.on('messagestr', onMsg);
        try {
            sendLogged(state, `/ping ${user}`);
        } catch (e) {
            done(null, `could not send /ping: ${e.message}`);
        }
    });
}

function safeClose(bot, window) {
    try { if (window) bot.closeWindow(window); } catch (e) { /* ignore */ }
}

async function rejoinSession(state, why) {
    const sid = sidByState.get(state);
    if (!REJOIN_ENABLED || !rejoinFn || !sid) return false;
    const sinceLast = Date.now() - (lastRejoin.get(sid) || 0);
    if (sinceLast < REJOIN_COOLDOWN_MS) {
        note(state, `${why} - not rejoining (last rejoin was ${Math.round(sinceLast / 1000)}s ago)`);
        return false;
    }
    lastRejoin.set(sid, Date.now());
    note(state, `${why} - disconnecting and rejoining the server`);
    console.warn(`[stats-scraper] ${sid}: ${why} - rejoining the server`);
    let r;
    try { r = await rejoinFn(sid); }
    catch (e) { r = { ok: false, error: e.message }; }
    if (!r || !r.ok) {
        note(state, `rejoin failed to start: ${r && r.error}`);
        return false;
    }
    const deadline = Date.now() + REJOIN_TIMEOUT_MS;
    await sleep(500);
    while (Date.now() < deadline) {
        if (state.bot && state.status === 'ready') break;
        if (state.status === 'awaiting_discord_auth' || state.status === 'error') break;
        await sleep(500);
    }
    if (!(state.bot && state.status === 'ready')) {
        note(state, `rejoin did not finish (status: ${state.status})`);
        console.warn(`[stats-scraper] ${sid}: rejoin did not finish (status: ${state.status})`);
        return false;
    }
    await sleep(REJOIN_SETTLE_MS);
    note(state, 'rejoined the server, ready');
    console.log(`[stats-scraper] ${sid}: rejoined, ready`);
    await hooks.run(sid, state);
    return true;
}

async function lookupOnce(state, user) {
    // A respawn briefly flips status to 'connecting' — give it a moment.
    for (let i = 0; i < 10 && state.bot && state.status === 'connecting'; i++) await sleep(500);
    if (!state.bot || state.status !== 'ready') {
        return { ok: false, error_code: 'not_ready', error: `Bot is not ready (status: ${state.status}).` };
    }
    const bot = state.bot;

    if (bot.currentWindow) {
        safeClose(bot, bot.currentWindow);
        await sleep(300);
    }

    const chatLines = [];
    const onMsg = (m) => chatLines.push(m);
    bot.on('messagestr', onMsg);

    let window = null;
    try {
        try {
            const opened = waitForWindowOrNotFound(bot, OPEN_TIMEOUT_MS); // listen BEFORE sending
            sendLogged(state, `/stats ${user}`);
            const res = await opened;
            if (res.notFound) {
                note(state, `/stats ${user}: user does not exist`);
                console.log(`[stats-scraper] /stats ${user}: User does not exist`);
                return { ok: false, error_code: 'not_found', error: 'User does not exist' };
            }
            window = res.window;
        } catch (e) {
            // The GUI may still pop open a moment after we gave up — close it
            // whenever it shows up so it never gets left hanging.
            const late = (w) => safeClose(bot, w);
            bot.once('windowOpen', late);
            setTimeout(() => bot.removeListener('windowOpen', late), 5000);
            return { ok: false, error_code: 'no_gui', error: e.message, chatLines };
        }

        // GUIs fill in over several packets: poll until all stats are present.
        const deadline = Date.now() + FILL_TIMEOUT_MS;
        let parsed;
        for (;;) {
            parsed = readWindow(window);
            if (Object.keys(parsed.stats).length >= EXPECTED_STATS || Date.now() > deadline) break;
            await sleep(150);
        }

        const titlePlain = flattenText(simplifyNbt(window.title));

        // Make sure this window is really about the player we asked for.
        if (titlePlain && !normalizeText(titlePlain).includes(normalizeText(user))) {
            return { ok: false, error_code: 'title_mismatch', error: `Window "${titlePlain}" is not for ${user}.` };
        }
        if (Object.keys(parsed.stats).length < 4) {
            return { ok: false, error_code: 'incomplete', error: 'GUI opened but stats were missing.', unknown: parsed.unknown, chatLines };
        }
        // The player exists (the GUI opened). Close it, then /ping to see if they're online.
        safeClose(bot, window);
        window = null;
        await sleep(400);
        let online = await checkOnline(state, user, PING_TIMEOUT_MS);
        if (online === null) {
            // No usable /ping reply: disconnect, log back in and run /ping again.
            if (await rejoinSession(state, `/ping ${user} got no reply`)) {
                online = await checkOnline(state, user, PING_TIMEOUT_MS);
            }
        }
        note(state, `/stats ${user}: stats read OK, ${online === true ? 'ONLINE' : online === false ? 'OFFLINE' : 'online status unknown (no /ping reply)'}`);
        return { ok: true, user, titlePlain, stats: parsed.stats, unknown: parsed.unknown, online };
    } finally {
        // Runs on success, on every error path, and if anything above throws.
        bot.removeListener('messagestr', onMsg);
        safeClose(bot, window);
    }
}

// Retries transient failures (menu didn't open / didn't fully load) before
// reporting an error, so a single laggy attempt never reaches the user.
async function runAttempts(state, user, n) {
    let result;
    for (let attempt = 1; attempt <= n; attempt++) {
        result = await lookupOnce(state, user);
        if (result.ok || !RETRY_CODES.has(result.error_code)) return result;
        console.warn(`[stats-scraper] /stats ${user}: ${result.error_code} (attempt ${attempt}/${n}) - ${result.error}. Chat seen: ${JSON.stringify((result.chatLines || []).slice(-5))}`);
        note(state, `/stats ${user}: ${result.error_code} (attempt ${attempt}/${n})${attempt < n ? ', retrying' : ', giving up for now'}`);
        if (attempt < n) await sleep(RETRY_GAP_MS);
    }
    return result;
}

async function lookupViaGui(state, user) {
    let result = await runAttempts(state, user, LOOKUP_ATTEMPTS);
    if (!result.ok && RETRY_CODES.has(result.error_code)) {
        // The menu never opened properly: disconnect, log back in, then try again.
        if (await rejoinSession(state, `/stats ${user} kept failing (${result.error_code})`)) {
            result = await runAttempts(state, user, 2);
        }
    }
    return result;
}

// ── balance lookup (/bal <user>) ─────────────────────────────────────────
// Used by the Discord /pay watcher. Sends `/bal <user>`, reads the chat reply
// and pulls the money amount out of it. The raw chat lines are returned and
// logged so the format can be checked if parsing ever misses.
const BAL_TIMEOUT_MS = Number(process.env.STATS_BAL_TIMEOUT_MS) || 5000;

function parseBalanceLine(line, want, selfMode) {
    const t = normalizeText(line);
    if (!t) return null;
    // Plain `/bal` replies exactly like: "You have $ 44,256,211"
    const mentions = selfMode
        ? /\byou have\s*\$/.test(t)
        : (t.includes(want) || /\b(balance|bal|money)\b/.test(t));
    if (!mentions) return null;
    // "$1,234.5K", "1.2M", "420 K", "$1000"
    const re = /\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmbt])?(?![a-z0-9])/gi;
    const MULT = { k: 1e3, m: 1e6, b: 1e9, t: 1e12 };
    let m, best = null;
    while ((m = re.exec(t)) !== null) {
        const v = parseAbbrev(m[1] + (m[2] || ''));
        if (v === null) continue;
        // last number on the line is the balance. `res` is how coarse the shown
        // value is: "1.2M" -> 100000, "420K" -> 1000, "1,500" -> 1.
        const decimals = (m[1].split('.')[1] || '').length;
        best = { value: v, res: (MULT[(m[2] || '').toLowerCase()] || 1) * Math.pow(10, -decimals) };
    }
    return best;
}

async function lookupBalanceOnce(state, user) {
    for (let i = 0; i < 10 && state.bot && state.status === 'connecting'; i++) await sleep(500);
    if (!state.bot || state.status !== 'ready') {
        return { ok: false, error_code: 'not_ready', error: `Bot is not ready (status: ${state.status}).` };
    }
    const bot = state.bot;
    // user === null -> plain `/bal` (the logged-in account's own, full balance)
    const label = user || '(self)';
    const want = user
        ? normalizeText(user).replace(/^\./, '')
        : (normalizeText(state.mcUsername || '').replace(/^\./, '') || '\u0000');
    const lines = [];
    return new Promise((resolve) => {
        const finish = (r) => {
            clearTimeout(timer);
            bot.removeListener('messagestr', onMsg);
            note(state, r.ok ? `/bal ${label}: read balance ${r.balance.toLocaleString('en-US')}` : `/bal ${label}: failed (${r.error_code}: ${r.error})`);
            if (!r.ok) console.warn(`[stats-scraper] /bal ${label}: ${r.error_code} - ${r.error}. Chat seen: ${JSON.stringify(lines)}`);
            else console.log(`[stats-scraper] /bal ${label}: ${r.balance} (from: ${r.raw})`);
            resolve(r);
        };
        const onMsg = (m) => {
            const s = String(m);
            lines.push(s.slice(0, 120));
            const t = normalizeText(s);
            if (user && t.includes('does not exist') && !t.includes('offline')) {
                return finish({ ok: false, error_code: 'not_found', error: 'User does not exist' });
            }
            const v = parseBalanceLine(s, want, !user);
            if (v !== null) finish({ ok: true, user: user || null, account: state.mcUsername || null, balance: v.value, resolution: v.res, raw: s.slice(0, 120) });
        };
        const timer = setTimeout(() => finish({
            ok: false, error_code: 'no_reply',
            error: `No recognisable /bal reply within ${BAL_TIMEOUT_MS}ms`,
        }), BAL_TIMEOUT_MS);
        bot.on('messagestr', onMsg);
        try { sendLogged(state, user ? `/bal ${user}` : '/bal'); }
        catch (e) { finish({ ok: false, error_code: 'error', error: `could not send /bal: ${e.message}` }); }
    });
}

async function lookupBalance(state, user) {
    let r;
    for (let attempt = 1; attempt <= 3; attempt++) {
        r = await lookupBalanceOnce(state, user);
        if (r.ok || r.error_code !== 'no_reply') return r;
        if (attempt < 3) await sleep(RETRY_GAP_MS);
    }
    return r;
}

// ── queue + cache ────────────────────────────────────────────────────────
// One account can only hold one GUI open at a time, so lookups run strictly
// one after another per session, with a minimum gap between commands.

const chains = new Map();
const pending = new Map();
const lastRun = new Map();
const cache = new Map();
const inflight = new Map();

function enqueue(sid, fn) {
    const n = pending.get(sid) || 0;
    if (n >= MAX_QUEUE) {
        return Promise.resolve({ ok: false, error_code: 'busy', error: 'Too many stats lookups queued.' });
    }
    pending.set(sid, n + 1);
    const prev = chains.get(sid) || Promise.resolve();
    const run = prev.then(async () => {
        const wait = (lastRun.get(sid) || 0) + MIN_GAP_MS - Date.now();
        if (wait > 0) await sleep(wait);
        try { return await fn(); }
        catch (e) { return { ok: false, error_code: 'error', error: e.message }; }
        finally { lastRun.set(sid, Date.now()); }
    }).finally(() => pending.set(sid, (pending.get(sid) || 1) - 1));
    chains.set(sid, run.catch(() => {}));
    return run;
}

async function getStats(sid, user, state) {
    const key = `${sid}:${normalizeText(user)}`;
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < CACHE_TTL_MS) return { ...hit.data, cached: true };
    if (inflight.has(key)) return inflight.get(key); // same player already being fetched

    const p = enqueue(sid, () => lookupViaGui(state, user)).then(result => {
        if (result.ok && result.online !== null && result.online !== undefined) cache.set(key, { at: Date.now(), data: result });
        return { ...result, cached: false };
    }).finally(() => inflight.delete(key));
    inflight.set(key, p);
    return p;
}

module.exports = function registerStatsScraper(app, getState, rejoin) {
    rejoinFn = typeof rejoin === 'function' ? rejoin : null;
    const rawGetState = getState;
    getState = (sid) => { const s = rawGetState(sid); sidByState.set(s, sid); return s; };
    async function handler(req, res) {
        const sid = req.params.sessionId;
        const user = String((req.method === 'POST' ? (req.body || {}).user : req.query.user) || '').trim();
        if (!/^\.?[A-Za-z0-9_]{1,24}$/.test(user)) {
            return res.json({ ok: false, error_code: 'bad_user', error: 'Missing or invalid user.' });
        }
        res.json(await getStats(sid, user, getState(sid)));
    }
    async function balHandler(req, res) {
        const sid = req.params.sessionId;
        const user = String((req.method === 'POST' ? (req.body || {}).user : req.query.user) || '').trim();
        if (!/^\.?[A-Za-z0-9_]{1,24}$/.test(user)) {
            return res.json({ ok: false, error_code: 'bad_user', error: 'Missing or invalid user.' });
        }
        // never cached: the /pay watcher needs the live number each time
        res.json(await enqueue(sid, () => lookupBalance(getState(sid), user)));
    }
    async function balSelfHandler(req, res) {
        const sid = req.params.sessionId;
        res.json(await enqueue(sid, () => lookupBalance(getState(sid), null)));
    }
    app.get('/bal-self/:sessionId', balSelfHandler);
    app.get('/bal-lookup/:sessionId', balHandler);
    app.post('/bal-lookup/:sessionId', balHandler);
    app.get('/stats-lookup/:sessionId', handler);
    app.post('/stats-lookup/:sessionId', handler);
    console.log('📊 [stats-scraper] registered /stats-lookup/:sessionId');
};

// exported for tests
module.exports._internals = { rejoinSession, sidByState, pushConsole, note, sendLogged, parseBalanceLine, parseAbbrev, parsePlaytimeMs, normalizeText, readWindow, lookupViaGui, getStats };
