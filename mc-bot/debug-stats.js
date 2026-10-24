// debug-stats.js
//
// PURPOSE: figure out WHERE donutsmp.net stores the numbers in the /stats GUI.
// The menu shows only titles ("Money", "Shards", ...) — the values are
// probably in the item's lore (tooltip), but could also be in the item name,
// custom NBT, the window title, or a chat message. This dumps ALL of it.
//
// It does NOT change any existing behaviour. It only adds two routes:
//
//   GET  /debug/sessions                      -> list sessions + status
//   GET  /debug/stats/:sessionId?user=Steve   -> run /stats Steve, dump GUI
//   POST /debug/stats/:sessionId  {user, settleMs, timeoutMs, command}
//
// Output is returned as JSON AND saved to mc-bot/debug/stats-<user>-<ts>.json
//
// Hook it up in index.js with two lines (before app.listen):
//   const registerStatsDebug = require('./debug-stats');
//   registerStatsDebug(app, getState, states);

const fs = require('fs');
const path = require('path');

const DEBUG_DIR = path.join(__dirname, 'debug');

// ── helpers ──────────────────────────────────────────────────────────────

// JSON.stringify that survives BigInt / Buffer / circular refs.
function safeJson(value, space) {
    // Track only the current ancestor chain so a shared (non-circular)
    // object that appears twice is still serialized both times.
    const stack = [];
    return JSON.stringify(value, function (k, v) {
        if (typeof v === 'bigint') return v.toString();
        if (Buffer.isBuffer(v)) return `<Buffer ${v.length} bytes>`;
        if (v && typeof v === 'object') {
            while (stack.length && stack[stack.length - 1] !== this) stack.pop();
            if (stack.includes(v)) return '<circular>';
            stack.push(v);
        }
        return v;
    }, space);
}

// Collapses prismarine-nbt's { type, value } wrappers into plain JS so the
// dump is readable. Handles compound / list / primitives.
function simplifyNbt(node) {
    if (node === null || node === undefined) return node;
    if (typeof node !== 'object') return node;
    if (Array.isArray(node)) return node.map(simplifyNbt);

    const keys = Object.keys(node);
    // { type: 'list', value: { type: 'compound', value: [ ... ] } }
    if (keys.includes('type') && keys.includes('value') && keys.length <= 3) {
        const t = node.type, v = node.value;
        if (t === 'list' && v && Array.isArray(v.value)) return v.value.map(simplifyNbt);
        if (t === 'compound' && v && typeof v === 'object') return simplifyNbt(v);
        return simplifyNbt(v);
    }
    const out = {};
    for (const k of keys) out[k] = simplifyNbt(node[k]);
    return out;
}

// Recursively collect every string with its path — this is how we find
// "Money" / "$1,234" wherever they're hiding.
function collectStrings(node, prefix, out) {
    if (node === null || node === undefined) return out;
    if (typeof node === 'string') {
        out.push({ path: prefix, text: node });
    } else if (Array.isArray(node)) {
        node.forEach((n, i) => collectStrings(n, `${prefix}[${i}]`, out));
    } else if (typeof node === 'object') {
        for (const k of Object.keys(node)) collectStrings(node[k], prefix ? `${prefix}.${k}` : k, out);
    }
    return out;
}

// Chat-component JSON string or plain text -> plain text (best effort).
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
        if (x.translate && !s) s = String(x.translate);
        if (Array.isArray(x.extra)) s += x.extra.map(flattenText).join('');
        return s;
    }
    return String(x);
}

// Best-effort lore decoding from either item format.
function extractLore(item) {
    const lines = [];
    try {
        // Legacy: nbt.display.Lore = list of JSON-string text components
        const lore = item?.nbt?.value?.display?.value?.Lore?.value?.value;
        if (Array.isArray(lore)) lore.forEach(l => lines.push(flattenText(l)));
    } catch (e) { /* ignore */ }
    try {
        // 1.20.5+: components [{type:'lore', data:...}]
        if (Array.isArray(item?.components)) {
            const c = item.components.find(c => c.type === 'lore' || c.type === 'minecraft:lore');
            if (c) {
                const simple = simplifyNbt(c.data);
                const arr = Array.isArray(simple) ? simple : [simple];
                arr.forEach(l => lines.push(flattenText(typeof l === 'string' ? l : l)));
            }
        }
    } catch (e) { /* ignore */ }
    return lines;
}

function dumpItem(slot, item) {
    const simpleNbt = simplifyNbt(item.nbt);
    const simpleComponents = Array.isArray(item.components)
        ? item.components.map(c => ({ type: c.type, data: simplifyNbt(c.data) }))
        : null;

    const strings = [];
    collectStrings(simpleNbt, 'nbt', strings);
    collectStrings(simpleComponents, 'components', strings);

    const rawComponents = item.components ? safeJson(item.components) : null;

    return {
        slot,
        name: item.name,
        displayName: item.displayName,
        count: item.count,
        decodedLore: extractLore(item),     // <- the thing we most hope is filled
        allStrings: strings,                 // every string anywhere in the item
        nbt: simpleNbt ?? null,
        components: simpleComponents,
        rawComponents: rawComponents && rawComponents.length > 6000
            ? rawComponents.slice(0, 6000) + '…<truncated>'
            : rawComponents
    };
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ── the actual debug run ─────────────────────────────────────────────────

async function runStatsDebug(state, user, opts = {}) {
    const settleMs = Math.max(0, Math.min(Number(opts.settleMs) || 1500, 10000));
    const timeoutMs = Math.max(1000, Math.min(Number(opts.timeoutMs) || 8000, 30000));
    const command = String(opts.command || `/stats ${user}`).slice(0, 100);

    if (!state.bot || state.status !== 'ready') {
        return { ok: false, error: `Bot is not ready (status: ${state.status}). Connect it to donutsmp.net first.` };
    }
    const bot = state.bot;

    // Start from a clean slate: close whatever panel is already open.
    if (bot.currentWindow) {
        try { bot.closeWindow(bot.currentWindow); } catch (e) { /* ignore */ }
        await sleep(300);
    }

    // Collect every chat/system line that arrives while we wait — some
    // servers print stats to chat instead of (or as well as) the GUI.
    const chatLines = [];
    const onMsg = (msg) => chatLines.push({ t: Date.now() - startedAt, text: msg });
    const startedAt = Date.now();
    bot.on('messagestr', onMsg);

    let window;
    try {
        const opened = new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                bot.removeListener('windowOpen', onOpen);
                reject(new Error(`No window opened within ${timeoutMs}ms`));
            }, timeoutMs);
            function onOpen(w) { clearTimeout(timer); resolve(w); }
            bot.once('windowOpen', onOpen);
        });

        bot.chat(command);
        window = await opened;
        // GUIs often fill in over several packets — let them settle.
        await sleep(settleMs);
    } catch (e) {
        bot.removeListener('messagestr', onMsg);
        return {
            ok: false,
            error: e.message,
            note: 'No GUI opened. Check chatLines — the server may have replied in chat instead.',
            command,
            chatLines
        };
    }
    bot.removeListener('messagestr', onMsg);

    const end = typeof window.inventoryStart === 'number' ? window.inventoryStart : window.slots.length;
    const items = [];
    const emptySlots = [];
    for (let i = 0; i < end; i++) {
        const it = window.slots[i];
        if (!it) { emptySlots.push(i); continue; }
        try { items.push(dumpItem(i, it)); }
        catch (e) { items.push({ slot: i, name: it.name, error: `dump failed: ${e.message}` }); }
    }

    const result = {
        ok: true,
        command,
        window: {
            id: window.id,
            type: window.type,
            titleRaw: window.title,
            titlePlain: flattenText(simplifyNbt(window.title)),
            inventoryStart: window.inventoryStart,
            totalSlots: window.slots.length,
            panelSlotCount: end
        },
        itemCount: items.length,
        emptySlots,
        items,
        chatLines,
        // Quick human summary: slot | name | first lore lines
        summary: items.map(i => ({
            slot: i.slot,
            item: i.name,
            title: i.displayName,
            lore: i.decodedLore
        }))
    };

    try { bot.closeWindow(window); } catch (e) { /* ignore */ }
    return result;
}

// ── route registration ───────────────────────────────────────────────────

module.exports = function registerStatsDebug(app, getState, states) {
    app.get('/debug/sessions', (req, res) => {
        const list = Object.keys(states || {}).map(id => ({
            sessionId: id,
            status: states[id].status,
            username: states[id].mcUsername,
            server: states[id].server
        }));
        res.json({ ok: true, sessions: list });
    });

    async function handler(req, res) {
        const sessionId = req.params.sessionId;
        const body = req.method === 'POST' ? (req.body || {}) : req.query;
        const user = String(body.user || '').trim();

        // Minecraft names are 3-16 chars of [A-Za-z0-9_]; Bedrock names start with '.'
        if (!/^\.?[A-Za-z0-9_]{1,24}$/.test(user)) {
            return res.json({ ok: false, error: 'Missing or invalid ?user=<ign>.' });
        }

        let result;
        try {
            result = await runStatsDebug(getState(sessionId), user, body);
        } catch (e) {
            result = { ok: false, error: `Unexpected error: ${e.message}` };
        }

        try {
            fs.mkdirSync(DEBUG_DIR, { recursive: true });
            const file = path.join(DEBUG_DIR, `stats-${user}-${Date.now()}.json`);
            fs.writeFileSync(file, safeJson(result, 2));
            result.savedTo = file;
            console.log(`[debug-stats] dump saved to ${file}`);
        } catch (e) {
            console.error(`[debug-stats] could not save dump: ${e.message}`);
        }

        res.type('application/json').send(safeJson(result, 2));
    }

    app.get('/debug/stats/:sessionId', handler);
    app.post('/debug/stats/:sessionId', handler);

    console.log('🔧 [debug-stats] debug routes registered (/debug/sessions, /debug/stats/:sessionId)');
};
