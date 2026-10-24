// follow-watch.js
//
// Backs the Discord /pay command. The logged-in account runs `/follow <player>`,
// after which the server prints that player's transactions in chat, in one of
// these two shapes (amounts are abbreviated: 1500470 shows as "1.5m"):
//
//   Wasx_ paid wiacek71 $ 5
//   wiacek71 was paid $ 5 by Wasx_
//
// This file records those lines while a watch is active and hands them to the
// Python side. Lines are only accepted when the WHOLE line is the transaction
// (anchored at the start), so a player typing "Wasx_ paid ..." in public chat
// (which shows up with their name/prefix in front) can't fake one.
//
// Routes:
//   GET /follow-start/:sessionId?receiver=<ign>&seconds=330
//   GET /follow-events/:sessionId?since=<id>
//   GET /follow-stop/:sessionId

// The logged-in account. The server shows its own transactions as "You paid X $ N"
// and "X paid you $ N", so "you" in a transaction line means this ign.
const OWN_IGN = '0xVoidX';
const hooks = require('./rejoin-hooks');
const MAX_EVENTS = 300;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Everything done on the account is mirrored into the session's dashboard console:
// commands as "> /command", notes as 🔧 system lines.
function pushConsole(state, text, type) {
    try {
        if (state && Array.isArray(state.chatMessages)) {
            state.chatMessages.push({ id: ++state.chatIdCounter, text, type: type || 'system' });
            if (state.chatMessages.length > 2000) state.chatMessages.splice(0, state.chatMessages.length - 1500); // keep memory bounded
        }
    } catch (e) { /* never let logging break a watch */ }
}
const note = (state, text) => pushConsole(state, `🔧 ${text}`, 'system');
function sendLogged(state, command) {
    pushConsole(state, `> ${command}`, 'sent');
    state.bot.chat(command);
}

const NAME = '[A-Za-z0-9_.]{1,24}';
const AMT = '\\$\\s*(\\d[\\d,]*(?:\\.\\d+)?)\\s*([kmbt])?(?![A-Za-z0-9])';
const PRE = '^[^A-Za-z0-9_.]*';
const RE_A = new RegExp(`${PRE}(${NAME}) paid (${NAME}) ${AMT}`, 'i');            // X paid Y $ N
const RE_B = new RegExp(`${PRE}(${NAME}) (?:was|were) paid ${AMT}\\s+by\\s+(${NAME})`, 'i'); // Y was paid $ N by X
const MULT = { k: 1e3, m: 1e6, b: 1e9, t: 1e12 };

function clean(line) {
    return String(line || '')
        .replace(/§[0-9a-fk-or]/gi, '')
        .replace(/^\s*(?:\[[^\]]{1,24}\]\s*)+/, '')   // "[10:43:06 PM] " timestamp prefixes
        .trim();
}

function amountFrom(numStr, suffix) {
    const num = parseFloat(String(numStr).replace(/,/g, ''));
    if (!isFinite(num)) return null;
    const decimals = (String(numStr).split('.')[1] || '').length;
    const mult = MULT[(suffix || '').toLowerCase()] || 1;
    // no K/M/B suffix = the server printed the full number, so it's exact (res 0)
    const res = suffix ? mult * Math.pow(10, -decimals) : 0;
    return { value: num * mult, res, shown: `${numStr}${suffix || ''}` };
}

const me = (n) => (/^you$/i.test(n) ? OWN_IGN : n);   // "You paid ..." / "... paid you"

// -> { payer, receiver, value, res, shown, raw } or null
function parseTransaction(line) {
    const text = clean(line);
    let m = RE_A.exec(text);
    if (m) {
        const a = amountFrom(m[3], m[4]);
        if (a) return { payer: me(m[1]), receiver: me(m[2]), ...a, raw: text.slice(0, 160) };
    }
    m = RE_B.exec(text);
    if (m) {
        const a = amountFrom(m[2], m[3]);
        if (a) return { payer: me(m[4]), receiver: me(m[1]), ...a, raw: text.slice(0, 160) };
    }
    return null;
}

// The three replies /follow gives (exactly as the server prints them):
//   "You followed .Creeping18"                 -> now following
//   "Already following"                        -> was already following
//   "No player named .Creeping18l was found"   -> that player doesn't exist
function classifyFollowReply(line) {
    const t = clean(line).replace(/^[^A-Za-z0-9_.]*/, '');
    if (/^no player named .+ was found/i.test(t)) return 'not_found';
    if (/^already following/i.test(t)) return 'already';
    if (/^you followed\b/i.test(t)) return 'followed';
    return null;
}

const norm = (n) => String(n || '').toLowerCase().replace(/^\./, '');
const follows = new Map(); // sessionId -> { receiver, bot, listener, events, nextId, timer }

function stopFollow(sid, state, why) {
    const f = follows.get(sid);
    if (!f) return false;
    clearTimeout(f.timer);
    try { f.bot.removeListener('messagestr', f.listener); } catch (e) { /* ignore */ }
    follows.delete(sid);
    console.log(`[follow-watch] ${sid}: stopped watching ${f.receiver} (${why}); ${f.events.length} event(s) seen.`);
    note(state, `/pay: stopped watching ${f.receiver} (${why}), ${f.events.length} transaction line(s) seen`);
    return true;
}

// After the account disconnects and rejoins (stats scraper), the bot object and
// the server-side follow are both gone: re-attach the chat listener and follow again.
hooks.add(async (sid, state) => {
    const f = follows.get(sid);
    if (!f || !state.bot) return;
    try { f.bot.removeListener('messagestr', f.listener); } catch (e) { /* ignore */ }
    f.bot = state.bot;
    state.bot.on('messagestr', f.listener);
    if (norm(f.receiver) !== norm(OWN_IGN)) {
        note(state, `/pay: rejoined - following ${f.receiver} again`);
        sendLogged(state, `/follow ${f.receiver}`);
    }
});

module.exports = function registerFollowWatch(app, getState) {
    app.get('/follow-start/:sessionId', async (req, res) => {
        const sid = req.params.sessionId;
        const receiver = String(req.query.receiver || '').trim();
        const seconds = Math.min(Math.max(Number(req.query.seconds) || 330, 30), 900);
        if (!/^\.?[A-Za-z0-9_]{1,24}$/.test(receiver)) {
            return res.json({ ok: false, error_code: 'bad_user', error: 'Missing or invalid receiver.' });
        }
        const state = getState(sid);
        for (let i = 0; i < 10 && state.bot && state.status === 'connecting'; i++) await sleep(500);
        if (!state.bot || state.status !== 'ready') {
            return res.json({ ok: false, error_code: 'not_ready', error: `Bot is not ready (status: ${state.status}).` });
        }
        const bot = state.bot;

        const existing = follows.get(sid);
        if (existing && existing.bot === bot) {
            if (existing.receiver.toLowerCase() !== receiver.toLowerCase()) {
                return res.json({ ok: false, error_code: 'busy', error: `Already following ${existing.receiver}.`, receiver: existing.receiver });
            }
            // same receiver: just extend the watch, no second /follow
            clearTimeout(existing.timer);
            existing.timer = setTimeout(() => stopFollow(sid, getState(sid), 'timeout'), seconds * 1000);
            return res.json({ ok: true, receiver: existing.receiver, last_id: existing.nextId - 1, reused: true, chat: [] });
        }
        if (existing) stopFollow(sid, state, 'stale bot instance');

        const f = { receiver, bot, events: [], nextId: 1, timer: null, listener: null };
        const seenChat = [];
        let reply = null;
        f.listener = (m) => {
            if (seenChat.length < 12) seenChat.push(String(m).slice(0, 120));
            if (!reply) reply = classifyFollowReply(m);
            const tx = parseTransaction(m);
            if (!tx) return;
            f.events.push({ id: f.nextId++, at: Date.now(), ...tx });
            if (f.events.length > MAX_EVENTS) f.events.shift();
            console.log(`[follow-watch] ${sid}: ${tx.payer} -> ${tx.receiver} ${tx.shown} (${tx.raw})`);
            note(state, `/pay: caught transaction: ${tx.payer} -> ${tx.receiver} ${tx.shown}`);
        };
        follows.set(sid, f);
        bot.on('messagestr', f.listener);

        if (norm(receiver) === norm(OWN_IGN)) {
            // Our own account can't follow itself. Its transactions show up as
            // "X paid you $ N" / "You paid X $ N", so just listen to chat.
            note(state, `/pay: ${receiver} is the logged-in account - watching chat for "paid you" lines (no /follow needed)`);
            f.timer = setTimeout(() => stopFollow(sid, getState(sid), 'timeout'), seconds * 1000);
            return res.json({ ok: true, receiver, last_id: 0, reused: false, status: 'own_account', chat: [] });
        }

        note(state, `/pay: following ${receiver} for up to ${seconds}s`);
        try { sendLogged(state, `/follow ${receiver}`); }
        catch (e) {
            stopFollow(sid, state, 'send failed');
            return res.json({ ok: false, error_code: 'error', error: `Could not send /follow: ${e.message}` });
        }
        // wait (up to 4s) for the server's reply to /follow
        for (let i = 0; i < 40 && !reply; i++) await sleep(100);
        note(state, `/follow ${receiver}: ${reply === 'followed' ? 'now following' : reply === 'already' ? 'already following' : reply === 'not_found' ? 'player not found' : 'no recognised reply'}`);
        console.log(`[follow-watch] ${sid}: /follow ${receiver} -> ${reply || 'no recognised reply'}. Chat right after: ${JSON.stringify(seenChat)}`);

        if (reply === 'not_found') {
            stopFollow(sid, state, 'player not found');
            return res.json({ ok: false, error_code: 'not_found', error: `No player named ${receiver} was found.`, receiver });
        }
        f.timer = setTimeout(() => stopFollow(sid, getState(sid), 'timeout'), seconds * 1000);
        // "You followed X" and "Already following" both mean we're following, so carry on.
        res.json({ ok: true, receiver, last_id: 0, reused: false, status: reply || 'unknown', chat: seenChat });
    });

    app.get('/follow-events/:sessionId', (req, res) => {
        const f = follows.get(req.params.sessionId);
        if (!f) return res.json({ ok: true, active: false, events: [] });
        const since = Number(req.query.since) || 0;
        res.json({ ok: true, active: true, receiver: f.receiver, events: f.events.filter(e => e.id > since) });
    });

    // ?receiver=<ign> (optional): only stop if the account is still following THAT player. A late/duplicate
    // stop from a finished payment can then never cancel the follow a newer payment just started.
    app.get('/follow-stop/:sessionId', (req, res) => {
        const sid = req.params.sessionId;
        const want = String(req.query.receiver || '').trim();
        const f = follows.get(sid);
        if (want && f && norm(f.receiver) !== norm(want)) {
            return res.json({ ok: true, stopped: false, skipped: 'following someone else', receiver: f.receiver });
        }
        res.json({ ok: true, stopped: stopFollow(sid, getState(sid), 'requested') });
    });

    console.log('👀 [follow-watch] registered /follow-start, /follow-events, /follow-stop');
};

module.exports._internals = { pushConsole, sendLogged, parseTransaction, clean, classifyFollowReply };
