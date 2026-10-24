"""Data for the admin panel's per-server Build Dashboard tab.

The money numbers come from cogs.builds.recap_data (the same function the /bm recap panel uses), so the
dashboard and the Discord panel always agree. Everything else is read straight from the build collections.
"""
from datetime import datetime, timedelta, timezone

STAGES = {          # key -> (label, badge colour class)
    "schematic": ("📎 Waiting for schematic", "grey"),
    "quote": ("💬 Waiting for quote", "amber"),
    "payment": ("💳 Waiting for payment", "amber"),
    "claim": ("📥 Paid, waiting for builder", "blue"),
    "building": ("🔨 Being built", "green"),
    "confirm": ("📨 Waiting for delivery confirm", "blue"),
    "review": ("✍️ Customer writing review", "blue"),
}


def _naive(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def _when(dt):
    dt = _naive(dt)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"


def _age(dt):
    dt = _naive(dt)
    if not dt:
        return "—"
    secs = max(0, int((datetime.utcnow() - dt).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _sum(db, col, q, field):
    r = list(db[col].aggregate([{"$match": q}, {"$group": {"_id": None, "sum": {"$sum": f"${field}"}}}]))
    return int(r[0]["sum"]) if r else 0


def build_dashboard(db, guild_id, resolve_name) -> dict:
    from cogs import builds as B      # lazy: the admin panel runs in the same process as the bot

    gid = str(guild_id)
    cfg = B._get_settings(db, gid)
    d = B.recap_data(db, gid, cfg)

    # ---- names: what the bot already stored first, Discord lookups only for the rest -------------------------
    known: dict = {}
    for p in db["build_payments"].find({"guild_id": gid}, {"opener_id": 1, "opener_name": 1, "claimed_by": 1,
                                                          "claimed_by_name": 1, "channel_id": 1, "channel_name": 1}):
        if p.get("opener_id") and p.get("opener_name") and p["opener_name"] != p["opener_id"]:
            known.setdefault(str(p["opener_id"]), p["opener_name"])
        if p.get("claimed_by") and p.get("claimed_by_name"):
            known.setdefault(str(p["claimed_by"]), p["claimed_by_name"])
    chan_names = {str(p["channel_id"]): p["channel_name"] for p in
                  db["build_payments"].find({"guild_id": gid, "channel_name": {"$ne": None}},
                                            {"channel_id": 1, "channel_name": 1}) if p.get("channel_id")}
    ledger = list(db["builder_ledger"].find({"guild_id": gid}).sort("created_at", -1))
    for r in ledger:
        if r.get("builder_id") and r.get("builder_name"):
            known.setdefault(str(r["builder_id"]), r["builder_name"])

    def name(uid):
        uid = str(uid or "")
        if not uid:
            return "—"
        if uid not in known:
            known[uid] = resolve_name(uid) or uid
        return known[uid]

    # ---- open tickets (every stage) --------------------------------------------------------------------------
    tickets = list(db["build_tickets"].find({**B._OPEN_BASE, "guild_id": gid}).sort("created_at", 1).limit(400))
    chans = [str(t.get("channel_id")) for t in tickets]
    paid_by = B._paid_by_channel(db, gid, chans)
    quote_counts = {r["_id"]: int(r["n"]) for r in db["build_quotes"].aggregate([
        {"$match": {"channel_id": {"$in": chans}}}, {"$group": {"_id": "$channel_id", "n": {"$sum": 1}}}])} if chans else {}

    def stage(t):
        if t.get("kind") == "custom" and t.get("awaiting_file"):
            return "schematic"
        if t.get("kind") == "custom" and not t.get("quote_id"):
            return "quote"
        if not t.get("paid"):
            return "payment"
        if not t.get("claimed_by"):
            return "claim"
        fs = t.get("finish_status")
        return "confirm" if fs == "awaiting_confirm" else ("review" if fs == "reviewing" else "building")

    open_rows, funnel = [], {k: 0 for k in STAGES}
    for t in tickets:
        st = stage(t)
        funnel[st] += 1
        b = B.ticket_build(db, t)
        paid, price = B._ticket_paid_price(db, t, paid_by)
        ch = str(t.get("channel_id"))
        note = ""
        if st == "quote":
            n = quote_counts.get(ch, 0)
            note = f"{n} quote{'s' if n != 1 else ''} received" if n else "no quotes yet"
        open_rows.append({
            "stage": st, "stage_label": STAGES[st][0], "color": STAGES[st][1],
            "build": (b or {}).get("name") or ("Custom build" if t.get("kind") == "custom" else "Build"),
            "kind": t.get("kind") or "build", "customer": name(t.get("opener_id")), "ign": t.get("ign") or "—",
            "builder": name(t["claimed_by"]) if t.get("claimed_by") else "—",
            "price": price, "paid": paid, "missing": max(0, price - paid) if st not in ("schematic", "quote") else 0,
            "channel_id": ch, "channel": chan_names.get(ch) or ch, "opened": _when(t.get("created_at")),
            "age": _age(t.get("created_at")), "note": note,
        })

    # ---- finished builds whose builder still has to be paid -----------------------------------------------------
    left_rows = []
    for t in db["build_tickets"].find({"guild_id": gid, "finished": True,
                                       "payout_status": {"$in": list(B.PAYOUT_OPEN)}}).sort("finished_at", 1).limit(300):
        st = t.get("payout_status")
        left_rows.append({
            "builder": name(t.get("claimed_by")), "customer": name(t.get("opener_id")),
            "build": t.get("payout_build_name") or "Build", "amount": int(t.get("payout_amount") or 0),
            "status": {"awaiting_ign": "⚠️ builder has no IGN", "tracking": "⏳ being paid"}.get(st, "⏳ waiting"),
            "finished": _when(t.get("finished_at")), "age": _age(t.get("finished_at")),
            "channel_id": str(t.get("channel_id")),
        })

    # ---- completed builds (ledger): the fee split per build -------------------------------------------------
    completed = []
    for r in ledger[:300]:
        worth, got = int(r.get("worth") or 0), int(r.get("received") or 0)
        completed.append({
            "when": _when(r.get("created_at")), "builder": name(r.get("builder_id")), "build": r.get("build_name") or "Build",
            "worth": worth, "builder_cut": got, "fee": max(0, worth - got), "owner_share": int(r.get("owner_share") or 0),
            "rating": r.get("rating"), "paid": bool(r.get("paid")), "source": r.get("source") or "auto",
        })

    # ---- refunds ------------------------------------------------------------------------------------------------
    refunds = []
    for t in db["build_tickets"].find({"guild_id": gid, "refunded": True}).sort("refunded_at", -1).limit(300):
        kept = int(t.get("refund_fee") or 0)
        op = int(t.get("refund_owner_pct") or 0) or int(cfg["owner_fee_percent"] or 0)
        fp = int(t.get("refund_fee_pct") or cfg["fee_percent"])
        refunds.append({
            "when": _when(t.get("refunded_at")), "customer": name(t.get("opener_id")),
            "build": t.get("refund_build_name") or "Build", "refunded": int(t.get("refund_amount") or 0),
            "percent": t.get("refund_percent", 100), "kept": kept,
            "owner_share": B.refund_owner_share(kept, op, fp) if kept else 0,
        })

    # ---- builders -----------------------------------------------------------------------------------------------
    per: dict = {}
    for r in ledger:
        b = per.setdefault(str(r.get("builder_id")), {"done": 0, "worth": 0, "paid": 0, "owed": 0, "ratings": []})
        b["done"] += 1
        b["worth"] += int(r.get("worth") or 0)
        b["paid" if r.get("paid") else "owed"] += int(r.get("received") or 0)
        if r.get("rating"):
            b["ratings"].append(int(r["rating"]))
    active: dict = {}
    for row in open_rows:
        if row["stage"] in ("building", "confirm", "review") and row["builder"] != "—":
            active[row["builder"]] = active.get(row["builder"], 0) + 1
    builders = []
    for uid, b in per.items():
        n = name(uid)
        builders.append({"name": n, "done": b["done"], "active": active.pop(n, 0), "worth": b["worth"], "paid": b["paid"],
                         "owed": b["owed"], "rating": round(sum(b["ratings"]) / len(b["ratings"]), 1) if b["ratings"] else None,
                         "ratings_n": len(b["ratings"])})
    for n, a in active.items():            # building something but nothing finished yet
        builders.append({"name": n, "done": 0, "active": a, "worth": 0, "paid": 0, "owed": 0, "rating": None, "ratings_n": 0})
    builders.sort(key=lambda x: (-x["worth"], x["name"]))

    # ---- payouts made -------------------------------------------------------------------------------------------
    builder_payouts = [{
        "when": _when(p.get("finished_at") or p.get("started_at")), "builder": name(p.get("builder_id")),
        "ign": p.get("builder_ign") or "—", "sender": p.get("sender") or "—", "build": p.get("build_name") or "Build",
        "amount": int(p.get("amount") or 0), "forced": bool(p.get("forced")),
    } for p in db["build_payouts"].find({"guild_id": gid, "status": "paid"}).sort("finished_at", -1).limit(200)]
    owner_payouts = [{
        "when": _when(p.get("finished_at") or p.get("started_at")), "ign": p.get("owner_ign") or "—",
        "sender": p.get("sender") or "—", "amount": int(p.get("amount") or 0), "forced": p.get("status") == "forced",
    } for p in db["owner_payouts"].find({"guild_id": gid, "status": {"$in": ["paid", "forced"]}}).sort("finished_at", -1).limit(100)]

    # ---- money in, last 14 days -------------------------------------------------------------------------------
    since = datetime.now(timezone.utc) - timedelta(days=14)
    days = {}
    for i in range(14):
        days[(datetime.utcnow() - timedelta(days=13 - i)).strftime("%m-%d")] = 0
    for p in db["build_payments"].find({"guild_id": gid, "status": "paid", "finished_at": {"$gte": since}},
                                       {"amount": 1, "finished_at": 1}):
        k = _naive(p.get("finished_at")).strftime("%m-%d") if p.get("finished_at") else None
        if k in days:
            days[k] += int(p.get("amount") or 0)
    top = max(days.values()) or 1
    chart = [{"day": k, "amount": v, "pct": max(2, int(v * 100 / top)) if v else 0} for k, v in days.items()]

    owner_on = bool(cfg["owner_ign"] and cfg["owner_fee_percent"])
    return {
        "d": d, "cfg": cfg, "owner_on": owner_on, "server_keeps": max(0, d["fee"] - d["owner_earned"]) if owner_on else d["fee"],
        "funnel": [{"key": k, "label": STAGES[k][0], "n": funnel[k], "color": STAGES[k][1]} for k in STAGES],
        "open_rows": open_rows, "left_rows": left_rows, "completed": completed, "refunds": refunds,
        "builders": builders, "builder_payouts": builder_payouts, "owner_payouts": owner_payouts, "chart": chart,
        "open_total": len(open_rows), "stages": STAGES,
    }
