// rejoin-hooks.js
// Small registry so features can restore their state after the stats scraper
// disconnects and rejoins the server (the bot object is replaced on rejoin).
const hooks = [];
module.exports = {
    add(fn) { hooks.push(fn); },
    async run(sessionId, state) {
        for (const h of hooks) {
            try { await h(sessionId, state); }
            catch (e) { console.warn(`[rejoin-hooks] hook failed: ${e.message}`); }
        }
    },
};
