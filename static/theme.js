/* Motion helpers: staggered reveal, cursor spotlight on cards, button ripple. Pure progressive enhancement. */
(function () {
    var still = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

    // 1) staggered entrance
    var sel = '.admin-top, .admin-nav, .saved-banner, .error-banner, .banner, .sc-card, .server-row, .stat-card, .plan-card, .bcard, .mbox, .session-card, .profile-card, .toggle-card, .chan-row, .login-box, .admin-box, tbody tr';
    var i = 0;
    document.querySelectorAll(sel).forEach(function (el) {
        if (still) return;
        el.style.setProperty('--i', Math.min(i++, 22));
        el.classList.add('reveal');
    });

    // 2) spotlight follows the cursor on cards
    var cards = '.sc-card, .server-row, .stat-card, .plan-card, .bcard, .mbox, .session-card, .profile-card, .toggle-card, .admin-box, .login-box';
    document.addEventListener('pointermove', function (e) {
        var c = e.target.closest && e.target.closest(cards);
        if (!c) return;
        var r = c.getBoundingClientRect();
        c.style.setProperty('--mx', (e.clientX - r.left) + 'px');
        c.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }, { passive: true });

    // 3) ripple on buttons
    document.addEventListener('pointerdown', function (e) {
        var b = e.target.closest && e.target.closest('button, .btn, .sc-btn, .save-btn, .admin-submit, .btn-new, .btn-manage');
        if (!b || b.disabled || still) return;
        var r = b.getBoundingClientRect(), d = Math.max(r.width, r.height);
        var s = document.createElement('span');
        s.className = 'ripple';
        s.style.cssText = 'width:' + d + 'px;height:' + d + 'px;left:' + (e.clientX - r.left - d / 2) + 'px;top:' + (e.clientY - r.top - d / 2) + 'px';
        b.appendChild(s);
        setTimeout(function () { s.remove(); }, 700);
    }, { passive: true });
})();
