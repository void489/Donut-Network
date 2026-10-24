"""
Build service.

COMMANDS (two roles, two roots; everything else is buttons)
  Builders (allow roles; managers can use these too)         /bhelp
    /builder claim | unclaim | finish | info | rename | close | stats
    /builder ign set | change
  Build managers (manager role / admins)                      /bmhelp
    /bm panel | assign | unassign | cancel-pay | reset-quote | quote add | quote accept | repost-quote | skip-file | queue | recap (live panel: money, owner, builds left to pay)
    /bm price set | more | less | cancel | force
    /bm refund start | cancel | force
    /bm force pay | finish | builder-paid | owner-paid
    /bm ign set | remove
    /bm ticket add | remove | force-close

FLOW
  The panel (/bm panel) lists every build + price. Picking one opens a private ticket; the bot sends the
  .litematic and a payment embed: Start Pay -> asks the payer's IGN, checks it exists, watches the receiver IGN
  (set on the dashboard) for 5 minutes, pings the ping role when the payment shows up. Every confirmed payment
  is logged to the payment-log channel.
  Custom builds: builders press "Submit Quote" (price + note); the customer picks one; the normal payment embed
  appears for that price and the picked builder is auto-claimed once paid.
  /builder finish (claiming builder only) DMs the customer "did you receive it?"; on Yes they rate + review, are
  removed from the ticket and it moves to the finished category. The build manager then gets a "pay builder"
  embed (price minus the server's fee %, set on the dashboard) with Pay Now; once paid the builder is DMed and
  the ticket closes. No review in 24h = automatic 5 stars. /bm force finish skips the customer's confirmation.
  /bm force pay marks a ticket paid; /bm force builder-paid marks the builder payout paid.
  /bm price set overrides the price (also after payment). After payment, /bm price more|less changes the price
  while the builder stays: more = the customer pays the extra, less = the BM pays the difference back; the new
  price applies once the money is seen. /bm price cancel | force drops / completes a pending change.
  /bm refund start [percentage] (build manager only) refunds the customer: 100% by default, or any percentage you
  choose; the rest of what they paid is kept as the server's fee. The builder is NOT credited anything (the build
  is never finished, so no ledger entry / payout). When the refund is seen the builder is unclaimed, the customer
  is DMed and the ticket is closed. /bm refund cancel | force drop it / complete it untracked.
  /bm ticket force-close deletes a ticket even if it is paid (asks to confirm, warns if not refunded).
  Builder tracker: every finished build is saved to builder_ledger (worth, money received after fee, rating),
  marked paid when the payout is confirmed. Dashboard (Manage -> Builder tracker) can edit entries.
  Builder payouts are logged to builder_payout_log_channel_id (falls back to the payment log).
  Digout: a "Digout" panel button asks IGN + X / Y / Z and prices it X*Y*Z*price_per_block.

Everything is configured per server on the dashboard (Bot Servers -> Manage):
  1. ping role        - pinged in every build ticket
  2. allow roles      - can see / talk in build tickets
  3. build manager    - can post the panel, send the build file, close tickets
  category            - where ticket channels are created. The NAME is stored, so the bot
                        creates it when missing and deletes it when the last ticket closes
  receiver IGN        - the in-game account customers pay
  builds              - name + .litematic file + price

Collections (shared with app.py): build_settings, builds, build_files (GridFS),
build_tickets, build_panels.
"""
import asyncio
import hashlib
import io
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
import gridfs
from bson.objectid import ObjectId
from discord import app_commands
from discord.ext import commands, tasks

from cogs.command_perms_core import check_enabled_or_reply
from cogs.pay import (ERRORS, POLL_GAP_SECONDS, WATCH_SECONDS, _get, _sessions,
                      amount_matches, fmt_ign_ok)
from cogs.pay import check_ign, paced, ign_cache_add, release_follow
from cogs.stats import ERROR_MESSAGES, lookup_stats

logger = logging.getLogger(__name__)

COLOR = 0x5865F2
MAX_OPTIONS = 25
MAX_BUILD_WATCHES = 8          # same cap as /pay
WATCHES: dict = {}             # payer ign (lower) -> {"channel_id", "receiver", "ends", "cancel"}
CHANNEL_WATCH: dict = {}       # ticket channel id -> payer key
OWNER_WATCH: dict = {}         # guild id -> payer key of the running "settle owner" payment
_BOT = None                    # set by the cog; lets background code refresh the recap panel
_tasks: set = set()            # keeps background watch tasks alive
_category_locks: dict[int, asyncio.Lock] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_money(n) -> str:
    """250000 -> $250K, 1500000 -> $1.5M, 1250000 -> $1.25M (no trailing zeros)."""
    n = float(n or 0)
    units = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
    for i, (div, suf) in enumerate(units):
        if abs(n) >= div:
            if round(abs(n) / div, 2) >= 1000 and i > 0:      # 999,999 -> $1M, not $1,000K
                div, suf = units[i - 1]
            return f"${n / div:,.2f}".rstrip("0").rstrip(".") + suf
    return f"${n:,.0f}"


def short_amount(n) -> str:
    """1500000 -> '1.5m', 250000 -> '250k' (what you'd type into /pay)."""
    n = float(n or 0)
    for div, suf in ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k")):
        if n >= div:
            txt = f"{n / div:.2f}".rstrip("0").rstrip(".")
            if round(float(txt) * div) == round(n):
                return txt + suf
            break
    return str(int(round(n)))


def slug(text: str, limit: int = 100) -> str:
    text = re.sub(r"\s+", "-", str(text or "").strip().lower())
    text = re.sub(r"[^\w-]", "", text)
    return re.sub(r"-{2,}", "-", text)[:limit].strip("-")


def parse_price(text):
    """'10k' -> 10000, '1.5m' -> 1500000, '2b', '1,000'. None if invalid."""
    m = re.fullmatch(r"\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmbt])?\s*", str(text).lower())
    if not m:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[m.group(2) or ""]
    val = round(float(m.group(1).replace(",", "")) * mult)
    return int(val) if val > 0 else None


def _ticket_build_base(db, doc: dict):
    if doc.get("kind") == "digout":
        return {"_id": "digout", "name": f"Digout {doc.get('dig_x')}x{doc.get('dig_y')}x{doc.get('dig_z')}",
                "price": int(doc.get("quote_price") or 0)}
    if doc.get("build_id"):
        try:
            return db["builds"].find_one({"_id": ObjectId(doc["build_id"])})
        except Exception:
            return None
    if doc.get("quote_price"):
        return {"_id": "custom", "name": "Custom Build", "price": doc["quote_price"]}
    return None


def ticket_build(db, doc: dict):
    """The 'build' a ticket is paying for: a catalog build, or the accepted quote of a custom build.
    A build manager's /bm price set (price_override) wins over every other price source."""
    build = _ticket_build_base(db, doc)
    ov = doc.get("price_override")
    if ov:
        build = {**(build or {"_id": "custom", "name": "Build"}), "price": int(ov)}
    return build


def _owner_pct_from(d: dict) -> int:
    """The owner's cut (% of the build price). It comes out of the server fee, so it is always 0 (off) or
    between 1 and the fee %."""
    fee = clamp_fee(d.get("fee_percent")) if d.get("fee_percent") not in (None, "") else PAYOUT_FEE_PERCENT
    try:
        pct = int(float(d.get("owner_fee_percent") or 0))
    except (TypeError, ValueError):
        pct = 0
    return max(0, min(pct, fee))


def get_owner_pct(db, guild_id) -> int:
    return _owner_pct_from(db["build_settings"].find_one({"_id": str(guild_id)}) or {})


DEFAULT_TICKET_LIMIT = 2


def clamp_ticket_limit(v) -> int:
    """Max open build tickets per person (dashboard setting): whole number 1-50, default 2."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULT_TICKET_LIMIT
    return max(1, min(50, n))


def _get_settings(db, guild_id) -> dict:
    d = db["build_settings"].find_one({"_id": str(guild_id)}) or {}
    return {
        "ping_role_id": d.get("ping_role_id") or "",
        "allow_role_ids": [str(r) for r in (d.get("allow_role_ids") or [])],
        "manager_role_id": d.get("manager_role_id") or "",
        "category_id": d.get("category_id") or "",
        "category_name": d.get("category_name") or "",
        "receiver_ign": d.get("receiver_ign") or "",
        "payment_log_channel_id": d.get("payment_log_channel_id") or "",
        "builder_payout_log_channel_id": d.get("builder_payout_log_channel_id") or "",
        "review_channel_id": d.get("review_channel_id") or "",
        "finished_category_id": d.get("finished_category_id") or "",
        "finished_category_name": d.get("finished_category_name") or "",
        "price_per_block": int(d.get("price_per_block") or 0),
        "fee_percent": clamp_fee(d.get("fee_percent")) if d.get("fee_percent") not in (None, "") else PAYOUT_FEE_PERCENT,
        "max_tickets_per_user": clamp_ticket_limit(d.get("max_tickets_per_user")),
        "owner_ign": d.get("owner_ign") or "",
        "owner_fee_percent": _owner_pct_from(d),
        "panel_title": d.get("panel_title") or "",
        "panel_color": d.get("panel_color") or "",
        "panel_banner_file_id": d.get("panel_banner_file_id") or "",
        "panel_banner_name": d.get("panel_banner_name") or "",
        "panel_logo_file_id": d.get("panel_logo_file_id") or "",
        "panel_logo_name": d.get("panel_logo_name") or "",
        "panel_intro": d.get("panel_intro") or "",
    }


def brand_color(cfg: dict) -> int:
    """The server's accent colour from the dashboard (#RRGGBB), else the default."""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str((cfg or {}).get("panel_color") or "").strip())
    return int(m.group(1), 16) if m else COLOR


def _get_builds(db, guild_id) -> list:
    return list(db["builds"].find({"guild_id": str(guild_id)}).sort("created_at", 1))


def _role_ids(member: discord.Member) -> set:
    return {str(r.id) for r in member.roles}


def is_manager(member: discord.Member, cfg: dict) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool(cfg["manager_role_id"]) and cfg["manager_role_id"] in _role_ids(member)


def is_ticket_staff(member: discord.Member, cfg: dict) -> bool:
    """Admins, the build manager role, or any allowed role."""
    return is_manager(member, cfg) or bool(set(cfg["allow_role_ids"]) & _role_ids(member))


HOW_IT_WORKS_STEPS = (
    "**1️⃣ Pick** - choose a build, press **⛏️ Digout** or **🛠️ Custom Build**. A private ticket opens.",
    "**2️⃣ Pay** - press **Start Pay**, run the `/pay` command in-game. It's verified automatically.",
    "**3️⃣ Build** - a builder claims your ticket and gets to work.",
    "**4️⃣ Confirm** - tell us you received it and (optionally) leave a review.",
)


async def load_panel_images(db, cfg: dict) -> dict:
    """{"banner": (filename, bytes), "logo": (filename, bytes)} for the uploads that load fine."""
    def load():
        out = {}
        fs = gridfs.GridFS(db, collection="panel_images")
        for kind in ("banner", "logo"):
            fid, name = cfg.get(f"panel_{kind}_file_id"), cfg.get(f"panel_{kind}_name")
            if not fid or not name:
                continue
            try:
                out[kind] = (name, fs.get(ObjectId(fid)).read())
            except Exception as e:
                logger.warning(f"panel {kind} image couldn't be loaded: {e}")
        return out
    return await asyncio.to_thread(load)


def panel_files(images: dict) -> list:
    """Fresh discord.File objects (they can only be sent once)."""
    return [discord.File(io.BytesIO(data), filename=name) for name, data in images.values()]


def panel_embed(guild: discord.Guild, builds: list, ppb: int = 0, cfg: dict = None, images: dict = None) -> discord.Embed:
    cfg = cfg or {}
    title = cfg.get("panel_title") or f"🍩 {guild.name} · Build Services"
    intro = cfg.get("panel_intro") or "Pick a build below, pay in-game, and a builder gets to work."
    lines = [intro, "", "**🧱 Catalog builds**"]
    shown = builds[:MAX_OPTIONS]
    if shown:
        lines += [f"• **{b['name']}** - `{fmt_money(b.get('price'))}`" for b in shown]
    else:
        lines.append("*No builds listed yet.*")
    lines.append("")
    if ppb:
        lines.append(f"**⛏️ Digout** - `{fmt_money(ppb)}` per block (X × Y × Z)")
    lines.append("**🛠️ Custom Build** - send your litematic, builders send you quotes")
    desc = "\n".join(lines)
    if len(desc) > 3900:
        desc = desc[:3890].rsplit("\n", 1)[0] + "\n…"
    embed = discord.Embed(title=title[:250], description=desc, color=brand_color(cfg))
    embed.add_field(name="How it works", value="\n".join(HOW_IT_WORKS_STEPS), inline=False)
    images = images or {}
    if "banner" in images:
        embed.set_image(url=f"attachment://{images['banner'][0]}")
    if "logo" in images:
        embed.set_thumbnail(url=f"attachment://{images['logo'][0]}")
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    footer = "Payments are verified automatically in-game"
    if len(builds) > MAX_OPTIONS:
        footer += f" · showing the first {MAX_OPTIONS} builds"
    embed.set_footer(text=footer, icon_url=guild.icon.url) if guild.icon else embed.set_footer(text=footer)
    return embed


def panel_signature(builds: list, ppb: int = 0, cfg: dict = None) -> str:
    cfg = cfg or {}
    style = "|".join(str(cfg.get(k) or "") for k in
                     ("panel_title", "panel_color", "panel_banner_file_id", "panel_logo_file_id", "panel_intro"))
    raw = f"v4|{ppb}|{style}|" + "|".join(f"{b['_id']}:{b['name']}:{b.get('price')}" for b in builds[:MAX_OPTIONS])
    return hashlib.md5(raw.encode()).hexdigest()


def _category_name(cfg: dict, prefix: str = "category") -> str:
    """Configured category name (survives the category being deleted / bot restarts).
    Falls back to the legacy "__new__:<name>" value in <prefix>_id."""
    name = (cfg.get(f"{prefix}_name") or "").strip()
    raw = cfg.get(f"{prefix}_id") or ""
    if not name and raw.startswith("__new__:"):
        name = raw[len("__new__:"):].strip()
    return name


async def resolve_category(guild: discord.Guild, db, cfg: dict, prefix: str = "category"):
    """A configured category, by settings key prefix ("category" = build tickets,
    "finished_category" = finished tickets): by id, else by name, else (re)created. The name is stored permanently."""
    raw = cfg.get(f"{prefix}_id") or ""
    name = _category_name(cfg, prefix)
    if not raw and not name:
        return None
    lock = _category_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        cat = None
        if raw.isdigit():
            ch = guild.get_channel(int(raw))
            if isinstance(ch, discord.CategoryChannel):
                cat = ch
                name = name or ch.name
        if cat is None and name:
            cat = next((c for c in guild.categories if c.name.strip().lower() == name.lower()), None)
            if cat is None:
                try:
                    cat = await guild.create_category(name=name, reason="Build ticket setup")
                except discord.HTTPException:
                    return None
        if cat is None:
            return None
        if raw != str(cat.id) or cfg.get(f"{prefix}_name") != name:
            await asyncio.to_thread(lambda: db["build_settings"].update_one(
                {"_id": str(guild.id)},
                {"$set": {f"{prefix}_id": str(cat.id), f"{prefix}_name": name}}, upsert=True))
            cfg[f"{prefix}_id"], cfg[f"{prefix}_name"] = str(cat.id), name
        return cat


async def delete_category_if_empty(guild: discord.Guild, db, category_id: int):
    """Delete the build / finished category once its last ticket channel is gone. The setting
    (name) is kept, so it is recreated the next time it's needed."""
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    if str(category_id) not in (cfg["category_id"], cfg["finished_category_id"]):
        return                      # neither the build nor the finished category - never touch other categories
    lock = _category_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        cat = guild.get_channel(category_id)
        if not isinstance(cat, discord.CategoryChannel) or cat.channels:
            return
        try:
            await cat.delete(reason="Build ticket category empty")
        except discord.HTTPException as e:
            logger.warning(f"could not delete empty build category: {e}")


def ticket_overwrites(guild: discord.Guild, opener: discord.Member, cfg: dict) -> dict:
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, attach_files=True),
        opener: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                            read_message_history=True, attach_files=True),
    }
    ids = list(cfg["allow_role_ids"])
    if cfg["manager_role_id"]:
        ids.append(cfg["manager_role_id"])
    for rid in ids:
        try:
            role = guild.get_role(int(rid))
        except (TypeError, ValueError):
            role = None
        if role:
            ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                   read_message_history=True, attach_files=True)
    return ow


def err(msg: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {msg}", color=0xED4245)


def add_pay_cmd(embed: discord.Embed, cmd) -> discord.Embed:
    """Always give the person the /pay command to copy-paste, whatever else the embed says
    (errors, 'checking…', busy, etc.). Skipped if the embed already contains it."""
    if not cmd or "/pay " in (embed.description or ""):
        return embed
    if any("/pay " in (f.value or "") for f in embed.fields):
        return embed
    embed.add_field(name="💸 Pay command (copy & paste)", value=f"```{cmd}```", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Panel (dropdown) -> opens the ticket
# ---------------------------------------------------------------------------

async def open_build_ticket(interaction: discord.Interaction, build: dict = None, ign: str = "", digout: str = "", farm: str = "",
                            dims: tuple = None):
    """Creates the ticket channel. build=None -> a custom build ticket (the customer
    sends their own .litematic file). The interaction must already be deferred (ephemeral)."""
    guild, db, user = interaction.guild, interaction.client.db, interaction.user
    dig = dims is not None            # Digout: price = X*Y*Z*price_per_block, no build file
    custom = build is None and not dig

    # max open build tickets per person (dashboard setting, default 2)
    blocked = await ticket_limit_block(guild, db, user)
    if blocked:
        return await interaction.followup.send(embed=blocked, ephemeral=True)

    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    ppb = int(cfg.get("price_per_block") or 0)
    if dig:
        if ppb <= 0:
            return await interaction.followup.send(embed=err("Digouts aren't set up yet (no price per block)."), ephemeral=True)
        dx, dy, dz = dims
        price = dx * dy * dz * ppb
        build = {"_id": "digout", "name": f"Digout {dx}x{dy}x{dz}", "price": price}
    label = "digout" if dig else ("custom" if custom else build["name"])
    channel = None
    for attempt in range(2):
        category = await resolve_category(guild, db, cfg)
        try:
            channel = await guild.create_text_channel(
                name=slug(f"{label}-{user.name}") or "build-ticket",
                category=category,
                overwrites=ticket_overwrites(guild, user, cfg),
                reason=f"Build ticket ({label}) opened by {user}",
            )
            break
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=err("I don't have permission to create ticket channels."), ephemeral=True)
        except discord.HTTPException:
            if attempt == 1:       # category was deleted under us - retry once
                return await interaction.followup.send(
                    embed=err("Couldn't create the ticket channel, please try again."), ephemeral=True)

    await asyncio.to_thread(lambda: db["build_tickets"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id),
        "opener_id": str(user.id), "build_id": None if (custom or dig) else str(build["_id"]),
        "kind": "digout" if dig else ("custom" if custom else "build"), "awaiting_file": custom,
        "ign": ign, "digout": digout, "farm": farm,
        "status": "open", "created_at": datetime.now(timezone.utc),
        **({"dig_x": dims[0], "dig_y": dims[1], "dig_z": dims[2], "price_per_block": ppb,
            "quote_price": build["price"]} if dig else {}),
    }))

    content = user.mention
    ping = guild.get_role(int(cfg["ping_role_id"])) if cfg["ping_role_id"].isdigit() else None
    if ping:
        content += f" {ping.mention}"
    if dig:
        embed = discord.Embed(
            title="⛏️ Digout",
            description=f"Digout ticket opened by {user.mention}.\nStaff will be with you shortly.",
            color=brand_color(cfg))
        embed.add_field(name="👤 Customer", value=user.mention, inline=True)
        embed.add_field(name="🎮 IGN", value=f"`{ign}`", inline=True)
        embed.add_field(name="📐 Dimensions", value=f"**{dims[0]} × {dims[1]} × {dims[2]}** (X × Y × Z)", inline=False)
        embed.add_field(name="🧮 Blocks", value=f"{dims[0] * dims[1] * dims[2]:,}", inline=True)
        embed.add_field(name="💵 Price per block", value=fmt_money(ppb), inline=True)
        embed.add_field(name="💰 Total price", value=f"**{fmt_money(build['price'])}**", inline=True)
    elif custom:
        embed = discord.Embed(
            title="🛠️ Custom Build",
            description=(f"Custom build ticket opened by {user.mention}.\n\n"
                         "📎 **Please send your `.litematic` file in this channel.**\n"
                         "Staff will be notified as soon as it arrives."),
            color=brand_color(cfg))
        embed.add_field(name="👤 Customer", value=user.mention, inline=True)
        embed.add_field(name="🎮 IGN", value=f"`{ign}`", inline=True)
        embed.add_field(name="⛏️ Digout", value=digout or "—", inline=True)
        embed.add_field(name="🌾 Requested farm", value=farm or "—", inline=False)
    else:
        embed = discord.Embed(
            title=f"🧱 {build['name']}",
            description=f"Build ticket opened by {user.mention}.\nStaff will be with you shortly.",
            color=brand_color(cfg))
        embed.add_field(name="💰 Price", value=f"**{fmt_money(build.get('price'))}**", inline=True)
        embed.add_field(name="👤 Customer", value=user.mention, inline=True)
        embed.add_field(name="🎮 IGN", value=f"`{ign}`", inline=True)
    await channel.send(content=content, embed=embed, view=BuildTicketView(),
                       allowed_mentions=discord.AllowedMentions(users=True, roles=True))

    if not custom:   # 1) the build file (not for digouts), 2) the payment embed
        if not dig:
            await send_build_file(channel, db, build)
        pay_msg = await channel.send(embed=pay_embed_idle(build, cfg), view=PayView(custom=_is_custom(build)))
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"channel_id": str(channel.id)}, {"$set": {"pay_message_id": str(pay_msg.id)}}))

    await interaction.followup.send(embed=discord.Embed(
        description=f"✅ Your ticket has been created: {channel.mention}", color=0x57F287), ephemeral=True)


async def ticket_limit_block(guild: discord.Guild, db, user) -> "discord.Embed | None":
    """An error embed if the user already has the max number of open build tickets (per-server limit), else None.
    Stale records (channel deleted) are closed on the way."""
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    limit = cfg["max_tickets_per_user"]
    docs = await asyncio.to_thread(lambda: list(db["build_tickets"].find(
        {"guild_id": str(guild.id), "opener_id": str(user.id), "status": "open", "finished": {"$ne": True}})))
    live = []
    for d in docs:
        ch = guild.get_channel(int(d["channel_id"]))
        if ch is None:
            await asyncio.to_thread(lambda d=d: db["build_tickets"].update_one(
                {"_id": d["_id"]}, {"$set": {"status": "closed"}}))
        else:
            live.append(ch)
    if len(live) >= limit:
        mentions = ", ".join(c.mention for c in live)
        if limit == 1:
            return err(f"You already have an open build ticket: {mentions}")
        return err(f"You already have {len(live)} open build tickets (the limit is {limit}): {mentions}")
    return None


class IntakeModal(discord.ui.Modal, title="Before we start"):
    ign = discord.ui.TextInput(label="What is your IGN?", placeholder="Your in-game name",
                               min_length=1, max_length=25, required=True)
    digout = discord.ui.TextInput(label="Do you have a digout?", placeholder="Yes / No",
                                  min_length=1, max_length=40, required=True)
    farm = discord.ui.TextInput(label="What farm do you want?", style=discord.TextStyle.paragraph,
                                placeholder="Describe the farm you want built",
                                min_length=2, max_length=400, required=True)

    def __init__(self, build: dict = None):
        super().__init__()
        self.build = build      # None -> custom build (3 questions); a catalog build only needs the IGN (price is set)
        if build is not None:
            self.remove_item(self.digout)
            self.remove_item(self.farm)

    async def on_submit(self, interaction: discord.Interaction):
        ign = str(self.ign.value).strip()
        if not fmt_ign_ok(ign):
            return await interaction.response.send_message(
                embed=err("That doesn't look like a valid IGN. Pick the build again and re-enter it."), ephemeral=True)
        digout = farm = ""
        if self.build is None:
            digout = str(self.digout.value).strip()
            low = digout.lower()
            if low in ("y", "yes", "yeah", "yep", "ye"):
                digout = "Yes"
            elif low in ("n", "no", "nope", "nah"):
                digout = "No"
            farm = str(self.farm.value).strip()
        await interaction.response.defer(ephemeral=True)
        await open_build_ticket(interaction, self.build, ign=ign, digout=digout, farm=farm)


class BuildSelect(discord.ui.Select):
    def __init__(self, builds: list = None):
        options = [
            discord.SelectOption(label=b["name"][:100], value=str(b["_id"]),
                                 description=f"Price: {fmt_money(b.get('price'))}", emoji="🧱")
            for b in (builds or [])[:MAX_OPTIONS]
        ] or [discord.SelectOption(label="No builds available", value="none")]
        super().__init__(placeholder="🏗️ Select a build…", options=options,
                         custom_id="build_panel_select", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        db = interaction.client.db
        value = self.values[0]
        if guild is None or db is None or value == "none":
            return await interaction.response.send_message(embed=err("No builds are available right now."), ephemeral=True)
        try:
            build = await asyncio.to_thread(
                lambda: db["builds"].find_one({"_id": ObjectId(value), "guild_id": str(guild.id)}))
        except Exception:
            build = None
        if not build:
            return await interaction.response.send_message(embed=err("That build no longer exists."), ephemeral=True)
        blocked = await ticket_limit_block(guild, db, interaction.user)
        if blocked:
            return await interaction.response.send_message(embed=blocked, ephemeral=True)
        await interaction.response.send_modal(IntakeModal(build))
        try:   # reset the dropdown so the same build can be picked again
            await interaction.message.edit(view=self.view)
        except discord.HTTPException:
            pass


class CustomBuildButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Custom Build", emoji="🛠️", style=discord.ButtonStyle.secondary,
                         custom_id="build_custom_open")

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.client.db is None:
            return await interaction.response.send_message(embed=err("Not available right now."), ephemeral=True)
        blocked = await ticket_limit_block(interaction.guild, interaction.client.db, interaction.user)
        if blocked:
            return await interaction.response.send_message(embed=blocked, ephemeral=True)
        await interaction.response.send_modal(IntakeModal(None))


MAX_DIG = 1000


class DigoutModal(discord.ui.Modal, title="Digout"):
    ign = discord.ui.TextInput(label="What is your IGN?", placeholder="Your in-game name",
                               min_length=1, max_length=25, required=True)
    x = discord.ui.TextInput(label="X dimension", placeholder="e.g. 50", min_length=1, max_length=5, required=True)
    y = discord.ui.TextInput(label="Y dimension", placeholder="e.g. 30", min_length=1, max_length=5, required=True)
    z = discord.ui.TextInput(label="Z dimension", placeholder="e.g. 50", min_length=1, max_length=5, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        ign = str(self.ign.value).strip()
        if not fmt_ign_ok(ign):
            return await interaction.response.send_message(
                embed=err("That doesn't look like a valid IGN. Press **Digout** again and re-enter it."), ephemeral=True)
        dims = []
        for label, box in (("X", self.x), ("Y", self.y), ("Z", self.z)):
            raw = str(box.value).strip()
            if not raw.isdigit() or not 1 <= int(raw) <= MAX_DIG:
                return await interaction.response.send_message(
                    embed=err(f"**{label}** must be a whole number from 1 to {MAX_DIG}."), ephemeral=True)
            dims.append(int(raw))
        await interaction.response.defer(ephemeral=True)
        await open_build_ticket(interaction, None, ign=ign, dims=tuple(dims))


class DigoutButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Digout", emoji="⛏️", style=discord.ButtonStyle.primary,
                         custom_id="build_digout_open")

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        if interaction.guild is None or db is None:
            return await interaction.response.send_message(embed=err("Not available right now."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if cfg["price_per_block"] <= 0:
            return await interaction.response.send_message(
                embed=err("Digouts aren't available right now."), ephemeral=True)
        blocked = await ticket_limit_block(interaction.guild, db, interaction.user)
        if blocked:
            return await interaction.response.send_message(embed=blocked, ephemeral=True)
        await interaction.response.send_modal(DigoutModal())


class HowItWorksButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="How it works", emoji="ℹ️", style=discord.ButtonStyle.secondary,
                         custom_id="build_howitworks")

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id) if (db is not None and interaction.guild) else {}
        e = discord.Embed(title="ℹ️ How it works", color=brand_color(cfg),
                          description="\n\n".join(HOW_IT_WORKS_STEPS))
        e.add_field(name="💬 Need help?", value="Ask in your ticket - staff are notified as soon as it opens.", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


class BuildPanelView(discord.ui.View):
    def __init__(self, builds: list = None, register: bool = False, digout: bool = False):
        super().__init__(timeout=None)
        if builds or register:      # register=True: used once at startup so all components are routable
            self.add_item(BuildSelect(builds))
        if digout or register:
            self.add_item(DigoutButton())
        self.add_item(CustomBuildButton())
        self.add_item(HowItWorksButton())


# ---------------------------------------------------------------------------
# Inside a ticket: build file, payment embed + tracking, close
# ---------------------------------------------------------------------------

async def send_build_file(channel: discord.TextChannel, db, build: dict):
    def load():
        if not build.get("file_id"):
            return None
        return gridfs.GridFS(db, collection="build_files").get(build["file_id"]).read()

    try:
        data = await asyncio.to_thread(load)
    except Exception as e:
        logger.warning(f"build file read failed: {e}")
        data = None
    if not data:
        return await channel.send("⚠️ The build file couldn't be loaded. Staff will send it manually.")
    if len(data) > channel.guild.filesize_limit:
        return await channel.send(
            f"⚠️ The build file ({len(data) / 1048576:.1f} MB) is over this server's upload limit. "
            "Staff will send it manually.")
    await channel.send(
        content=f"📦 **{build['name']}** — here's your build file:",
        file=discord.File(io.BytesIO(data), filename=build.get("filename") or f"{build['name']}.litematic"))


def pay_embed_idle(build: dict, cfg: dict) -> discord.Embed:
    """Before the IGN is verified: no /pay command and no receiver shown."""
    if cfg["receiver_ign"]:
        desc = ("Press **Start Pay** and I'll verify the IGN you submitted.\n"
                "Once it's verified, the payment command will appear here.")
    else:
        desc = "⚠️ The receiver IGN isn't set on the dashboard yet. Ask staff."
    e = discord.Embed(title="💸 Payment Request", description=desc, color=discord.Color.from_rgb(255, 170, 0))
    e.add_field(name="🧱 Build", value=build["name"], inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(build.get('price'))}**", inline=True)
    e.add_field(name="⏳ Time to pay", value="**5 minutes** after your IGN is verified", inline=False)
    e.set_footer(text="Press Start Pay to begin.")
    return e


def pay_embed_waiting(build: dict, payer: str, receiver: str, ends: float) -> discord.Embed:
    amount = short_amount(build.get("price"))
    e = discord.Embed(
        title="⏳ Waiting for payment", color=discord.Color.from_rgb(255, 170, 0),
        description=f"Run this command in-game to pay:\n```/pay {receiver} {amount}```")
    e.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
    e.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(build.get('price'))}**", inline=True)
    e.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
    e.set_footer(text="Waiting for the payment to show up in-game…")
    return e


def _is_custom(build) -> bool:
    return bool(build) and build.get("_id") == "custom"


class PayView(discord.ui.View):
    """Persistent: the Start Pay button on the payment embed (+ Change Quote on custom builds)."""
    def __init__(self, custom: bool = False):
        super().__init__(timeout=None)
        if custom:
            self.add_item(ChangeQuoteButton())

    @discord.ui.button(label="Start Pay", emoji="💸", style=discord.ButtonStyle.success,
                       custom_id="build_pay_start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        if not doc:
            return await interaction.response.send_message(embed=err("This is not an active build ticket."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if str(interaction.user.id) != doc["opener_id"] and not is_ticket_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the ticket opener can start the payment."), ephemeral=True)
        if doc.get("paid"):
            return await interaction.response.send_message(embed=err("This build is already paid."), ephemeral=True)
        if not cfg["receiver_ign"]:
            return await interaction.response.send_message(
                embed=err("The receiver IGN isn't set on the dashboard yet."), ephemeral=True)
        if interaction.channel.id in CHANNEL_WATCH:
            return await interaction.response.send_message(
                embed=err("Payment is already being tracked in this ticket."), ephemeral=True)
        payer = (doc.get("ign") or "").strip()
        if not payer:       # older tickets without a stored IGN
            return await interaction.response.send_modal(PayerModal())
        await interaction.response.defer(ephemeral=True)
        await run_payment(interaction, payer)


class WaitingView(discord.ui.View):
    """Persistent: Cancel button while a payment is being tracked (+ Change Quote on custom builds)."""
    def __init__(self, custom: bool = False):
        super().__init__(timeout=None)
        if custom:
            self.add_item(ChangeQuoteButton())

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger,
                       custom_id="build_pay_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        if not doc:
            return await interaction.response.send_message(embed=err("This is not an active build ticket."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if str(interaction.user.id) != doc["opener_id"] and not is_ticket_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the ticket opener or staff can cancel this."), ephemeral=True)
        key = CHANNEL_WATCH.get(interaction.channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()      # the watch task cleans up
        build = await asyncio.to_thread(ticket_build, db, doc)
        await interaction.response.edit_message(embed=pay_embed_idle(build, cfg), view=PayView(custom=_is_custom(build)))


class PayerModal(discord.ui.Modal, title="Start payment"):
    ign = discord.ui.TextInput(label="Your in-game name (IGN)", placeholder="The account that will pay",
                               min_length=1, max_length=25, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        payer = str(self.ign.value).strip()
        await interaction.response.defer(ephemeral=True)
        await run_payment(interaction, payer)


# ---------------------------------------------------------------------------
# Verified IGN cache (collection verified_igns): an IGN that was already checked is never looked up again.
#   {ign_lower, ign, source: "bot" | "admin", added_at, added_by, last_used_at, uses}
# "bot" = added automatically after a successful in-game lookup; "admin" = added by hand on the dashboard
# (counts as verified). Entries can be added / removed on the dashboard (Verified IGNs).
# ---------------------------------------------------------------------------

async def run_payment(interaction: discord.Interaction, payer: str):
    """Verify the payer IGN, start following the receiver and track the payment.
    The interaction must already be deferred (ephemeral)."""
    db = interaction.client.db
    guild, channel = interaction.guild, interaction.channel
    cmd_ref = [None]        # set as soon as receiver + amount are known

    async def show(embed):
        """Private (only they see it): edits the same ephemeral message in place."""
        embed = add_pay_cmd(embed, cmd_ref[0])
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"build pay: couldn't update private message: {e}")

    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"}))
    if not doc or doc.get("paid"):
        return await show(err("This ticket can't take a payment right now."))
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    receiver = cfg["receiver_ign"]
    build = await asyncio.to_thread(ticket_build, db, doc)
    if not receiver or not build:
        return await show(err("This build isn't set up for payments."))
    cmd_ref[0] = f"/pay {receiver} {short_amount(build.get('price'))}"
    if not fmt_ign_ok(payer):
        return await show(err("That doesn't look like a valid IGN."))
    if payer.lower().lstrip(".") == receiver.lower().lstrip("."):
        return await show(err("The payer can't be the receiver."))

    def busy_reason():
        if channel.id in CHANNEL_WATCH:
            return "Payment is already being tracked in this ticket."
        if payer.lower() in WATCHES:
            return f"A payment from **{payer}** is already being tracked in another ticket."
        if len(WATCHES) >= MAX_BUILD_WATCHES:
            return "Too many payments are being tracked right now. Try again in a minute."
        return None

    reason = busy_reason()
    if reason:
        return await show(err(reason))

    await show(discord.Embed(
        description=f"🔎 Checking if **{payer}** exists… this can take a few seconds. "
                    "You'll be pinged in the ticket when you can pay.",
        color=0x5865F2))

    # does the IGN exist? (the worker's /stats lookup says "user doesn't exist")
    code = await check_ign(db, payer)
    if code == "not_found":
        return await show(err(f"No player named **{payer}** was found. Check the spelling."))
    if code:
        return await show(err(
            f"Couldn't verify that IGN: {ERROR_MESSAGES.get(code, code)}"))

    reason = busy_reason()       # state may have changed during the lookup
    if reason:
        return await show(err(reason))

    # follow the RECEIVER (shared by every build ticket)
    sids = await _sessions(db)
    if not sids:
        return await show(err(
            "Couldn't start: the worker account isn't linked."))
    started, error = None, "not_ready"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        for sid in sids:
            try:
                data = await paced(_get, http, f"/follow-start/{sid}", receiver=receiver, seconds=WATCH_SECONDS + 30)
            except Exception as e:
                logger.warning(f"build pay: follow-start failed: {e}")
                error = "not_ready"
                continue
            if data.get("ok"):
                started = (sid, data)
                break
            error = data.get("error_code") or "error"
            if error != "not_ready":
                break
    if not started:
        return await show(err(f"Couldn't start tracking: {ERRORS.get(error, error)}"))
    sid, data = started

    reason = busy_reason()       # a double click may have started a watch while we were contacting the worker
    if reason:
        return await show(err(reason))
    ends = time.time() + WATCH_SECONDS
    cancel = asyncio.Event()
    key = payer.lower()
    WATCHES[key] = {"channel_id": channel.id, "receiver": receiver, "ends": ends, "cancel": cancel}
    CHANNEL_WATCH[channel.id] = key
    opener = guild.get_member(int(doc["opener_id"]))
    log = await asyncio.to_thread(lambda: db["build_payments"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
        "build_id": str(build["_id"]), "build_name": build["name"],
        "payer": payer, "receiver": receiver, "amount": int(build.get("price") or 0),
        "opener_id": doc["opener_id"], "opener_name": opener.display_name if opener else doc["opener_id"],
        "status": "tracking", "started_at": datetime.now(timezone.utc)}))
    log_id = log.inserted_id

    pay_msg = channel.get_partial_message(int(doc["pay_message_id"])) if doc.get("pay_message_id") else None
    try:
        if pay_msg:
            await pay_msg.edit(embed=pay_embed_waiting(build, payer, receiver, ends), view=WaitingView(custom=_is_custom(build)))
    except discord.HTTPException as e:
        logger.warning(f"build pay: couldn't edit payment message: {e}")
    await show(discord.Embed(
        description=f"✅ **{payer}** was found. Tracking started. Send the payment in-game now.",
        color=0x57F287))
    verified_msg = None
    try:   # public embed (pings the customer) with the pay command, shown only now that the IGN is verified
        verified = discord.Embed(
            title="✅ IGN verified — you can pay now", color=0x57F287,
            description=f"Run this command in-game to pay:\n```/pay {receiver} {short_amount(build.get('price'))}```")
        verified.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
        verified.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
        verified.add_field(name="💰 Amount", value=f"**{fmt_money(build.get('price'))}**", inline=True)
        verified.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
        verified_msg = await channel.send(content=f"<@{doc['opener_id']}>", embed=verified,
                                          allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException as e:
        logger.warning(f"build pay: couldn't post the verified embed: {e}")

    task = asyncio.create_task(watch_payment(
        interaction.client, db, channel, pay_msg, doc, build, cfg, payer, receiver,
        sid, int(data.get("last_id") or 0), ends, cancel, log_id, verified_msg))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_payment(bot, db, channel, pay_msg, doc, build, cfg, payer, receiver,
                        sid, since, ends, cancel, log_id, verified_msg=None):
    key = payer.lower()
    pl, rl = payer.lower().lstrip("."), receiver.lower().lstrip(".")
    value = int(build.get("price") or 0)
    near_misses, misses, paid = [], 0, False

    async def finish(status, **extra):
        try:
            await asyncio.to_thread(lambda: db["build_payments"].update_one(
                {"_id": log_id}, {"$set": {"status": status, "finished_at": datetime.now(timezone.utc), **extra}}))
        except Exception as e:
            logger.warning(f"build pay: couldn't write payment log: {e}")

    async def edit(embed, view):
        if pay_msg is None:
            return
        try:
            await pay_msg.edit(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning(f"build pay: couldn't edit payment message: {e}")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.time() < ends and not paid:
                await asyncio.sleep(POLL_GAP_SECONDS)
                if cancel.is_set():
                    await finish("cancelled")
                    return                       # the Cancel button already reset the message
                try:
                    data = await _get(http, f"/follow-events/{sid}", since=since)
                except Exception as e:
                    misses += 1
                    logger.warning(f"build pay: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    logger.warning("build pay: the follow watch ended on the MC bot side.")
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != pl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(value, float(ev["value"]), float(ev["res"])):
                        paid = True
                        # atomic: only ONE watcher / command may flip the ticket to paid; everyone else stops silently
                        won = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                            {"_id": doc["_id"], "paid": {"$ne": True}},
                            {"$set": {"paid": True, "paid_by": payer, "paid_at": datetime.now(timezone.utc)}}))
                        if not won.modified_count:
                            logger.info(f"build pay: duplicate detection ignored ({ev['raw']})")
                            await asyncio.to_thread(lambda: db["build_payments"].delete_one({"_id": log_id}))
                            break
                        logger.info(f"build pay confirmed: {ev['raw']} (wanted {value})")
                        await release_follow(bot, sid, receiver, ignore_watch_key=key, who="build pay")
                        await finish("paid", server_message=ev["raw"])
                        done = discord.Embed(
                            title="✅ Money Received", color=discord.Color.green(),
                            description=f"**{receiver}** received **{fmt_money(value)}** from **{payer}**!")
                        done.add_field(name="🧱 Build", value=build["name"], inline=True)
                        done.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
                        done.add_field(name="💰 Amount", value=f"**{fmt_money(value)}**", inline=True)
                        done.add_field(name="🧾 Server message", value=f"```{ev['raw']}```", inline=False)
                        await edit(done, None)
                        fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
                        if fresh.get("quote_builder_id") and not fresh.get("claimed_by"):
                            # custom build: payment confirmed -> assign the chosen builder now
                            bid = fresh["quote_builder_id"]
                            try:
                                builder = channel.guild.get_member(int(bid)) or await channel.guild.fetch_member(int(bid))
                            except (discord.HTTPException, ValueError):
                                builder = None
                            if builder:
                                res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                                    {"_id": doc["_id"], "claimed_by": {"$in": [None]}},
                                    {"$set": {"claimed_by": bid}}))
                                if res.modified_count:
                                    await apply_claim(channel.guild, channel, builder, fresh, cfg)
                                    await asyncio.to_thread(lambda: db["build_payments"].update_one(
                                        {"_id": log_id}, {"$set": {"claimed_by": bid, "claimed_by_name": builder.display_name}}))
                                    try:
                                        await channel.send(embed=discord.Embed(
                                            title="🙋 Build claimed", color=0x57F287,
                                            description=(f"{builder.mention} is now assigned to this build.\n"
                                                         "Only the customer, the builder and the build manager can see this ticket now.")),
                                            allowed_mentions=discord.AllowedMentions(users=True))
                                    except discord.HTTPException as e:
                                        logger.warning(f"build pay: couldn't send claim message: {e}")
                                fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or fresh
                        claimer = fresh.get("claimed_by")
                        if claimer:      # custom build: the chosen builder was auto-claimed
                            text = (f"<@{claimer}> **Build Has Been paid**, you can start building! "
                                    f"(customer: <@{fresh['opener_id']}>)")
                            mentions = discord.AllowedMentions(users=True)
                        else:
                            ping = f"<@&{cfg['ping_role_id']}> " if cfg["ping_role_id"] else ""
                            text = f"{ping}**Build Has Been paid** please use `/builder claim`"
                            mentions = discord.AllowedMentions(roles=True)
                        try:
                            await channel.send(text, allowed_mentions=mentions)
                        except discord.HTTPException as e:
                            logger.warning(f"build pay: couldn't send paid message: {e}")
                        await send_payment_log(channel, cfg, fresh, build, payer, receiver, value, ev["raw"])
                        break
                    near_misses.append(ev["raw"])

        if not paid and not cancel.is_set():
            await finish("not_detected", near_misses=near_misses[-3:])
            desc = (f"No payment from **{payer}** to **{receiver}** of about **{fmt_money(value)}** "
                    f"was seen in 5 minutes. Press **Start Pay** to try again.")
            if near_misses:
                desc += "\nI did see a payment between them with a different amount:\n```" + "\n".join(near_misses[-3:]) + "```"
            if misses:
                desc += f"\n({misses} status check(s) failed while watching.)"
            await edit(discord.Embed(title="⌛ Payment not detected", color=discord.Color.red(), description=desc),
                       PayView(custom=_is_custom(build)))
    except Exception as e:
        logger.error(f"build pay watch crashed: {e}", exc_info=True)
        await finish("error")
        await edit(pay_embed_idle(build, cfg), PayView(custom=_is_custom(build)))
    finally:
        if verified_msg is not None:      # the payment embed above already shows the result
            try:
                await verified_msg.delete()
            except discord.HTTPException:
                pass
        WATCHES.pop(key, None)
        if CHANNEL_WATCH.get(channel.id) == key:
            CHANNEL_WATCH.pop(channel.id, None)
        await release_follow(bot, sid, receiver, who="build pay")


async def send_payment_log(channel, cfg, doc, build, payer, receiver, value, raw, forced_by=None):
    """Post a confirmed payment to the payment-log channel set on the dashboard."""
    cid = cfg.get("payment_log_channel_id") or ""
    if not cid.isdigit():
        return
    log_ch = channel.guild.get_channel(int(cid))
    if log_ch is None:
        logger.warning("payment log channel not found (deleted?)")
        return
    e = discord.Embed(title="💰 Payment confirmed", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.add_field(name="🧱 Build", value=build["name"], inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(value)}**", inline=True)
    e.add_field(name="🏷️ Type", value={"custom": "Custom", "digout": "Digout"}.get(doc.get("kind"), "Catalog"), inline=True)
    e.add_field(name="👤 Payer IGN", value=f"`{payer}`", inline=True)
    e.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
    e.add_field(name="🎫 Ticket", value=f"{channel.mention} (`#{channel.name}`)", inline=True)
    e.add_field(name="🙍 Customer", value=f"<@{doc['opener_id']}>", inline=True)
    e.add_field(name="🛠️ Builder", value=f"<@{doc['claimed_by']}>" if doc.get("claimed_by") else "— not claimed yet", inline=True)
    if forced_by is not None:
        e.add_field(name="⚠️ Forced", value=f"Marked as paid by {forced_by.mention}", inline=False)
    else:
        e.add_field(name="🧾 Server message", value=f"```{raw}```"[:1024], inline=False)
    try:
        await log_ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        logger.warning(f"payment log send failed: {ex}")


async def apply_claim(guild: discord.Guild, channel: discord.TextChannel, claimer: discord.Member, doc: dict, cfg: dict):
    """Lock the ticket down to: manager role, the claimer and the opener."""
    keep = {guild.default_role.id, guild.me.id, claimer.id, int(doc["opener_id"])}
    if cfg["manager_role_id"].isdigit():
        keep.add(int(cfg["manager_role_id"]))
    for target in list(channel.overwrites):
        if target.id not in keep:
            try:
                await channel.set_permissions(target, overwrite=None, reason=f"Build claimed by {claimer}")
            except discord.HTTPException as e:
                logger.warning(f"build claim: couldn't remove {target}: {e}")
    try:
        await channel.set_permissions(claimer, overwrite=discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True))
    except discord.HTTPException as e:
        logger.warning(f"build claim: couldn't grant claimer access: {e}")


# ---------------------------------------------------------------------------
# Custom build quotes
# ---------------------------------------------------------------------------

MAX_QUOTES = 25


def quote_embed(opener_id: str, quotes: list, chosen: dict = None) -> discord.Embed:
    if chosen:
        e = discord.Embed(title="✅ Quote accepted", color=0x57F287,
                          description=(f"<@{opener_id}> picked the quote from <@{chosen['builder_id']}>.\n"
                                       f"💰 **{fmt_money(chosen['price'])}**"
                                       + (f"\n📝 {chosen['note']}" if chosen.get("note") else "")))
        return e
    e = discord.Embed(
        title="📋 Quotes", color=COLOR,
        description=("**Builders:** press **Submit Quote** to offer a price (pressing it again updates your quote) "
                     "or **Withdraw Quote** to remove it. Quotes are locked once the customer picks one.\n"
                     f"**<@{opener_id}>:** pick the quote you want from the dropdown."))
    if not quotes:
        e.add_field(name="No quotes yet", value="Waiting for builders…", inline=False)
    for i, q in enumerate(quotes[:MAX_QUOTES], 1):
        val = f"💰 **{fmt_money(q['price'])}**"
        if q.get("note"):
            val += f"\n📝 {q['note']}"
        e.add_field(name=f"{i}. {q.get('builder_name') or 'Builder'}", value=val, inline=True)
    e.set_footer(text="Once you pick a quote, the payment step starts. The builder is assigned after payment is confirmed.")
    return e


class QuoteSelect(discord.ui.Select):
    def __init__(self, quotes: list = None):
        options = [
            discord.SelectOption(
                label=f"{q.get('builder_name') or 'Builder'} — {fmt_money(q['price'])}"[:100],
                value=str(q["_id"]), description=(q.get("note") or "No note")[:100], emoji="🛠️")
            for q in (quotes or [])[:MAX_QUOTES]
        ] or [discord.SelectOption(label="No quotes yet", value="none")]
        super().__init__(placeholder="✅ Choose a quote…", options=options,
                         custom_id="build_quote_pick", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        db, guild, channel = interaction.client.db, interaction.guild, interaction.channel
        value = self.values[0]
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(channel.id), "status": "open", "kind": "custom"}))
        if not doc:
            return await interaction.response.send_message(embed=err("This is not an active custom build ticket."), ephemeral=True)
        if str(interaction.user.id) != doc["opener_id"]:
            return await interaction.response.send_message(
                embed=err("Only the ticket opener can choose a quote."), ephemeral=True)
        if doc.get("quote_id"):
            return await interaction.response.send_message(embed=err("A quote was already chosen."), ephemeral=True)
        try:
            quote = None if value == "none" else await asyncio.to_thread(
                lambda: db["build_quotes"].find_one({"_id": ObjectId(value), "channel_id": str(channel.id)}))
        except Exception:
            quote = None
        if not quote:
            return await interaction.response.send_message(
                embed=err("That quote isn't available anymore."), ephemeral=True)
        try:
            builder = guild.get_member(int(quote["builder_id"])) or await guild.fetch_member(int(quote["builder_id"]))
        except (discord.HTTPException, ValueError):
            builder = None
        if builder is None:
            return await interaction.response.send_message(
                embed=err("That builder is no longer in the server. Pick another quote."), ephemeral=True)

        await interaction.response.defer()
        res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"], "quote_id": {"$in": [None]}},
            {"$set": {"quote_id": str(quote["_id"]), "quote_price": int(quote["price"]),
                      "quote_builder_id": quote["builder_id"],
                      "quote_chosen_at": datetime.now(timezone.utc)}}))
        if not res.modified_count:
            return await interaction.followup.send(embed=err("A quote was already chosen."), ephemeral=True)

        cfg = await asyncio.to_thread(_get_settings, db, guild.id)
        # 1) close the quote panel
        try:
            await interaction.message.edit(embed=quote_embed(doc["opener_id"], [], chosen=quote),
                                           view=None if doc.get("paid") else ChangeQuoteView())
        except discord.HTTPException as e:
            logger.warning(f"quote panel close failed: {e}")
        if doc.get("paid") and doc.get("requote_paid"):
            # already paid before (the first builder left): settle the difference instead of a new payment
            return await settle_requote_pick(guild, channel, db, cfg, doc, quote, builder)
        # 2) the normal payment embed (the builder is only assigned once payment is confirmed), priced at the accepted quote
        build = {"_id": "custom", "name": "Custom Build", "price": int(quote["price"])}
        pay_msg = await channel.send(embed=pay_embed_idle(build, cfg), view=PayView(custom=True))
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"pay_message_id": str(pay_msg.id)}}))


class QuoteModal(discord.ui.Modal, title="Submit your quote"):
    price = discord.ui.TextInput(label="Price", placeholder="e.g. 250k, 1.5m, 2b", min_length=1, max_length=20)
    note = discord.ui.TextInput(label="Note (optional)", placeholder="ETA, materials, anything the customer should know",
                                required=False, max_length=150, style=discord.TextStyle.short)

    async def on_submit(self, interaction: discord.Interaction):
        db, guild, channel, user = interaction.client.db, interaction.guild, interaction.channel, interaction.user
        price = parse_price(self.price.value)
        if price is None:
            return await interaction.response.send_message(
                embed=err("Invalid price. Use something like `250k`, `5m` or `1.5b`."), ephemeral=True)
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(channel.id), "status": "open", "kind": "custom"}))
        if not doc or doc.get("quote_id"):
            return await interaction.response.send_message(
                embed=err("This ticket isn't taking quotes anymore."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, guild.id)
        if not is_ticket_staff(user, cfg):
            return await interaction.response.send_message(
                embed=err("Only builders (allowed roles) can submit quotes."), ephemeral=True)
        if str(user.id) == doc["opener_id"]:
            return await interaction.response.send_message(
                embed=err("You can't quote your own ticket."), ephemeral=True)
        mine = await asyncio.to_thread(lambda: db["build_quotes"].find_one(
            {"channel_id": str(channel.id), "builder_id": str(user.id)}))
        if not mine:
            count = await asyncio.to_thread(lambda: db["build_quotes"].count_documents({"channel_id": str(channel.id)}))
            if count >= MAX_QUOTES:
                return await interaction.response.send_message(
                    embed=err(f"This ticket already has {MAX_QUOTES} quotes."), ephemeral=True)
        await asyncio.to_thread(lambda: db["build_quotes"].update_one(
            {"channel_id": str(channel.id), "builder_id": str(user.id)},
            {"$set": {"guild_id": str(guild.id), "builder_name": user.display_name, "price": price,
                      "note": str(self.note.value or "").strip(), "updated_at": datetime.now(timezone.utc)},
             "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True))
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$pull": {"quote_declined_by": str(user.id)}}))
        await interaction.response.send_message(
            embed=discord.Embed(description=f"✅ Your quote of **{fmt_money(price)}** was {'updated' if mine else 'submitted'}.",
                                color=0x57F287), ephemeral=True)
        await refresh_quote_panel(channel, db, doc)


class SubmitQuoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Submit Quote", emoji="📝", style=discord.ButtonStyle.primary,
                         custom_id="build_quote_submit")

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open", "kind": "custom"}))
        if not doc or doc.get("quote_id"):
            return await interaction.response.send_message(
                embed=err("This ticket isn't taking quotes anymore."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if not is_ticket_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only builders (allowed roles) can submit quotes."), ephemeral=True)
        if str(interaction.user.id) == doc["opener_id"]:
            return await interaction.response.send_message(
                embed=err("You can't quote your own ticket."), ephemeral=True)
        await interaction.response.send_modal(QuoteModal())


class WithdrawQuoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Withdraw Quote", emoji="🗑️", style=discord.ButtonStyle.secondary,
                         custom_id="build_quote_withdraw")

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open", "kind": "custom"}))
        if not doc or doc.get("quote_id"):
            return await interaction.response.send_message(
                embed=err("Quotes are locked. Ask the build manager if the price must change."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if not is_ticket_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only builders (allowed roles) can withdraw quotes."), ephemeral=True)
        res = await asyncio.to_thread(lambda: db["build_quotes"].delete_one(
            {"channel_id": str(interaction.channel.id), "builder_id": str(interaction.user.id)}))
        if not res.deleted_count:
            return await interaction.response.send_message(embed=err("You don't have a quote on this ticket."), ephemeral=True)
        await interaction.response.send_message(
            embed=discord.Embed(description="🗑️ Your quote was withdrawn.", color=0x57F287), ephemeral=True)
        await refresh_quote_panel(interaction.channel, db, doc)


class QuotePanelView(discord.ui.View):
    def __init__(self, quotes: list = None, register: bool = False):
        super().__init__(timeout=None)
        self.add_item(SubmitQuoteButton())
        self.add_item(WithdrawQuoteButton())
        if quotes or register:      # register=True: used once at startup so both components are routable
            self.add_item(QuoteSelect(quotes))


class ChangeQuoteButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Change Quote", emoji="🔄", style=discord.ButtonStyle.secondary,
                         custom_id="build_quote_change")

    async def callback(self, interaction: discord.Interaction):
        db, guild, channel, user = interaction.client.db, interaction.guild, interaction.channel, interaction.user
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(channel.id), "status": "open", "kind": "custom"}))
        if not doc or not doc.get("quote_id"):
            return await interaction.response.send_message(embed=err("There's no accepted quote to change."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, guild.id)
        if str(user.id) != doc["opener_id"] and not is_manager(user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the ticket opener or the build manager can change the quote."), ephemeral=True)
        problem = quote_reset_blocker(doc)
        if problem:
            return await interaction.response.send_message(embed=err(problem), ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        ok, msg = await reset_quote(db, guild, channel, doc, user)
        await interaction.followup.send(
            embed=discord.Embed(description=("✅ " if ok else "❌ ") + msg, color=0x57F287 if ok else 0xED4245),
            ephemeral=True)


class ChangeQuoteView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ChangeQuoteButton())


def quote_reset_blocker(doc: dict):
    """Why the accepted quote can't be reset right now (None = it can)."""
    if doc.get("paid"):
        return "This ticket is already paid. Ask the build manager to use `/bm price set` instead."
    if doc.get("claimed_by"):
        return "A builder is already assigned. Ask the build manager."
    return None


async def reset_quote(db, guild: discord.Guild, channel: discord.TextChannel, doc: dict, by):
    """Undo the customer's quote pick (before payment): cancel payment tracking, drop the payment embed,
    reopen the quotes panel with every quote kept. Returns (ok, message)."""
    key = CHANNEL_WATCH.get(channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
        await asyncio.sleep(1.5)            # let the watch task wind down
    old_price = doc.get("quote_price")
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "paid": {"$ne": True}, "quote_id": {"$nin": [None]}},
        {"$unset": {"quote_id": "", "quote_price": "", "quote_builder_id": "", "quote_chosen_at": "",
                    "pay_message_id": "", "price_override": ""},
         "$push": {"price_history": {"action": "quote_reset", "old": old_price, "new": None,
                                     "by": str(by.id), "at": datetime.now(timezone.utc)}}}))
    if not res.modified_count:
        return False, "The quote couldn't be reset (the ticket may have just been paid)."
    if doc.get("pay_message_id"):
        try:
            await channel.get_partial_message(int(doc["pay_message_id"])).delete()
        except discord.HTTPException as e:
            logger.warning(f"reset quote: couldn't delete the payment embed: {e}")
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    await refresh_quote_panel(channel, db, fresh)
    try:
        await channel.send(embed=discord.Embed(
            title="🔄 Quote reset", color=0xFEE75C,
            description=(f"{by.mention} reset the accepted quote. <@{doc['opener_id']}>, please pick a quote again "
                         "from the panel above.")),
            allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass
    return True, "Quote reset. The customer can pick again."


_QUOTE_KEYS = ("quote_id", "quote_price", "quote_builder_id", "quote_chosen_at", "price_override")


async def _reopen_quote_panel(db, channel: discord.TextChannel, doc: dict):
    """Clear the chosen quote and re-render the open quote panel (every other quote stays)."""
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$unset": {k: "" for k in _QUOTE_KEYS}}))
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    await refresh_quote_panel(channel, db, fresh)


async def reopen_quotes_after_release(guild: discord.Guild, channel: discord.TextChannel, db, doc: dict,
                                      cfg: dict, old_builder_id: str, by=None) -> bool:
    """A builder of a PAID custom build can't do it and left: reopen the quote panel so the customer can pick
    another builder. What the customer already paid is remembered (requote_paid): the new pick is settled
    against it (extra payment / refund of the difference) before the new builder is assigned.
    Returns True when the quotes were reopened (False = not a paid custom build, nothing done)."""
    if doc.get("kind") != "custom" or not doc.get("paid") or not doc.get("quote_id") or doc.get("finished"):
        return False
    build = await asyncio.to_thread(ticket_build, db, doc)
    paid_amount = int(doc.get("requote_paid") or (build or {}).get("price") or doc.get("quote_price") or 0)
    if paid_amount <= 0:
        return False
    # a pending price change belonged to the builder who left: drop it
    if doc.get("adjust_status"):
        key = CHANNEL_WATCH.get(channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
            await asyncio.sleep(1.5)
        if doc.get("adjust_message_id"):
            try:
                await channel.get_partial_message(int(doc["adjust_message_id"])).edit(
                    content=None, view=None,
                    embed=discord.Embed(title="🚫 Price change cancelled", color=0x99AAB5,
                                        description="The builder left, so this price change was dropped."))
            except discord.HTTPException:
                pass
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$set": {"requote_paid": paid_amount},
         "$addToSet": {"quote_declined_by": str(old_builder_id)},
         "$unset": {k: "" for k in _ADJUST_KEYS},
         "$push": {"price_history": {"action": "builder_left", "old": paid_amount, "new": None,
                                     "by": str(by.id) if by else str(old_builder_id), "at": datetime.now(timezone.utc)}}}))
    await asyncio.to_thread(lambda: db["build_quotes"].delete_one(
        {"channel_id": str(channel.id), "builder_id": str(old_builder_id)}))
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    await _reopen_quote_panel(db, channel, fresh)
    ping = f"<@&{cfg['ping_role_id']}> " if cfg["ping_role_id"] else ""
    try:
        await channel.send(
            f"{ping}<@{doc['opener_id']}> <@{old_builder_id}> can't do this build, so the quotes are **open again**.\n"
            f"The customer already paid **{fmt_money(paid_amount)}**. Builders: submit a quote with **Submit Quote**. "
            f"<@{doc['opener_id']}>: pick one from the panel. If the new price is higher you pay the difference, "
            "if it's lower the difference is paid back to you.",
            allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    except discord.HTTPException:
        pass
    return True


async def settle_requote_pick(guild, channel, db, cfg, doc: dict, quote: dict, builder):
    """The customer picked a new quote on an already-paid ticket: compare with what they paid.
    Same -> builder assigned now. Higher -> customer pays the extra. Lower -> the build manager pays back the
    difference. In both of the last cases the builder is assigned when the difference is settled."""
    paid_amount = int(doc.get("requote_paid") or 0)
    new_price = int(quote["price"])
    delta = new_price - paid_amount
    if delta == 0:
        res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"], "claimed_by": {"$in": [None]}},
            {"$set": {"claimed_by": str(builder.id)}, "$unset": {"requote_paid": ""}}))
        if res.modified_count:
            await asyncio.to_thread(lambda: db["build_payments"].update_many(
                {"channel_id": str(channel.id), "status": "paid"},
                {"$set": {"claimed_by": str(builder.id), "claimed_by_name": builder.display_name}}))
            await apply_claim(guild, channel, builder, doc, cfg)
            await channel.send(
                f"{builder.mention} **Build Has Been paid**, you can start building! (customer: <@{doc['opener_id']}>)\n"
                "Same price as before, so nothing more to pay.",
                allowed_mentions=discord.AllowedMentions(users=True))
        return
    mode = "more" if delta > 0 else "less"
    fields = {"adjust_status": "pending", "adjust_dir": mode, "adjust_delta": abs(delta), "adjust_old": paid_amount,
              "adjust_new": new_price, "adjust_build_name": "Custom Build"}
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "adjust_status": {"$exists": False}}, {"$set": fields}))
    if not res.modified_count:
        return
    full = {**doc, **fields, "quote_price": new_price}
    if mode == "more":
        content, mentions = f"<@{doc['opener_id']}>", discord.AllowedMentions(users=True)
    else:
        content = f"<@&{cfg['manager_role_id']}>" if cfg["manager_role_id"] else None
        mentions = discord.AllowedMentions(roles=True)
    msg = await channel.send(content=content, embed=adjust_embed(full, cfg), view=AdjustView(), allowed_mentions=mentions)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"adjust_message_id": str(msg.id)}}))


async def open_quotes(channel: discord.TextChannel, db, doc: dict, cfg: dict, headline: str):
    """Ping the builders and post the quote panel of a custom build ticket (file received or bypassed)."""
    ping = f"<@&{cfg['ping_role_id']}> " if cfg["ping_role_id"] else ""
    await channel.send(f"{ping}{headline}\nBuilders, please submit your quotes below.",
                       allowed_mentions=discord.AllowedMentions(roles=True, users=True))
    panel = await channel.send(embed=quote_embed(str(doc["opener_id"]), []), view=QuotePanelView())
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"quote_message_id": str(panel.id)}}))


async def refresh_quote_panel(channel: discord.TextChannel, db, doc: dict):
    """Re-render the quote panel with the current quotes."""
    if not doc.get("quote_message_id"):
        return
    quotes = await asyncio.to_thread(lambda: list(
        db["build_quotes"].find({"channel_id": str(channel.id)}).sort("created_at", 1)))
    try:
        await channel.get_partial_message(int(doc["quote_message_id"])).edit(
            embed=quote_embed(doc["opener_id"], quotes), view=QuotePanelView(quotes))
    except discord.HTTPException as e:
        logger.warning(f"quote panel refresh failed: {e}")


async def _ticket_context(interaction: discord.Interaction):
    """(ticket_doc, cfg) if this is an open build ticket and the user is staff; else replies and None."""
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(interaction.channel.id), "status": "open"}))
    if not doc:
        await interaction.response.send_message(embed=err("This is not an active build ticket."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_ticket_staff(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("You don't have permission to use this button."), ephemeral=True)
        return None
    return doc, cfg


PAID_CLOSE_MSG = ("This ticket is already **paid**, so it can't be closed. "
                  "Use `/builder finish` - it closes by itself once the builder is paid.")


async def close_build_ticket(interaction: discord.Interaction, doc: dict):
    """Close + delete a build ticket (button and /builder close). Refuses once the ticket is paid."""
    db = interaction.client.db
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    if fresh.get("paid"):
        return await interaction.response.send_message(embed=err(PAID_CLOSE_MSG), ephemeral=True)
    key = CHANNEL_WATCH.get(interaction.channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "paid": {"$ne": True}},
        {"$set": {"status": "closed", "closed_by": str(interaction.user.id)}}))
    if not res.modified_count:           # got paid in the meantime
        return await interaction.response.send_message(embed=err(PAID_CLOSE_MSG), ephemeral=True)
    await interaction.response.send_message(
        embed=discord.Embed(description=f"🔒 Ticket closed by {interaction.user.mention}. Deleting in 5 seconds…",
                            color=0xED4245),
        allowed_mentions=discord.AllowedMentions.none())
    await asyncio.sleep(5)
    try:
        await interaction.channel.delete(reason=f"Build ticket closed by {interaction.user}")
    except discord.HTTPException:
        pass


class CloseBuildButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                         custom_id="build_ticket_close")

    async def callback(self, interaction: discord.Interaction):
        ctx = await _ticket_context(interaction)
        if not ctx:
            return
        doc, _ = ctx
        await close_build_ticket(interaction, doc)


# ---------------------------------------------------------------------------
# Finish: customer confirmation (DM) -> review -> finished category -> builder payout
# ---------------------------------------------------------------------------

PAYOUT_FEE_PERCENT = 15


def clamp_fee(v) -> int:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return PAYOUT_FEE_PERCENT


def get_fee(db, guild_id) -> int:
    """This server's builder fee % (set on the dashboard; default PAYOUT_FEE_PERCENT)."""
    d = db["build_settings"].find_one({"_id": str(guild_id)}) or {}
    return clamp_fee(d.get("fee_percent")) if d.get("fee_percent") not in (None, "") else PAYOUT_FEE_PERCENT


def payout_amount(price, fee=None) -> int:
    """What the builder gets: the build price minus the fee (the server's fee % when given)."""
    fee = PAYOUT_FEE_PERCENT if fee is None else clamp_fee(fee)
    return int(int(price or 0) * (100 - fee) // 100)


def stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


async def _dm_ticket(interaction: discord.Interaction, statuses: tuple):
    """(ticket_doc, guild) for the DM message this button/select lives on, or replies and None."""
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"finish_dm_message_id": str(interaction.message.id), "status": "open"}))
    if not doc or str(interaction.user.id) != doc["opener_id"] or doc.get("finish_status") not in statuses:
        await interaction.response.send_message(
            embed=err("This request is outdated or was already answered."), ephemeral=True)
        return None
    guild = interaction.client.get_guild(int(doc["guild_id"]))
    if guild is None:
        await interaction.response.send_message(embed=err("I can't reach that server right now."), ephemeral=True)
        return None
    return doc, guild


REVIEW_HOURS = 24
AUTO_REVIEW_TEXT = "W Service Fast and Easy"


def begin_finish(db, ticket_id, expect_status=None, auto_review: bool = False) -> bool:
    """Atomically mark the ticket finished. False if it was already finished.
    Normal finish: opens the 24h review window. auto_review (force-finish): the 5-star review is
    recorded right away, so there is no pending review and the 24h timer never touches it."""
    flt = {"_id": ticket_id, "finished": {"$ne": True}}
    if expect_status:
        flt["finish_status"] = expect_status
    now = datetime.now(timezone.utc)
    fields = {"finish_status": "done", "finished": True, "finished_at": now}
    if auto_review:
        fields.update({"review_status": "auto", "review_rating": 5})
    else:
        fields.update({"review_status": "pending", "review_due_at": now + timedelta(hours=REVIEW_HOURS)})
    res = db["build_tickets"].update_one(flt, {"$set": fields})
    return bool(res.modified_count)


def review_prompt_embed(forced: bool = False) -> discord.Embed:
    return discord.Embed(
        title="⭐ Rate your build (optional)", color=COLOR,
        description=("Your build is finished" + (" (marked as delivered by the build manager)" if forced else "") +
                     ".\nYou can pick a rating from **1 to 5** and write a short review below.\n\n"
                     f"Not interested? No problem - if there's no review within **{REVIEW_HOURS} hours** "
                     "we'll leave a 5★ review for you."))


class FinishConfirmView(discord.ui.View):
    """Persistent: the Yes / No buttons in the customer's DM."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Yes, I got it", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="build_finish_yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await _dm_ticket(interaction, ("awaiting_confirm",))
        if not ctx:
            return
        doc, guild = ctx
        db = interaction.client.db
        if not await asyncio.to_thread(begin_finish, db, doc["_id"], "awaiting_confirm"):
            return await interaction.response.send_message(embed=err("Already answered."), ephemeral=True)
        # the review is optional: the build is finished right now
        await interaction.response.edit_message(embed=review_prompt_embed(), view=ReviewView())
        try:
            fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
            cfg = await asyncio.to_thread(_get_settings, db, guild.id)
            build = await asyncio.to_thread(ticket_build, db, fresh)
            bm = guild.get_member(int(fresh["claimed_by"])) if fresh.get("claimed_by") else None
            await asyncio.to_thread(ledger_record_finish, db, fresh, build, None, bm.display_name if bm else "")
            await finalize_finish(guild, db, fresh, cfg)
        except Exception as e:
            logger.error(f"finish: finalize crashed: {e}", exc_info=True)

    @discord.ui.button(label="No, not yet", emoji="❌", style=discord.ButtonStyle.danger,
                       custom_id="build_finish_no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await _dm_ticket(interaction, ("awaiting_confirm",))
        if not ctx:
            return
        doc, guild = ctx
        db = interaction.client.db
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"finish_status": None}}))
        await interaction.response.edit_message(
            embed=discord.Embed(description="👍 Thanks, I've let the builder know you haven't received it yet.",
                                color=0xFEE75C), view=None)
        channel = guild.get_channel(int(doc["channel_id"]))
        if channel is not None:
            cfg = await asyncio.to_thread(_get_settings, db, guild.id)
            mgr = f" <@&{cfg['manager_role_id']}>" if cfg["manager_role_id"] else ""
            try:
                await channel.send(
                    f"⚠️ <@{doc.get('claimed_by')}>{mgr} — <@{doc['opener_id']}> says they **haven't received the build yet**. "
                    "Please sort it out, then run `/builder finish` again.",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True))
            except discord.HTTPException as e:
                logger.warning(f"finish: couldn't post 'not received': {e}")


class ReviewSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="⭐ Choose a rating (1-5)…", custom_id="build_review_rating", min_values=1, max_values=1,
            options=[discord.SelectOption(label=f"{n} / 5", value=str(n), description=stars(n)) for n in range(5, 0, -1)])

    async def callback(self, interaction: discord.Interaction):
        ctx = await _dm_ticket(interaction, ("done",))
        if not ctx:
            return
        doc, _ = ctx
        if doc.get("review_status") != "pending":
            return await interaction.response.send_message(
                embed=err("A review was already left for this build."), ephemeral=True)
        await interaction.response.send_modal(ReviewModal(int(self.values[0]), doc["_id"]))


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ReviewSelect())


async def post_review(guild: discord.Guild, db, doc: dict, rating: int, review: str, auto: bool = False):
    """Save a review, fill the builder's tracker rating and post it to the review channel."""
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    build = await asyncio.to_thread(ticket_build, db, doc)
    build_name = build["name"] if build else "Build"
    await asyncio.to_thread(lambda: db["build_reviews"].insert_one({
        "guild_id": str(guild.id), "channel_id": doc["channel_id"], "customer_id": doc["opener_id"],
        "builder_id": doc.get("claimed_by"), "build_name": build_name, "rating": rating,
        "review": review, "auto": auto, "created_at": datetime.now(timezone.utc)}))
    try:
        await asyncio.to_thread(ledger_set_rating, db, doc, rating)
    except Exception as e:
        logger.warning(f"builder ledger (rating) failed: {e}")
    rch = guild.get_channel(int(cfg["review_channel_id"])) if cfg["review_channel_id"].isdigit() else None
    if rch is None:
        logger.warning("review channel not set/found - review saved to the database only")
        return
    e = discord.Embed(title="⭐ New build review" + (" (auto)" if auto else ""), color=0xFEE75C, description=review,
                      timestamp=datetime.now(timezone.utc))
    e.add_field(name="Rating", value=f"{stars(rating)} ({rating}/5)", inline=False)
    e.add_field(name="🧱 Build", value=build_name, inline=True)
    e.add_field(name="🙍 Customer", value=f"<@{doc['opener_id']}>", inline=True)
    e.add_field(name="🛠️ Builder", value=f"<@{doc.get('claimed_by')}>", inline=True)
    if auto:
        e.set_footer(text=f"Automatic review: the customer didn't review within {REVIEW_HOURS} hours")
    try:
        await rch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        logger.warning(f"review post failed: {ex}")


class ReviewModal(discord.ui.Modal, title="Write your review"):
    text = discord.ui.TextInput(label="Your review", style=discord.TextStyle.paragraph,
                                placeholder="How was the build and the builder?", min_length=3, max_length=800)

    def __init__(self, rating: int, ticket_id):
        super().__init__()
        self.rating, self.ticket_id = rating, ticket_id

    async def on_submit(self, interaction: discord.Interaction):
        db = interaction.client.db
        await interaction.response.defer()
        res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": self.ticket_id, "review_status": "pending"},
            {"$set": {"review_status": "submitted", "review_rating": self.rating}}))
        if not res.modified_count:
            return await interaction.followup.send(
                embed=err("A review was already left for this build."), ephemeral=True)
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": self.ticket_id}))
        guild = interaction.client.get_guild(int(doc["guild_id"]))
        review = str(self.text.value).strip()
        try:
            await interaction.edit_original_response(
                embed=discord.Embed(title="💚 Thank you!", color=0x57F287,
                                    description=f"{stars(self.rating)}\n\n{review}\n\nYour review was submitted."),
                view=None)
        except discord.HTTPException:
            pass
        if guild is None:
            return
        await post_review(guild, db, doc, self.rating, review)


# ---------------------------------------------------------------------------
# Builder tracker (builder_ledger): one entry per finished build, editable on the dashboard
# ---------------------------------------------------------------------------

def ledger_record_finish(db, doc: dict, build, rating, builder_name: str = ""):
    """Create the builder's ledger entry when the customer confirms + rates. Never overwrites
    an existing entry (so manual dashboard edits are kept)."""
    if not doc.get("claimed_by"):
        return
    price = int(build.get("price") or 0) if build else 0
    db["builder_ledger"].update_one(
        {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"])},
        {"$setOnInsert": {
            "builder_id": doc["claimed_by"], "builder_name": builder_name,
            "build_name": build["name"] if build else "Build", "worth": price,
            "received": payout_amount(price, get_fee(db, doc["guild_id"])), "rating": rating, "paid": False,
            "source": "auto", "created_at": datetime.now(timezone.utc)}},
        upsert=True)


def ledger_set_rating(db, doc: dict, rating: int):
    """Fill in the rating of the ticket's ledger entry (only if it has none, so manual edits stay)."""
    db["builder_ledger"].update_one(
        {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"]), "rating": None}, {"$set": {"rating": rating}})


def ledger_mark_paid(db, doc: dict, amount: int):
    """The builder payout was confirmed: flag the ticket's ledger entry as paid (creating it if missing)."""
    if not doc.get("claimed_by"):
        return
    price = int(doc.get("payout_price") or 0)
    now = datetime.now(timezone.utc)
    db["builder_ledger"].update_one(
        {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"])},
        {"$set": {"paid": True, "paid_at": now},
         "$setOnInsert": {
             "builder_id": doc["claimed_by"], "builder_name": "",
             "build_name": doc.get("payout_build_name") or "Build", "worth": price,
             "received": int(amount), "rating": doc.get("review_rating"),
             "source": "auto", "created_at": now}},
        upsert=True)
    # The owner's share is fixed now (at today's owner %) and only for builds whose builder was paid.
    try:
        pct = get_owner_pct(db, doc["guild_id"])
        share = price * pct // 100 if pct and price > 0 else 0
        if share > 0:
            db["builder_ledger"].update_one(
                {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"]), "owner_share": {"$exists": False}},
                {"$set": {"owner_pct": pct, "owner_share": share}})
    except Exception as e:
        logger.warning(f"owner share (ledger) failed: {e}")


async def finalize_finish(guild: discord.Guild, db, doc: dict, cfg: dict, forced_by=None):
    """Remove the customer from the ticket, move it to the finished category, start the builder payout.
    Every step is isolated: a failure is reported in the ticket instead of silently stopping the flow."""
    channel = guild.get_channel(int(doc["channel_id"]))
    if channel is None:
        return
    problems = []
    # 1) the customer loses access to the ticket
    try:
        for target in list(channel.overwrites):
            if getattr(target, "id", None) == int(doc["opener_id"]):
                await channel.set_permissions(target, overwrite=None, reason="Build finished")
    except Exception as e:
        logger.error(f"finish: couldn't remove the customer: {e}", exc_info=True)
        problems.append(f"couldn't remove the customer from the ticket ({e})")
    # 2) move to the finished category (created if missing), then tidy the old category
    try:
        old_cat = channel.category_id
        if not _category_name(cfg, "finished_category") and not cfg.get("finished_category_id"):
            problems.append("no **finished category** is set in the dashboard")
        else:
            for attempt in range(2):
                fin = await resolve_category(guild, db, cfg, "finished_category")
                if fin is None:
                    problems.append("I couldn't find or create the finished category (check my **Manage Channels** permission)")
                    break
                if old_cat == fin.id:
                    break
                try:
                    await channel.edit(category=fin, reason="Build finished")
                except discord.NotFound:
                    if attempt == 1:       # the (empty) category was auto-deleted under us - retry once
                        raise
                    continue
                if old_cat:
                    await delete_category_if_empty(guild, db, old_cat)
                break
    except discord.Forbidden:
        logger.error("finish: no permission to move the ticket", exc_info=True)
        problems.append("I don't have permission to move the ticket (I need **Manage Channels** on the finished category)")
    except Exception as e:
        logger.error(f"finish: couldn't move the ticket: {e}", exc_info=True)
        problems.append(f"couldn't move the ticket to the finished category ({e})")
    # 3) announce + payout
    try:
        who = (f"Marked as delivered by {forced_by.mention} (build manager)." if forced_by
               else f"<@{doc['opener_id']}> confirmed delivery.")
        desc = f"{who}\nThe customer has been removed from this ticket."
        if problems:
            desc += "\n\n⚠️ " + "\n⚠️ ".join(problems)
        await channel.send(embed=discord.Embed(title="✅ Build finished", color=0x57F287, description=desc),
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    try:
        fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
        await start_payout(guild, db, channel, fresh, cfg)
    except Exception as e:
        logger.error(f"finish: payout step failed: {e}", exc_info=True)
        try:
            await channel.send(f"⚠️ Couldn't start the payout step ({e}).")
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Builder IGN (permanent) + payout to the builder (price - fee), paid by the build manager
# ---------------------------------------------------------------------------

async def get_builder_ign(db, guild_id, user_id) -> str:
    d = await asyncio.to_thread(lambda: db["builder_igns"].find_one(
        {"guild_id": str(guild_id), "user_id": str(user_id)}))
    return (d or {}).get("ign") or ""


def payout_embed(doc: dict) -> discord.Embed:
    price = int(doc.get("payout_price") or 0)
    e = discord.Embed(
        title="💸 Pay the builder", color=discord.Color.from_rgb(255, 170, 0),
        description=(f"<@{doc.get('claimed_by')}> finished **{doc.get('payout_build_name') or 'the build'}**.\n"
                     "Press **Pay Now** to send the builder their share."))
    e.add_field(name="🛠️ Builder", value=f"<@{doc.get('claimed_by')}>", inline=True)
    e.add_field(name="🎮 Builder IGN", value=f"`{doc.get('builder_ign')}`", inline=True)
    e.add_field(name="🧱 Build price", value=fmt_money(price), inline=True)
    e.add_field(name=f"➖ Fee ({doc.get('payout_fee_percent', PAYOUT_FEE_PERCENT)}%)", value=fmt_money(price - int(doc.get('payout_amount') or 0)), inline=True)
    e.add_field(name="💰 Amount to pay", value=f"**{fmt_money(doc.get('payout_amount'))}**", inline=True)
    return e


def payout_waiting_embed(doc: dict, sender: str, ends: float) -> discord.Embed:
    e = discord.Embed(
        title="⏳ Waiting for the builder payout", color=discord.Color.from_rgb(255, 170, 0),
        description=f"Run this command in-game to pay:\n```/pay {doc['builder_ign']} {short_amount(doc.get('payout_amount'))}```")
    e.add_field(name="👤 Sender", value=f"`{sender}`", inline=True)
    e.add_field(name="🎯 Builder", value=f"`{doc['builder_ign']}`", inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(doc.get('payout_amount'))}**", inline=True)
    e.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
    e.set_footer(text="Waiting for the payment to show up in-game…")
    return e


async def post_payout_embed(guild: discord.Guild, db, channel, doc: dict, cfg: dict, builder_ign: str):
    build = await asyncio.to_thread(ticket_build, db, doc)
    price = int(build.get("price") or 0) if build else 0
    amount = payout_amount(price, cfg["fee_percent"])
    if amount <= 0:
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"payout_status": "none"}}))
        return await channel.send("ℹ️ This build has no price, so there's nothing to pay out. Close the ticket when ready.")
    fields = {"builder_ign": builder_ign, "payout_amount": amount, "payout_price": price,
              "payout_build_name": build["name"], "payout_status": "pending",
              "payout_fee_percent": cfg["fee_percent"]}
    full = {**doc, **fields}
    mgr = f"<@&{cfg['manager_role_id']}>" if cfg["manager_role_id"] else None
    msg = await channel.send(content=mgr, embed=payout_embed(full), view=PayoutView(),
                             allowed_mentions=discord.AllowedMentions(roles=True))
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {**fields, "payout_message_id": str(msg.id)}}))


async def start_payout(guild: discord.Guild, db, channel, doc: dict, cfg: dict):
    ign = await get_builder_ign(db, guild.id, doc.get("claimed_by"))
    if ign:
        return await post_payout_embed(guild, db, channel, doc, cfg, ign)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"payout_status": "awaiting_ign"}}))
    try:
        await channel.send(
            f"💬 <@{doc.get('claimed_by')}> — you haven't set your IGN yet. Run **`/builder ign set`** "
            "so the build manager can pay you.",
            allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass


class PayoutView(discord.ui.View):
    """Persistent: Pay Now on the payout embed (build manager / admins only)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Pay Now", emoji="💸", style=discord.ButtonStyle.success,
                       custom_id="build_payout_pay")
    async def pay(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        if not doc or doc.get("payout_status") != "pending":
            return await interaction.response.send_message(
                embed=err("There's no pending payout in this ticket."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if not is_manager(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the build manager can pay the builder."), ephemeral=True)
        if interaction.channel.id in CHANNEL_WATCH:
            return await interaction.response.send_message(
                embed=err("A payment is already being tracked in this ticket."), ephemeral=True)
        await interaction.response.send_modal(PayoutModal())


class PayoutWaitingView(discord.ui.View):
    """Persistent: Cancel while a payout is being tracked."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger,
                       custom_id="build_payout_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if not doc or not is_manager(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the build manager can cancel this."), ephemeral=True)
        key = CHANNEL_WATCH.get(interaction.channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"], "payout_status": "tracking"}, {"$set": {"payout_status": "pending"}}))
        await interaction.response.edit_message(embed=payout_embed(doc), view=PayoutView())


class PayoutModal(discord.ui.Modal, title="Pay the builder"):
    ign = discord.ui.TextInput(label="Sender IGN (the account paying)", placeholder="Your in-game name",
                               min_length=1, max_length=25, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await run_payout(interaction, str(self.ign.value).strip())


async def run_payout(interaction: discord.Interaction, sender: str):
    """Verify the sender IGN, follow the builder's IGN and track the payout. Interaction already deferred."""
    db, guild, channel = interaction.client.db, interaction.guild, interaction.channel

    cmd_ref = [None]        # set as soon as receiver + amount are known

    async def show(embed):
        embed = add_pay_cmd(embed, cmd_ref[0])
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"payout: couldn't update private message: {e}")

    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"}))
    if not doc or doc.get("payout_status") != "pending":
        return await show(err("There's no pending payout in this ticket."))
    amount = int(doc.get("payout_amount") or 0)
    receiver = await get_builder_ign(db, guild.id, doc.get("claimed_by"))   # live: edits made on the dashboard apply
    if not receiver:
        return await show(err("The builder has no IGN saved anymore. Ask them to run `/builder ign set`."))
    if amount <= 0:
        return await show(err("This payout isn't set up correctly."))
    cmd_ref[0] = f"/pay {receiver} {short_amount(amount)}"
    if receiver != doc.get("builder_ign"):
        doc["builder_ign"] = receiver
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"builder_ign": receiver}}))
    if not fmt_ign_ok(sender):
        return await show(err("That doesn't look like a valid IGN."))
    if sender.lower().lstrip(".") == receiver.lower().lstrip("."):
        return await show(err("The sender can't be the builder."))

    def busy_reason():
        if channel.id in CHANNEL_WATCH:
            return "A payment is already being tracked in this ticket."
        if sender.lower() in WATCHES:
            return f"A payment from **{sender}** is already being tracked in another ticket."
        if len(WATCHES) >= MAX_BUILD_WATCHES:
            return "Too many payments are being tracked right now. Try again in a minute."
        return None

    reason = busy_reason()
    if reason:
        return await show(err(reason))
    await show(discord.Embed(description=f"🔎 Checking if **{sender}** exists… this can take a few seconds.",
                             color=0x5865F2))
    code = await check_ign(db, sender)
    if code == "not_found":
        return await show(err(f"No player named **{sender}** was found. Check the spelling."))
    if code:
        return await show(err(f"Couldn't verify that IGN: {ERROR_MESSAGES.get(code, code)}"))
    reason = busy_reason()
    if reason:
        return await show(err(reason))

    sids = await _sessions(db)
    if not sids:
        return await show(err("Couldn't start: the worker account isn't linked."))
    started, error = None, "not_ready"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        for sid in sids:
            try:
                data = await paced(_get, http, f"/follow-start/{sid}", receiver=receiver, seconds=WATCH_SECONDS + 30)
            except Exception as e:
                logger.warning(f"payout: follow-start failed: {e}")
                error = "not_ready"
                continue
            if data.get("ok"):
                started = (sid, data)
                break
            error = data.get("error_code") or "error"
            if error != "not_ready":
                break
    if not started:
        return await show(err(f"Couldn't start tracking: {ERRORS.get(error, error)}"))
    sid, data = started

    ends = time.time() + WATCH_SECONDS
    cancel = asyncio.Event()
    key = sender.lower()
    WATCHES[key] = {"channel_id": channel.id, "receiver": receiver, "ends": ends, "cancel": cancel}
    CHANNEL_WATCH[channel.id] = key
    log = await asyncio.to_thread(lambda: db["build_payouts"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
        "builder_id": doc.get("claimed_by"), "builder_ign": receiver, "sender": sender, "amount": amount,
        "build_name": doc.get("payout_build_name"), "status": "tracking", "started_at": datetime.now(timezone.utc)}))
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"payout_status": "tracking"}}))

    waiting = payout_waiting_embed(doc, sender, ends)
    pay_msg = channel.get_partial_message(int(doc["payout_message_id"])) if doc.get("payout_message_id") else None
    edited = False
    if pay_msg is not None:
        try:
            await pay_msg.edit(content=None, embed=waiting, view=PayoutWaitingView())
            edited = True
        except discord.HTTPException as e:
            logger.warning(f"payout: couldn't edit the payout message, posting a new one: {e}")
    if not edited:
        try:
            pay_msg = await channel.send(embed=waiting, view=PayoutWaitingView())
            await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                {"_id": doc["_id"]}, {"$set": {"payout_message_id": str(pay_msg.id)}}))
        except discord.HTTPException as e2:
            logger.warning(f"payout: couldn't post the waiting embed: {e2}")
            pay_msg = None
    await show(discord.Embed(
        title=f"✅ {sender} was found — send the payment now",
        description=f"Run this command in-game:\n```/pay {receiver} {short_amount(amount)}```",
        color=0x57F287))
    task = asyncio.create_task(watch_payout(
        interaction.client, db, guild, channel, pay_msg, doc, sender, receiver, amount,
        sid, int(data.get("last_id") or 0), ends, cancel, log.inserted_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_payout(bot, db, guild, channel, pay_msg, doc, sender, receiver, amount,
                       sid, since, ends, cancel, log_id):
    key = sender.lower()
    sl, rl = sender.lower().lstrip("."), receiver.lower().lstrip(".")
    near_misses, misses, paid = [], 0, False

    async def finish(status, **extra):
        try:
            await asyncio.to_thread(lambda: db["build_payouts"].update_one(
                {"_id": log_id}, {"$set": {"status": status, "finished_at": datetime.now(timezone.utc), **extra}}))
        except Exception as e:
            logger.warning(f"payout: couldn't write payout log: {e}")

    async def edit(embed, view):
        if pay_msg is None:
            return
        try:
            await pay_msg.edit(content=None, embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning(f"payout: couldn't edit payout message: {e}")

    async def set_status(status):
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"payout_status": status}}))

    close_after = False
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.time() < ends and not paid:
                await asyncio.sleep(POLL_GAP_SECONDS)
                if cancel.is_set():
                    await finish("cancelled")
                    return                     # the Cancel button already reset the message + status
                try:
                    data = await _get(http, f"/follow-events/{sid}", since=since)
                except Exception as e:
                    misses += 1
                    logger.warning(f"payout: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    logger.warning("payout: the follow watch ended on the MC bot side.")
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != sl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(amount, float(ev["value"]), float(ev["res"])):
                        paid = True
                        logger.info(f"payout confirmed: {ev['raw']} (wanted {amount})")
                        await release_follow(bot, sid, receiver, ignore_watch_key=key, who="payout")
                        await finish("paid", server_message=ev["raw"])
                        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                            {"_id": doc["_id"]},
                            {"$set": {"payout_status": "paid", "payout_paid_at": datetime.now(timezone.utc),
                                      "payout_paid_by": sender}}))
                        done = discord.Embed(
                            title="✅ Builder paid", color=discord.Color.green(),
                            description=f"<@{doc.get('claimed_by')}> (`{receiver}`) received **{fmt_money(amount)}** from **{sender}**.")
                        done.add_field(name="🧾 Server message", value=f"```{ev['raw']}```", inline=False)
                        await edit(done, None)
                        try:
                            await asyncio.to_thread(ledger_mark_paid, db, doc, amount)
                        except Exception as e:
                            logger.warning(f"builder ledger (paid) failed: {e}")
                        schedule_recap_refresh(guild.id)
                        await send_payout_log(channel, guild, db, doc, sender, receiver, amount, ev["raw"])
                        await dm_builder_paid(bot, guild, doc, amount, sender)
                        close_after = True
                        break
                    near_misses.append(ev["raw"])

        if not paid and not cancel.is_set():
            await finish("not_detected", near_misses=near_misses[-3:])
            await set_status("pending")
            desc = (f"No payment from **{sender}** to **{receiver}** of about **{fmt_money(amount)}** "
                    "was seen in 5 minutes. Press **Pay Now** to try again.")
            if near_misses:
                desc += "\nI did see a payment between them with a different amount:\n```" + "\n".join(near_misses[-3:]) + "```"
            await edit(discord.Embed(title="⌛ Payment not detected", color=discord.Color.red(), description=desc),
                       PayoutView())
    except Exception as e:
        logger.error(f"payout watch crashed: {e}", exc_info=True)
        await finish("error")
        await set_status("pending")
        await edit(payout_embed({**doc, "payout_status": "pending"}), PayoutView())
    finally:
        WATCHES.pop(key, None)
        if CHANNEL_WATCH.get(channel.id) == key:
            CHANNEL_WATCH.pop(channel.id, None)
        await release_follow(bot, sid, receiver, who="payout")

    if close_after:
        await auto_close_ticket(channel, db, doc)


async def send_payout_log(channel, guild, db, doc, sender, receiver, amount, raw):
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    log_ch = None
    # the dedicated builder-payout channel first, then the normal payment log as a fallback
    for key in ("builder_payout_log_channel_id", "payment_log_channel_id"):
        cid = cfg.get(key) or ""
        log_ch = guild.get_channel(int(cid)) if cid.isdigit() else None
        if log_ch is not None:
            break
    if log_ch is None:
        return
    e = discord.Embed(title="💸 Builder payout confirmed", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.add_field(name="🧱 Build", value=doc.get("payout_build_name") or "—", inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(amount)}**", inline=True)
    e.add_field(name="🏷️ Type", value="Builder payout", inline=True)
    e.add_field(name="👤 Sender IGN", value=f"`{sender}`", inline=True)
    e.add_field(name="🎯 Builder IGN", value=f"`{receiver}`", inline=True)
    e.add_field(name="🛠️ Builder", value=f"<@{doc.get('claimed_by')}>", inline=True)
    e.add_field(name="🎫 Ticket", value=f"{channel.mention} (`#{channel.name}`)", inline=True)
    e.add_field(name="🧾 Server message", value=f"```{raw}```"[:1024], inline=False)
    try:
        await log_ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        logger.warning(f"payout log send failed: {ex}")


async def dm_builder_paid(bot, guild, doc, amount, sender):
    try:
        user = bot.get_user(int(doc["claimed_by"])) or await bot.fetch_user(int(doc["claimed_by"]))
        await user.send(embed=discord.Embed(
            title="💸 You have been paid!", color=0x57F287,
            description=(f"You received **{fmt_money(amount)}** for **{doc.get('payout_build_name') or 'your build'}** "
                         f"in **{guild.name}**.")))
    except (discord.HTTPException, ValueError, TypeError) as e:
        logger.warning(f"payout: couldn't DM the builder: {e}")


async def auto_close_ticket(channel, db, doc):
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "closed", "closed_by": "auto-payout"}}))
    try:
        await channel.send(embed=discord.Embed(
            description="🔒 Builder paid — ticket closed automatically. Deleting in 5 seconds…", color=0xED4245))
        await asyncio.sleep(5)
        await channel.delete(reason="Build finished and builder paid")
    except discord.HTTPException as e:
        logger.warning(f"auto close failed: {e}")


# ---------------------------------------------------------------------------
# Builder IGN management (/builder ign set|change, /bm ign set, /bm ign remove)
# and claim management (/builder unclaim, /bm assign, /bm unassign)
# ---------------------------------------------------------------------------

async def _verify_ign(db, ign: str):
    """An error embed if the IGN is invalid / doesn't exist / can't be verified, else None."""
    if not fmt_ign_ok(ign):
        return err("That doesn't look like a valid IGN.")
    code = await check_ign(db, ign)
    if code == "not_found":
        return err(f"No player named **{ign}** was found. Check the spelling.")
    if code:
        return err(f"Couldn't verify that IGN: {ERROR_MESSAGES.get(code, code)}")
    return None


async def _ign_taken(db, guild_id, ign: str, user_id) -> bool:
    d = await asyncio.to_thread(lambda: db["builder_igns"].find_one(
        {"guild_id": str(guild_id), "ign_lower": ign.lower().lstrip("."), "user_id": {"$ne": str(user_id)}}))
    return bool(d)


async def _payout_tracking(db, guild_id, user_id) -> bool:
    d = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"guild_id": str(guild_id), "claimed_by": str(user_id), "payout_status": "tracking", "status": "open"}))
    return bool(d)


TRACKING_MSG = "A payout to this builder is being tracked right now. Try again once it finishes."


async def save_builder_ign(db, guild_id, member, ign: str, source: str, by) -> None:
    """Insert or replace the builder's IGN (keeps a history of previous ones)."""
    now = datetime.now(timezone.utc)
    flt = {"guild_id": str(guild_id), "user_id": str(member.id)}
    existing = await asyncio.to_thread(lambda: db["builder_igns"].find_one(flt))
    upd = {"$set": {"ign": ign, "ign_lower": ign.lower().lstrip("."), "user_name": member.display_name,
                    "verified": True, "verified_at": now, "source": source, "set_by": str(by)},
           "$setOnInsert": {"created_at": now}}
    if existing and existing.get("ign") and existing["ign"] != ign:
        upd["$push"] = {"previous_igns": {"ign": existing["ign"], "changed_at": now, "by": str(by)}}
    await asyncio.to_thread(lambda: db["builder_igns"].update_one(flt, upd, upsert=True))


async def _edit_payout_msg(channel, d: dict, **kwargs):
    if not d.get("payout_message_id"):
        return
    try:
        await channel.get_partial_message(int(d["payout_message_id"])).edit(**kwargs)
    except discord.HTTPException as e:
        logger.warning(f"payout message edit failed: {e}")


async def sync_payouts(guild: discord.Guild, db, user_id):
    """After a builder's IGN was set / changed / removed: bring their waiting payouts up to date.
    awaiting_ign + IGN now present -> post the Pay embed; pending -> show the new IGN (or fall back
    to awaiting_ign if the IGN was removed)."""
    ign = await get_builder_ign(db, guild.id, user_id)
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    docs = await asyncio.to_thread(lambda: list(db["build_tickets"].find(
        {"guild_id": str(guild.id), "claimed_by": str(user_id),
         "payout_status": {"$in": ["pending", "awaiting_ign"]}, "status": "open"})))
    for d in docs:
        ch = guild.get_channel(int(d["channel_id"]))
        if ch is None:
            continue
        try:
            if d["payout_status"] == "awaiting_ign":
                if ign:
                    await post_payout_embed(guild, db, ch, d, cfg, ign)
            elif ign:
                await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                    {"_id": d["_id"]}, {"$set": {"builder_ign": ign}}))
                await _edit_payout_msg(ch, d, content=None, embed=payout_embed({**d, "builder_ign": ign}), view=PayoutView())
            else:
                await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                    {"_id": d["_id"]}, {"$set": {"payout_status": "awaiting_ign"}}))
                await _edit_payout_msg(ch, d, content=None, view=None, embed=discord.Embed(
                    title="⚠️ Builder IGN removed", color=0xFEE75C,
                    description="This builder has no IGN saved. The payout is on hold until they set one."))
                await ch.send(f"💬 <@{user_id}> — your IGN was removed. Run **`/builder ign set`** so you can be paid.",
                              allowed_mentions=discord.AllowedMentions(users=True))
        except Exception as e:
            logger.warning(f"sync_payouts failed for {d.get('channel_id')}: {e}")


# ---- /builder ign set | change -------------------------------------------

builder_group = app_commands.Group(name="builder", description="Builder tools", guild_only=True)
builder_ign_group = app_commands.Group(name="ign", description="Your payout IGN")
builder_group.add_command(builder_ign_group)


async def _builder_ign_common(interaction: discord.Interaction, ign: str, changing: bool):
    name = "builder ign change" if changing else "builder ign set"
    if not await check_enabled_or_reply(interaction, name):
        return
    db, guild, user = interaction.client.db, interaction.guild, interaction.user
    if db is None:
        return await interaction.response.send_message(embed=err("Database unavailable."), ephemeral=True)
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    if not is_ticket_staff(user, cfg):
        return await interaction.response.send_message(
            embed=err("Only builders (allowed roles) can set a builder IGN."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ign = ign.strip()
    current = await get_builder_ign(db, guild.id, user.id)
    if not changing and current:
        await interaction.followup.send(embed=discord.Embed(
            description=f"ℹ️ Your builder IGN is already **{current}**. Use `/builder ign change` to replace it.",
            color=0x5865F2), ephemeral=True)
        return await sync_payouts(guild, db, user.id)
    if changing and not current:
        return await interaction.followup.send(embed=err("You haven't set an IGN yet. Use `/builder ign set`."), ephemeral=True)
    if changing and current.lower().lstrip(".") == ign.lower().lstrip("."):
        return await interaction.followup.send(embed=err(f"Your IGN is already **{current}**."), ephemeral=True)
    if changing and await _payout_tracking(db, guild.id, user.id):
        return await interaction.followup.send(embed=err("A payout to you is being tracked right now. Try again once it finishes."), ephemeral=True)
    if await _ign_taken(db, guild.id, ign, user.id):
        return await interaction.followup.send(embed=err("That IGN is already registered to another builder."), ephemeral=True)
    bad = await _verify_ign(db, ign)
    if bad:
        return await interaction.followup.send(embed=bad, ephemeral=True)
    await save_builder_ign(db, guild.id, user, ign, "builder", user.id)
    desc = (f"**{current}** ➜ **{ign}** — verified and saved." if changing
            else f"**{ign}** is verified and saved. Payouts will be sent to this account.")
    await interaction.followup.send(embed=discord.Embed(
        title="✅ Builder IGN " + ("changed" if changing else "saved"), color=0x57F287, description=desc), ephemeral=True)
    await sync_payouts(guild, db, user.id)


@builder_ign_group.command(name="set", description="Set your in-game name for payouts (verified)")
@app_commands.describe(ign="Your in-game name")
async def builder_ign_set(interaction: discord.Interaction, ign: str):
    await _builder_ign_common(interaction, ign, changing=False)


@builder_ign_group.command(name="change", description="Change your saved in-game name (verified)")
@app_commands.describe(ign="Your new in-game name")
async def builder_ign_change(interaction: discord.Interaction, ign: str):
    await _builder_ign_common(interaction, ign, changing=True)


# ---- /bm ign set | /bm ign remove (build manager only) ---------------------

bm_group = app_commands.Group(name="bm", description="Build manager tools", guild_only=True)
bm_ign_group = app_commands.Group(name="ign", description="Builder payout IGNs")
bm_price_group = app_commands.Group(name="price", description="Change a ticket's price")
bm_refund_group = app_commands.Group(name="refund", description="Handle a pending refund")
bm_force_group = app_commands.Group(name="force", description="Force a step without a tracked payment")
bm_ticket_group = app_commands.Group(name="ticket", description="Ticket access and closing")
bm_payout_group = app_commands.Group(name="payout", description="Builder payout for a finished build")
bm_quote_group = app_commands.Group(name="quote", description="Add or accept quotes on behalf of a builder / customer")
for _g in (bm_quote_group, bm_payout_group, bm_ign_group, bm_price_group, bm_refund_group, bm_force_group, bm_ticket_group):
    bm_group.add_command(_g)


async def _bm_gate(interaction: discord.Interaction, command_name: str):
    """(db, cfg) if the user may use build-manager commands; else replies and None."""
    if not await check_enabled_or_reply(interaction, command_name):
        return None
    db = interaction.client.db
    if db is None:
        await interaction.response.send_message(embed=err("Database unavailable."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_manager(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
        return None
    return db, cfg


@bm_ign_group.command(name="set", description="Set (verified) a builder's IGN for payouts")
@app_commands.describe(builder="The builder", ign="Their in-game name")
async def bm_set_ign(interaction: discord.Interaction, builder: discord.Member, ign: str):
    ctx = await _bm_gate(interaction, "bm ign set")
    if not ctx:
        return
    db, cfg = ctx
    guild = interaction.guild
    if builder.bot:
        return await interaction.response.send_message(embed=err("Bots can't be builders."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ign = ign.strip()
    current = await get_builder_ign(db, guild.id, builder.id)
    if current and current.lower().lstrip(".") == ign.lower().lstrip("."):
        return await interaction.followup.send(embed=err(f"{builder.mention}'s IGN is already **{current}**."), ephemeral=True)
    if current and await _payout_tracking(db, guild.id, builder.id):
        return await interaction.followup.send(embed=err(TRACKING_MSG), ephemeral=True)
    if await _ign_taken(db, guild.id, ign, builder.id):
        return await interaction.followup.send(embed=err("That IGN is already registered to another builder."), ephemeral=True)
    bad = await _verify_ign(db, ign)
    if bad:
        return await interaction.followup.send(embed=bad, ephemeral=True)
    await save_builder_ign(db, guild.id, builder, ign, "bm", interaction.user.id)
    desc = (f"{builder.mention}: **{current}** ➜ **{ign}**" if current else f"{builder.mention}: **{ign}**") + " — verified and saved."
    await interaction.followup.send(embed=discord.Embed(title="✅ Builder IGN set", color=0x57F287, description=desc),
                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    await sync_payouts(guild, db, builder.id)


@bm_ign_group.command(name="remove", description="Remove a builder's saved IGN")
@app_commands.describe(builder="The builder")
async def bm_remove_ign(interaction: discord.Interaction, builder: discord.Member):
    ctx = await _bm_gate(interaction, "bm ign remove")
    if not ctx:
        return
    db, cfg = ctx
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    current = await get_builder_ign(db, guild.id, builder.id)
    if not current:
        return await interaction.followup.send(embed=err(f"{builder.mention} has no IGN saved."), ephemeral=True)
    if await _payout_tracking(db, guild.id, builder.id):
        return await interaction.followup.send(embed=err(TRACKING_MSG), ephemeral=True)
    await asyncio.to_thread(lambda: db["builder_igns"].delete_one(
        {"guild_id": str(guild.id), "user_id": str(builder.id)}))
    await interaction.followup.send(embed=discord.Embed(
        title="🗑️ Builder IGN removed", color=0xFEE75C,
        description=f"Removed **{current}** from {builder.mention}."),
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    await sync_payouts(guild, db, builder.id)


# ---- /bm force builder-paid -------------------------------------------------------

@builder_group.command(name="stats", description="Builds done, money earned and what you're still owed")
@app_commands.describe(builder="(Build manager) look up another builder")
async def builder_stats(interaction: discord.Interaction, builder: discord.Member = None):
    if not await check_enabled_or_reply(interaction, "builder stats"):
        return
    db, guild, user = interaction.client.db, interaction.guild, interaction.user
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    if not is_ticket_staff(user, cfg):
        return await interaction.response.send_message(
            embed=err("Only builders (allowed roles) can use this."), ephemeral=True)
    target = builder or user
    if target.id != user.id and not is_manager(user, cfg):
        return await interaction.response.send_message(
            embed=err("Only the build manager can look up other builders."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)

    def load():
        rows = list(db["builder_ledger"].find({"guild_id": str(guild.id), "builder_id": str(target.id)}))
        active = db["build_tickets"].count_documents(
            {"guild_id": str(guild.id), "claimed_by": str(target.id), "status": "open", "finished": {"$ne": True}})
        return rows, active
    rows, active = await asyncio.to_thread(load)
    earned = sum(int(r.get("received") or 0) for r in rows if r.get("paid"))
    owed = sum(int(r.get("received") or 0) for r in rows if not r.get("paid"))
    worth = sum(int(r.get("worth") or 0) for r in rows)
    ratings = [int(r["rating"]) for r in rows if r.get("rating")]
    ign = await get_builder_ign(db, guild.id, target.id)
    e = discord.Embed(title=f"🛠️ {target.display_name}", color=brand_color(cfg))
    e.add_field(name="🏁 Builds finished", value=str(len(rows)), inline=True)
    e.add_field(name="🔨 In progress", value=str(active), inline=True)
    e.add_field(name="⭐ Average rating", value=f"{sum(ratings) / len(ratings):.1f} ({len(ratings)})" if ratings else "—", inline=True)
    e.add_field(name="🧱 Total build value", value=fmt_money(worth), inline=True)
    e.add_field(name="✅ Paid to you", value=fmt_money(earned), inline=True)
    e.add_field(name="⏳ Still owed", value=fmt_money(owed), inline=True)
    e.add_field(name="🎮 Payout IGN", value=f"`{ign}`" if ign else "⚠️ not set — use `/builder ign set`", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bm_force_group.command(name="builder-paid", description="(Build manager) Mark the builder as paid for this finished build, without a payment")
async def builder_force_pay(interaction: discord.Interaction):
    ctx = await _bm_gate(interaction, "bm force builder-paid")
    if not ctx:
        return
    db, cfg = ctx
    guild, channel, user = interaction.guild, interaction.channel, interaction.user
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"}))
    if not doc:
        return await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
    if not doc.get("claimed_by"):
        return await interaction.response.send_message(
            embed=err("This ticket has no builder to pay."), ephemeral=True)
    if not doc.get("finished"):
        return await interaction.response.send_message(
            embed=err("This build isn't finished yet. Finish it first (`/builder finish` or `/bm force finish`)."),
            ephemeral=True)
    status = doc.get("payout_status")
    if status == "paid":
        return await interaction.response.send_message(embed=err("The builder was already paid."), ephemeral=True)
    if status == "tracking" or channel.id in CHANNEL_WATCH:
        return await interaction.response.send_message(
            embed=err("A payout is being tracked right now. Press **Cancel** on the payout embed first, then try again."),
            ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    price = int((build or {}).get("price") or 0)
    amount = int(doc.get("payout_amount") or payout_amount(price, await asyncio.to_thread(get_fee, db, guild.id)))
    if amount <= 0:
        return await interaction.response.send_message(
            embed=err("This build has no price, so there's nothing to pay out."), ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    ign = (await get_builder_ign(db, guild.id, doc["claimed_by"])) or doc.get("builder_ign") or ""
    now = datetime.now(timezone.utc)
    sender = f"forced ({user.display_name})"
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "payout_status": {"$nin": ["paid", "tracking"]}},
        {"$set": {"payout_status": "paid", "payout_paid_at": now, "payout_paid_by": sender,
                  "payout_forced_by": str(user.id), "payout_amount": amount, "payout_price": price,
                  "payout_build_name": (build or {}).get("name") or doc.get("payout_build_name") or "Build",
                  "builder_ign": ign}}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("The builder was already paid."), ephemeral=True)
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc

    await asyncio.to_thread(lambda: db["build_payouts"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
        "builder_id": fresh.get("claimed_by"), "builder_ign": ign or "—", "sender": sender, "amount": amount,
        "build_name": fresh.get("payout_build_name"), "status": "paid", "forced": True, "forced_by": str(user.id),
        "server_message": f"Force-marked as paid by {user.display_name}",
        "started_at": now, "finished_at": now}))
    if fresh.get("payout_message_id"):          # the Pay Now embed is no longer needed
        try:
            await channel.get_partial_message(int(fresh["payout_message_id"])).edit(
                content=None, view=None, embed=discord.Embed(
                    title="✅ Builder marked as paid", color=discord.Color.green(),
                    description=(f"{user.mention} marked <@{fresh.get('claimed_by')}> as paid "
                                 f"(**{fmt_money(amount)}**).")))
        except discord.HTTPException as e:
            logger.warning(f"builder force pay: couldn't edit the payout message: {e}")
    try:
        await asyncio.to_thread(ledger_mark_paid, db, fresh, amount)
    except Exception as e:
        logger.warning(f"builder ledger (force paid) failed: {e}")
    schedule_recap_refresh(guild.id)
    await send_payout_log(channel, guild, db, fresh, sender, ign or "—", amount,
                          f"Force-marked as paid by {user.display_name}")
    await dm_builder_paid(interaction.client, guild, fresh, amount, sender)
    await interaction.followup.send(embed=discord.Embed(
        description=f"✅ Builder marked as paid ({fmt_money(amount)}). Closing the ticket…", color=0x57F287),
        ephemeral=True)
    await auto_close_ticket(channel, db, fresh)


# ---- /bm recap --------------------------------------------------------------

def _recap_totals(db, guild_id, since):
    """(received_total, received_count, paid_out_total, paid_out_count) of confirmed payments."""
    q = {"guild_id": str(guild_id), "status": "paid"}
    if since is not None:
        q["finished_at"] = {"$gte": since}

    def total(col):
        r = list(db[col].aggregate([{"$match": q},
                                    {"$group": {"_id": None, "sum": {"$sum": "$amount"}, "n": {"$sum": 1}}}]))
        return (int(r[0]["sum"]), int(r[0]["n"])) if r else (0, 0)
    rec, rec_n = total("build_payments")
    out, out_n = total("build_payouts")
    return rec, rec_n, out, out_n


def _refund_kept(db, guild_id, since):
    """(kept_total, count): money kept as our fee from partial refunds (refund_fee). The builder gets none of it."""
    q = {"guild_id": str(guild_id), "refunded": True, "refund_fee": {"$gt": 0}}
    if since is not None:
        q["refunded_at"] = {"$gte": since}
    r = list(db["build_tickets"].aggregate([{"$match": q},
                                            {"$group": {"_id": None, "sum": {"$sum": "$refund_fee"}, "n": {"$sum": 1}}}]))
    return (int(r[0]["sum"]), int(r[0]["n"])) if r else (0, 0)


# ---- /bm recap: live panel + owner settle -------------------------------------
# One panel per server, posted with /bm recap. It edits itself (every ~45s when something changed, and right
# after a builder payout / refund / owner payment). All time only.
#
# Owner: gets owner_fee_percent % of the BUILD PRICE (set on the dashboard, always between 1 and the fee %, so
# it comes out of the fee). The share is fixed when the builder payout is confirmed (builder_ledger.owner_share)
# or when a partial refund is completed (money we kept counts as fee: owner gets owner% / fee% of it). Builds whose
# builder isn't paid yet don't count. Paid to owner = owner_payouts (status paid / forced).

def refund_owner_share(kept: int, owner_pct: int, fee_pct: int) -> int:
    """Owner's cut of money we KEPT from a partial refund. The kept money counts as the server fee, and the owner
    gets the same fraction of it that he gets of a normal build's fee: owner_pct / fee_pct.
    e.g. fee 10%, owner 5%: of 10m kept the owner gets 5m (not 5% of 10m)."""
    if kept <= 0 or owner_pct <= 0 or fee_pct <= 0:
        return 0
    return min(int(kept), int(kept) * int(owner_pct) // int(fee_pct))


def _owner_total(db, col, q, field):
    r = list(db[col].aggregate([{"$match": q}, {"$group": {"_id": None, "sum": {"$sum": f"${field}"}, "n": {"$sum": 1}}}]))
    return (int(r[0]["sum"]), int(r[0]["n"])) if r else (0, 0)


def _ledger_fee(db, gid, since):
    """Our fee on FINISHED builds: price minus what the builder gets, fixed when the build finished, so changing the
    fee % later doesn't rewrite old numbers and unfinished builds (which can still be refunded) don't count."""
    q = {"guild_id": gid}
    if since is not None:
        q["created_at"] = {"$gte": since}
    r = list(db["builder_ledger"].aggregate([
        {"$match": q},
        {"$group": {"_id": None, "sum": {"$sum": {"$subtract": [{"$ifNull": ["$worth", 0]}, {"$ifNull": ["$received", 0]}]}}}}]))
    return max(0, int(r[0]["sum"])) if r else 0


def _refund_owner_earned(db, gid, cfg) -> int:
    """Owner's share of everything we kept from partial refunds (kept money = server fee, see refund_owner_share).
    Recomputed from the stored numbers so older refunds follow the same rule. A refund done before the owner % was
    set (stored 0 / missing) uses the current owner % and fee %."""
    total = 0
    for t in db["build_tickets"].find({"guild_id": gid, "refunded": True, "refund_fee": {"$gt": 0}}):
        op = int(t.get("refund_owner_pct") or 0) or int(cfg["owner_fee_percent"] or 0)
        fp = int(t.get("refund_fee_pct") or cfg["fee_percent"])
        total += refund_owner_share(int(t.get("refund_fee") or 0), op, fp)
    return total


def _refunded_totals(db, gid, since):
    q = {"guild_id": gid, "refunded": True}
    if since is not None:
        q["refunded_at"] = {"$gte": since}
    return _owner_total(db, "build_tickets", q, "refund_amount")


def recap_data(db, guild_id, cfg: dict, since=None) -> dict:
    """since=None: all time (the live panel). With a date only the money numbers are limited to that period."""
    gid = str(guild_id)
    rec, rec_n, out, out_n = _recap_totals(db, gid, since)
    kept, kept_n = _refund_kept(db, gid, since)
    fee = _ledger_fee(db, gid, since) + kept
    earned_b, _ = _owner_total(db, "builder_ledger", {"guild_id": gid, "paid": True, "owner_share": {"$gt": 0}}, "owner_share")
    earned_r = _refund_owner_earned(db, gid, cfg)
    owner_paid, _ = _owner_total(db, "owner_payouts", {"guild_id": gid, "status": {"$in": ["paid", "forced"]}}, "amount")
    owner_earned = earned_b + earned_r
    left_total, left_n = _owner_total(db, "build_tickets", {"guild_id": gid, "finished": True,
                                                            "payout_status": {"$in": list(PAYOUT_OPEN)}}, "payout_amount")
    refunded_total, refunded_n = _refunded_totals(db, gid, since)
    # builds in progress: how many each builder has, and how much the customers paid for them
    tickets = list(db["build_tickets"].find({**_IN_PROGRESS, "guild_id": gid}))
    paid_by = _paid_by_channel(db, gid, [str(t.get("channel_id")) for t in tickets])
    per: dict = {}
    for t in tickets:
        paid, price = _ticket_paid_price(db, t, paid_by)
        row = per.setdefault(str(t["claimed_by"]), [0, 0, 0])
        row[0] += 1
        row[1] += paid
        row[2] += price
    prog = sorted(per.items(), key=lambda kv: (-kv[1][0], kv[0]))
    claim = list(db["build_tickets"].find({**_PEND_CLAIM, "guild_id": gid}))
    claim_paid_by = _paid_by_channel(db, gid, [str(t.get("channel_id")) for t in claim])
    claim_paid = sum(_ticket_paid_price(db, t, claim_paid_by)[0] for t in claim)
    quote_n = db["build_tickets"].count_documents({**_PEND_QUOTE, "guild_id": gid})
    schem_n = db["build_tickets"].count_documents({**_PEND_SCHEM, "guild_id": gid})
    return {
        "claim_n": len(claim), "claim_paid": claim_paid, "quote_n": quote_n, "schem_n": schem_n,
        "rec": rec, "rec_n": rec_n, "fee": fee, "out": out, "out_n": out_n, "kept": kept, "kept_n": kept_n,
        "owner_earned": owner_earned, "owner_paid": owner_paid, "owner_left": max(0, owner_earned - owner_paid),
        "left_total": left_total, "left_n": left_n, "refunded_total": refunded_total, "refunded_n": refunded_n,
        "progress_n": len(tickets), "progress": [[b, v[0], v[1], v[2]] for b, v in prog[:6]],
        "progress_paid": sum(v[1] for _, v in prog), "progress_price": sum(v[2] for _, v in prog),
    }


def _paid_by_channel(db, guild_id, channel_ids) -> dict:
    """{channel_id: net money the customer paid} from confirmed payments (refunds/price cuts are negative rows)."""
    ids = [c for c in channel_ids if c and c != "None"]
    if not ids:
        return {}
    rows = db["build_payments"].aggregate([
        {"$match": {"guild_id": str(guild_id), "channel_id": {"$in": ids}, "status": "paid"}},
        {"$group": {"_id": "$channel_id", "sum": {"$sum": "$amount"}}}])
    return {r["_id"]: int(r["sum"]) for r in rows}


def _ticket_paid_price(db, t: dict, paid_by: dict):
    """(paid, price) for one ticket. A ticket marked paid with no payment row (old data) counts as paid in full."""
    b = ticket_build(db, t)
    price = int((b or {}).get("price") or 0)
    paid = paid_by.get(str(t.get("channel_id")), 0)
    if paid <= 0 and t.get("paid"):
        paid = price
    return max(0, paid), price


PAYOUT_OPEN = ("pending", "tracking", "awaiting_ign")
_OPEN_BASE = {"status": "open", "finished": {"$ne": True}, "refunded": {"$ne": True}}
# waiting lists: paid but nobody claimed it / custom build waiting for quotes / custom build waiting for its schematic
_PEND_CLAIM = {**_OPEN_BASE, "paid": True, "claimed_by": {"$in": [None, ""]}}
_PEND_QUOTE = {**_OPEN_BASE, "kind": "custom", "awaiting_file": {"$ne": True}, "quote_id": {"$in": [None, ""]}}
_PEND_SCHEM = {**_OPEN_BASE, "kind": "custom", "awaiting_file": True}
_IN_PROGRESS = {"status": "open", "claimed_by": {"$nin": [None, ""]}, "finished": {"$ne": True}, "refunded": {"$ne": True}}


def _recap_sig(d: dict, cfg: dict) -> str:
    raw = json.dumps({"d": d, "o": [cfg["owner_ign"], cfg["owner_fee_percent"], cfg["fee_percent"], cfg["panel_color"]]},
                     sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def recap_embed(d: dict, cfg: dict) -> discord.Embed:
    e = discord.Embed(title="📊 Build recap", color=brand_color(cfg), timestamp=datetime.now(timezone.utc),
                      description="All-time totals. This panel updates itself.")
    e.add_field(name="💰 Money", inline=False, value=(
        f"**Payments received:** {fmt_money(d['rec'])} ({d['rec_n']} payment{'s' if d['rec_n'] != 1 else ''})\n"
        f"🏦 **Our fee (finished builds):** {fmt_money(d['fee'])}"
        + (f" (incl. {fmt_money(d['kept'])} kept from {d['kept_n']} partial refund{'s' if d['kept_n'] != 1 else ''})" if d["kept"] else "")
        + f"\n💸 **Paid to builders:** {fmt_money(d['out'])} ({d['out_n']} payout{'s' if d['out_n'] != 1 else ''})"))
    if cfg["owner_ign"] and cfg["owner_fee_percent"]:
        e.add_field(name="👑 Owner", inline=False, value=(
            f"`{cfg['owner_ign']}` gets **{cfg['owner_fee_percent']}%** of each build + his share of refund money we kept (from our fee)\n"
            f"✅ **Paid to owner:** {fmt_money(d['owner_paid'])}\n"
            f"⏳ **Left to pay to owner:** {fmt_money(d['owner_left'])}"))
    else:
        e.add_field(name="👑 Owner", inline=False,
                    value="Not set up yet. Add the owner IGN and fee % in the dashboard (Owner section).")
    e.add_field(name="🧾 Builds left to pay", inline=True, value=(
        f"**{d['left_n']}** build{'s' if d['left_n'] != 1 else ''}\n{fmt_money(d['left_total'])} owed to builders"))
    e.add_field(name="↩️ Refunded", inline=True, value=(
        f"**{d['refunded_n']}** build{'s' if d['refunded_n'] != 1 else ''}\n{fmt_money(d['refunded_total'])} refunded"
        + (f"\n{fmt_money(d['kept'])} kept" if d["kept"] else "")))
    e.add_field(name="📋 Waiting", inline=False, value=(
        f"📥 **To be claimed:** {d['claim_n']}" + (f" · {fmt_money(d['claim_paid'])} paid" if d["claim_n"] else "") + "\n"
        f"💬 **To be quoted:** {d['quote_n']}\n"
        f"📎 **Schematic pending:** {d['schem_n']}"))
    prog = "\n".join(f"<@{b}> · {n} · {fmt_money(paid)} / {fmt_money(price)}" for b, n, paid, price in d["progress"]) \
        or "Nobody is building right now."
    if d["progress_n"]:
        prog += f"\n**Paid so far: {fmt_money(d['progress_paid'])} of {fmt_money(d['progress_price'])}**"
    e.add_field(name=f"🔨 Builders in progress ({d['progress_n']})", inline=False, value=prog[:1024])
    e.set_footer(text="Owner share only counts builds the builder was already paid for.")
    return e


def _unix(dt) -> int:
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


RECAP_PAGE = 10


def recap_list_embed(db, guild_id, kind: str, page: int = 0):
    """(embed, pages) for one page of a recap list."""
    gid = str(guild_id)
    col = db["build_tickets"]
    lines, title = [], ""
    if kind == "left":
        title = "🧾 Builds left to pay builders"
        q = {"guild_id": gid, "finished": True, "payout_status": {"$in": list(PAYOUT_OPEN)}}
        sort = [("_id", 1)]
    elif kind == "refunded":
        title = "↩️ Refunded builds"
        q = {"guild_id": gid, "refunded": True}
        sort = [("refunded_at", -1)]
    elif kind == "claim":
        title = "📥 Paid, waiting for a builder"
        q = {**_PEND_CLAIM, "guild_id": gid}
        sort = [("_id", 1)]
    elif kind == "quote":
        title = "💬 Custom builds waiting for a quote"
        q = {**_PEND_QUOTE, "guild_id": gid}
        sort = [("_id", 1)]
    elif kind == "schematic":
        title = "📎 Custom builds waiting for the schematic"
        q = {**_PEND_SCHEM, "guild_id": gid}
        sort = [("_id", 1)]
    else:
        title = "🔨 Builds in progress"
        q = {**_IN_PROGRESS, "guild_id": gid}
        sort = [("_id", 1)]
    total = col.count_documents(q)
    pages = max(1, -(-total // RECAP_PAGE))
    page = max(0, min(page, pages - 1))
    rows = list(col.find(q).sort(sort).skip(page * RECAP_PAGE).limit(RECAP_PAGE))
    if kind == "left":
        for t in rows:
            st = t.get("payout_status")
            note = "⚠️ builder has no IGN" if st == "awaiting_ign" else ("⏳ being paid" if st == "tracking" else "")
            lines.append(f"<@{t.get('claimed_by')}> · {t.get('payout_build_name') or 'Build'} · "
                         f"**{fmt_money(t.get('payout_amount') or 0)}**" + (f" · {note}" if note else ""))
    elif kind == "refunded":
        for t in rows:
            kept = int(t.get("refund_fee") or 0)
            lines.append(f"<@{t.get('opener_id')}> · {t.get('refund_build_name') or 'Build'} · refunded "
                         f"**{fmt_money(t.get('refund_amount') or 0)}**" + (f" · kept {fmt_money(kept)}" if kept else "")
                         + f" · <t:{_unix(t.get('refunded_at'))}:R>")
    elif kind == "claim":
        paid_by = _paid_by_channel(db, gid, [str(t.get("channel_id")) for t in rows])
        for t in rows:
            b = ticket_build(db, t)
            paid, price = _ticket_paid_price(db, t, paid_by)
            when = f" · paid <t:{_unix(t.get('paid_at'))}:R>" if t.get("paid_at") else ""
            lines.append(f"<@{t.get('opener_id')}> · **{(b or {}).get('name') or 'Build'}** · <#{t.get('channel_id')}>\n"
                         f"↳ paid **{fmt_money(paid)}** / {fmt_money(price)}{when}")
    elif kind == "quote":
        counts = {r["_id"]: int(r["n"]) for r in db["build_quotes"].aggregate([
            {"$match": {"channel_id": {"$in": [str(t.get("channel_id")) for t in rows]}}},
            {"$group": {"_id": "$channel_id", "n": {"$sum": 1}}}])}
        for t in rows:
            n = counts.get(str(t.get("channel_id")), 0)
            when = f" · opened <t:{_unix(t.get('created_at'))}:R>" if t.get("created_at") else ""
            lines.append(f"<@{t.get('opener_id')}> · <#{t.get('channel_id')}>\n"
                         f"↳ {n} quote{'s' if n != 1 else ''} received{when}" if n else
                         f"<@{t.get('opener_id')}> · <#{t.get('channel_id')}>\n↳ no quotes yet{when}")
    elif kind == "schematic":
        for t in rows:
            when = f" · opened <t:{_unix(t.get('created_at'))}:R>" if t.get("created_at") else ""
            lines.append(f"<@{t.get('opener_id')}> · <#{t.get('channel_id')}>\n↳ no .litematic uploaded yet{when}")
    else:
        paid_by = _paid_by_channel(db, gid, [str(t.get("channel_id")) for t in rows])
        for t in rows:
            b = ticket_build(db, t)
            paid, price = _ticket_paid_price(db, t, paid_by)
            if price and paid >= price:
                status = "✅ paid in full"
            elif paid > 0:
                status = f"⚠️ partial, {fmt_money(price - paid)} missing"
            else:
                status = "❌ nothing paid"
            lines.append(f"<@{t.get('claimed_by')}> · **{(b or {}).get('name') or 'Build'}** · <#{t.get('channel_id')}>\n"
                         f"↳ customer <@{t.get('opener_id')}> · paid **{fmt_money(paid)}** / {fmt_money(price)} · {status}")
    e = discord.Embed(title=f"{title} ({total})", description="\n".join(lines) or "Nothing here.", color=COLOR)
    if pages > 1:
        e.set_footer(text=f"Page {page + 1}/{pages}")
    return e, pages


class RecapListView(discord.ui.View):
    """Prev / Next for a recap list (private message)."""
    def __init__(self, db, guild_id, kind: str, page: int, pages: int):
        super().__init__(timeout=300)
        self.db, self.guild_id, self.kind, self.page, self.pages = db, guild_id, kind, page, pages
        self._sync()

    def _sync(self):
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    async def _go(self, interaction: discord.Interaction, delta: int):
        self.page = max(0, min(self.page + delta, self.pages - 1))
        e, self.pages = await asyncio.to_thread(recap_list_embed, self.db, self.guild_id, self.kind, self.page)
        self._sync()
        await interaction.response.edit_message(embed=e, view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, -1)

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, 1)


PERIODS = {"today": ("Today", timedelta(days=1)), "week": ("Last 7 days", timedelta(days=7)),
           "month": ("Last 30 days", timedelta(days=30))}


def recap_period_embed(d: dict, cfg: dict, label: str) -> discord.Embed:
    e = discord.Embed(title=f"📊 Build recap · {label}", color=brand_color(cfg), timestamp=datetime.now(timezone.utc),
                      description="Snapshot for this period (doesn't update itself). The live panel is all-time.")
    e.add_field(name="💰 Money", inline=False, value=(
        f"**Payments received:** {fmt_money(d['rec'])} ({d['rec_n']})\n"
        f"🏦 **Our fee (finished builds):** {fmt_money(d['fee'])}"
        + (f" (incl. {fmt_money(d['kept'])} kept from {d['kept_n']} partial refund{'s' if d['kept_n'] != 1 else ''})" if d["kept"] else "")
        + f"\n💸 **Paid to builders:** {fmt_money(d['out'])} ({d['out_n']})"))
    e.add_field(name="↩️ Refunded", inline=True, value=f"**{d['refunded_n']}** · {fmt_money(d['refunded_total'])}")
    return e


async def refresh_recaps(bot, guild_id=None):
    """Edit the posted recap panel(s) if their numbers changed."""
    db = getattr(bot, "db", None)
    if db is None:
        return
    q = {"guild_id": str(guild_id)} if guild_id else {}
    try:
        panels = await asyncio.to_thread(lambda: list(db["build_recaps"].find(q)))
    except Exception as e:
        logger.warning(f"recap refresh: {e}")
        return
    for p in panels:
        guild = bot.get_guild(int(p["guild_id"]))
        if guild is None:
            continue
        try:
            cfg = await asyncio.to_thread(_get_settings, db, guild.id)
            d = await asyncio.to_thread(recap_data, db, guild.id, cfg)
        except Exception as e:
            logger.warning(f"recap data failed: {e}")
            continue
        sig = _recap_sig(d, cfg)
        if sig == p.get("sig"):
            continue
        channel = guild.get_channel_or_thread(int(p["channel_id"]))
        if channel is None:
            await asyncio.to_thread(lambda: db["build_recaps"].delete_one({"_id": p["_id"]}))
            continue
        try:
            await channel.get_partial_message(int(p["message_id"])).edit(embed=recap_embed(d, cfg), view=RecapView())
        except discord.NotFound:
            await asyncio.to_thread(lambda: db["build_recaps"].delete_one({"_id": p["_id"]}))
            continue
        except discord.HTTPException as e:
            logger.warning(f"recap panel edit failed: {e}")
            continue
        await asyncio.to_thread(lambda: db["build_recaps"].update_one({"_id": p["_id"]}, {"$set": {"sig": sig}}))
        await asyncio.sleep(0.3)


def schedule_recap_refresh(guild_id):
    """Fire-and-forget refresh of the recap panel (used right after money moves)."""
    if _BOT is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(refresh_recaps(_BOT, guild_id))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@bm_group.command(name="recap", description="Post the live build recap panel (it updates itself)")
@app_commands.describe(period="Optional: a one-off snapshot for a period instead of the live all-time panel")
@app_commands.choices(period=[app_commands.Choice(name=v[0], value=k) for k, v in PERIODS.items()])
async def bm_recap(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    ctx = await _bm_gate(interaction, "bm recap")
    if not ctx:
        return
    db, cfg = ctx
    guild = interaction.guild
    if period is not None:
        label, delta = PERIODS[period.value]
        await interaction.response.defer()
        d = await asyncio.to_thread(recap_data, db, guild.id, cfg, datetime.now(timezone.utc) - delta)
        return await interaction.followup.send(embed=recap_period_embed(d, cfg, label))
    await interaction.response.defer()
    d = await asyncio.to_thread(recap_data, db, guild.id, cfg)
    old = await asyncio.to_thread(lambda: db["build_recaps"].find_one({"guild_id": str(guild.id)}))
    msg = await interaction.followup.send(embed=recap_embed(d, cfg), view=RecapView(), wait=True)
    await asyncio.to_thread(lambda: db["build_recaps"].update_one(
        {"guild_id": str(guild.id)},
        {"$set": {"guild_id": str(guild.id), "channel_id": str(interaction.channel.id), "message_id": str(msg.id),
                  "sig": _recap_sig(d, cfg)}}, upsert=True))
    if old:       # one panel per server: the previous one goes away
        try:
            ch = guild.get_channel_or_thread(int(old["channel_id"]))
            if ch is not None:
                await ch.get_partial_message(int(old["message_id"])).delete()
        except (discord.HTTPException, ValueError):
            pass


async def _recap_gate(interaction: discord.Interaction):
    """(db, cfg) for a build manager pressing a recap button; else replies and None."""
    db = interaction.client.db
    if db is None or interaction.guild is None:
        await interaction.response.send_message(embed=err("Database unavailable."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_manager(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
        return None
    return db, cfg


class RecapView(discord.ui.View):
    """Persistent buttons under the recap panel (build manager only)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Settle Owner Amount", emoji="👑", style=discord.ButtonStyle.success,
                       custom_id="build_recap_settle", row=0)
    async def settle(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await _recap_gate(interaction)
        if not ctx:
            return
        db, cfg = ctx
        if not cfg["owner_ign"] or not cfg["owner_fee_percent"]:
            return await interaction.response.send_message(
                embed=err("The owner IGN and fee aren't set up yet. Add them in the dashboard (Owner section)."), ephemeral=True)
        if interaction.guild.id in OWNER_WATCH:
            return await interaction.response.send_message(
                embed=err("A payment to the owner is already being tracked. Wait for it or press Cancel on it."), ephemeral=True)
        await interaction.response.send_modal(OwnerSettleModal())

    async def _list(self, interaction: discord.Interaction, kind: str):
        ctx = await _recap_gate(interaction)
        if not ctx:
            return
        db, _cfg = ctx
        await interaction.response.defer(ephemeral=True)
        e, pages = await asyncio.to_thread(recap_list_embed, db, interaction.guild.id, kind)
        view = RecapListView(db, interaction.guild.id, kind, 0, pages) if pages > 1 else discord.utils.MISSING
        await interaction.followup.send(embed=e, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Left to pay", emoji="🧾", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_left", row=0)
    async def left(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "left")

    @discord.ui.button(label="Refunded", emoji="↩️", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_refunded", row=0)
    async def refunded(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "refunded")

    @discord.ui.button(label="In progress", emoji="🔨", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_progress", row=0)
    async def progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "progress")

    @discord.ui.button(label="To claim", emoji="📥", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_claim", row=1)
    async def to_claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "claim")

    @discord.ui.button(label="To quote", emoji="💬", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_quote", row=1)
    async def to_quote(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "quote")

    @discord.ui.button(label="Schematic", emoji="📎", style=discord.ButtonStyle.secondary,
                       custom_id="build_recap_schematic", row=1)
    async def to_schematic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._list(interaction, "schematic")


def owner_waiting_embed(owner_ign: str, sender: str, amount: int, ends: float) -> discord.Embed:
    e = discord.Embed(
        title="⏳ Waiting for the owner payment", color=discord.Color.from_rgb(255, 170, 0),
        description=f"Run this command in-game to pay the owner:\n```/pay {owner_ign} {short_amount(amount)}```")
    e.add_field(name="👤 Sender", value=f"`{sender}`", inline=True)
    e.add_field(name="👑 Owner", value=f"`{owner_ign}`", inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(amount)}**", inline=True)
    e.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
    e.set_footer(text="Waiting for the payment to show up in-game…")
    return e


class OwnerSettleWaitingView(discord.ui.View):
    """Persistent: Cancel while the owner payment is being tracked."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, custom_id="build_owner_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await _recap_gate(interaction)
        if not ctx:
            return
        db, _cfg = ctx
        key = OWNER_WATCH.get(interaction.guild.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
        else:       # leftover from a restart: nothing is running anymore
            await asyncio.to_thread(lambda: db["owner_payouts"].update_many(
                {"guild_id": str(interaction.guild.id), "status": "tracking"},
                {"$set": {"status": "cancelled", "finished_at": datetime.now(timezone.utc)}}))
        await interaction.response.edit_message(
            embed=discord.Embed(title="🚫 Owner payment cancelled", color=0x99AAB5,
                                description=f"Cancelled by {interaction.user.mention}. Press **Settle Owner Amount** to try again."),
            view=None)


class OwnerSettleModal(discord.ui.Modal, title="Settle the owner's amount"):
    ign = discord.ui.TextInput(label="Sender IGN (the account paying)", placeholder="Your in-game name",
                               min_length=1, max_length=25, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await run_owner_settle(interaction, str(self.ign.value).strip())


async def run_owner_settle(interaction: discord.Interaction, sender: str):
    """Verify the sender IGN, follow the owner's IGN and track the payment. Interaction already deferred."""
    db, guild, channel = interaction.client.db, interaction.guild, interaction.channel

    cmd_ref = [None]        # set as soon as receiver + amount are known

    async def show(embed):
        embed = add_pay_cmd(embed, cmd_ref[0])
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"owner settle: couldn't update private message: {e}")

    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    receiver = cfg["owner_ign"]
    if not receiver or not cfg["owner_fee_percent"]:
        return await show(err("The owner IGN and fee aren't set up in the dashboard."))
    d = await asyncio.to_thread(recap_data, db, guild.id, cfg)
    amount = int(d["owner_left"])
    if amount <= 0:
        return await show(err("There's nothing left to pay the owner right now."))
    cmd_ref[0] = f"/pay {receiver} {short_amount(amount)}"
    if not fmt_ign_ok(sender):
        return await show(err("That doesn't look like a valid IGN."))
    if sender.lower().lstrip(".") == receiver.lower().lstrip("."):
        return await show(err("The sender can't be the owner."))

    def busy_reason():
        if guild.id in OWNER_WATCH:
            return "A payment to the owner is already being tracked."
        if sender.lower() in WATCHES:
            return f"A payment from **{sender}** is already being tracked."
        if len(WATCHES) >= MAX_BUILD_WATCHES:
            return "Too many payments are being tracked right now. Try again in a minute."
        return None

    reason = busy_reason()
    if reason:
        return await show(err(reason))
    await show(discord.Embed(description=f"🔎 Checking if **{sender}** exists… this can take a few seconds.", color=0x5865F2))
    code = await check_ign(db, sender)
    if code == "not_found":
        return await show(err(f"No player named **{sender}** was found. Check the spelling."))
    if code:
        return await show(err(f"Couldn't verify that IGN: {ERROR_MESSAGES.get(code, code)}"))
    reason = busy_reason()
    if reason:
        return await show(err(reason))

    sids = await _sessions(db)
    if not sids:
        return await show(err("Couldn't start: the worker account isn't linked."))
    started, error = None, "not_ready"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        for sid in sids:
            try:
                data = await paced(_get, http, f"/follow-start/{sid}", receiver=receiver, seconds=WATCH_SECONDS + 30)
            except Exception as e:
                logger.warning(f"owner settle: follow-start failed: {e}")
                error = "not_ready"
                continue
            if data.get("ok"):
                started = (sid, data)
                break
            error = data.get("error_code") or "error"
            if error != "not_ready":
                break
    if not started:
        return await show(err(f"Couldn't start tracking: {ERRORS.get(error, error)}"))
    sid, data = started

    ends = time.time() + WATCH_SECONDS
    cancel = asyncio.Event()
    key = sender.lower()
    WATCHES[key] = {"channel_id": channel.id, "receiver": receiver, "ends": ends, "cancel": cancel}
    OWNER_WATCH[guild.id] = key
    log = await asyncio.to_thread(lambda: db["owner_payouts"].insert_one({
        "guild_id": str(guild.id), "owner_ign": receiver, "sender": sender, "amount": amount,
        "status": "tracking", "started_at": datetime.now(timezone.utc), "by": str(interaction.user.id)}))
    pay_msg = None
    try:
        pay_msg = await channel.send(embed=owner_waiting_embed(receiver, sender, amount, ends), view=OwnerSettleWaitingView())
    except discord.HTTPException as e:
        logger.warning(f"owner settle: couldn't post the waiting embed: {e}")
    await show(discord.Embed(
        title=f"✅ {sender} was found — send the payment now",
        description=f"Run this command in-game:\n```/pay {receiver} {short_amount(amount)}```", color=0x57F287))
    task = asyncio.create_task(watch_owner_settle(
        interaction.client, db, guild, pay_msg, sender, receiver, amount,
        sid, int(data.get("last_id") or 0), ends, cancel, log.inserted_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_owner_settle(bot, db, guild, pay_msg, sender, receiver, amount, sid, since, ends, cancel, log_id):
    key = sender.lower()
    sl, rl = sender.lower().lstrip("."), receiver.lower().lstrip(".")
    near_misses, misses, paid = [], 0, False

    async def finish(status, **extra):
        try:
            await asyncio.to_thread(lambda: db["owner_payouts"].update_one(
                {"_id": log_id}, {"$set": {"status": status, "finished_at": datetime.now(timezone.utc), **extra}}))
        except Exception as e:
            logger.warning(f"owner settle: couldn't write the log: {e}")

    async def edit(embed):
        if pay_msg is None:
            return
        try:
            await pay_msg.edit(content=None, embed=embed, view=None)
        except discord.HTTPException as e:
            logger.warning(f"owner settle: couldn't edit the message: {e}")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.time() < ends and not paid:
                await asyncio.sleep(POLL_GAP_SECONDS)
                if cancel.is_set():
                    await finish("cancelled")
                    return                     # the Cancel button already edited the message
                try:
                    data = await _get(http, f"/follow-events/{sid}", since=since)
                except Exception as e:
                    misses += 1
                    logger.warning(f"owner settle: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    logger.warning("owner settle: the follow watch ended on the MC bot side.")
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != sl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(amount, float(ev["value"]), float(ev["res"])):
                        paid = True
                        logger.info(f"owner payment confirmed: {ev['raw']} (wanted {amount})")
                        await release_follow(bot, sid, receiver, ignore_watch_key=key, who="owner settle")
                        await finish("paid", server_message=ev["raw"])
                        done = discord.Embed(title="✅ Owner paid", color=discord.Color.green(),
                                             timestamp=datetime.now(timezone.utc),
                                             description=f"`{receiver}` received **{fmt_money(amount)}** from **{sender}**.")
                        done.add_field(name="🧾 Server message", value=f"```{ev['raw']}```"[:1024], inline=False)
                        await edit(done)
                        await send_owner_log(guild, db, sender, receiver, amount, ev["raw"])
                        break
                    near_misses.append(ev["raw"])
        if not paid and not cancel.is_set():
            await finish("not_detected", near_misses=near_misses[-3:])
            desc = (f"No payment from **{sender}** to **{receiver}** of about **{fmt_money(amount)}** "
                    "was seen in 5 minutes. Press **Settle Owner Amount** to try again.")
            if near_misses:
                desc += "\nI did see a payment between them with a different amount:\n```" + "\n".join(near_misses[-3:]) + "```"
            await edit(discord.Embed(title="⌛ Payment not detected", color=discord.Color.red(), description=desc))
    except Exception as e:
        logger.error(f"owner settle watch crashed: {e}", exc_info=True)
        await finish("error")
        await edit(discord.Embed(title="⚠️ Something went wrong", color=discord.Color.red(),
                                 description="The payment tracking crashed. Press **Settle Owner Amount** to try again."))
    finally:
        WATCHES.pop(key, None)
        if OWNER_WATCH.get(guild.id) == key:
            OWNER_WATCH.pop(guild.id, None)
        await release_follow(bot, sid, receiver, who="owner settle")
        schedule_recap_refresh(guild.id)


async def send_owner_log(guild, db, sender, receiver, amount, raw):
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    log_ch = None
    for key in ("builder_payout_log_channel_id", "payment_log_channel_id"):
        cid = cfg.get(key) or ""
        log_ch = guild.get_channel(int(cid)) if cid.isdigit() else None
        if log_ch is not None:
            break
    if log_ch is None:
        return
    e = discord.Embed(title="👑 Owner payment confirmed", color=0x57F287, timestamp=datetime.now(timezone.utc))
    e.add_field(name="💰 Amount", value=f"**{fmt_money(amount)}**", inline=True)
    e.add_field(name="🏷️ Type", value="Owner fee", inline=True)
    e.add_field(name="👤 Sender IGN", value=f"`{sender}`", inline=True)
    e.add_field(name="🎯 Owner IGN", value=f"`{receiver}`", inline=True)
    e.add_field(name="🧾 Server message", value=f"```{raw}```"[:1024], inline=False)
    try:
        await log_ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        logger.warning(f"owner log send failed: {ex}")


@bm_force_group.command(name="owner-paid", description="(Build manager) Mark the owner as paid without a tracked payment")
@app_commands.describe(amount="How much you paid, e.g. 250k, 1.5m (default: everything left to pay the owner)")
async def force_owner_paid(interaction: discord.Interaction, amount: str = None):
    ctx = await _bm_gate(interaction, "bm force owner-paid")
    if not ctx:
        return
    db, cfg = ctx
    guild, user = interaction.guild, interaction.user
    await interaction.response.defer(ephemeral=True)
    d = await asyncio.to_thread(recap_data, db, guild.id, cfg)
    left = int(d["owner_left"])
    if left <= 0:
        return await interaction.followup.send(embed=err("There's nothing left to pay the owner."), ephemeral=True)
    value = parse_price(amount) if amount else left
    if not value or value <= 0:
        return await interaction.followup.send(embed=err("Give a positive amount like `250k` or `1.5m`."), ephemeral=True)
    if value > left:
        return await interaction.followup.send(
            embed=err(f"Only {fmt_money(left)} is left to pay the owner. Use an amount up to that."), ephemeral=True)
    now = datetime.now(timezone.utc)
    await asyncio.to_thread(lambda: db["owner_payouts"].insert_one({
        "guild_id": str(guild.id), "owner_ign": cfg["owner_ign"], "sender": f"forced ({user.display_name})",
        "amount": value, "status": "forced", "forced_by": str(user.id), "started_at": now, "finished_at": now,
        "server_message": f"Force-marked as paid by {user.display_name}"}))
    await send_owner_log(guild, db, f"forced ({user.display_name})", cfg["owner_ign"] or "—", value,
                         f"Force-marked as paid by {user.display_name}")
    schedule_recap_refresh(guild.id)
    await interaction.followup.send(embed=discord.Embed(
        description=f"✅ Marked **{fmt_money(value)}** as paid to the owner. {fmt_money(left - value)} left.",
        color=0x57F287), ephemeral=True)


# ---- claim management -------------------------------------------------------

async def release_claim(guild: discord.Guild, channel: discord.TextChannel, db, doc: dict, cfg: dict):
    """Un-claim a ticket: clear the claimer, reopen the ticket to the allowed roles, cancel a pending finish."""
    old_id = int(doc["claimed_by"])
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"claimed_by": None, "finish_status": None}}))
    await asyncio.to_thread(lambda: db["build_payments"].update_many(
        {"channel_id": str(channel.id), "status": "paid"},
        {"$set": {"claimed_by": None, "claimed_by_name": None}}))
    opener = guild.get_member(int(doc["opener_id"]))
    ow = ticket_overwrites(guild, opener or guild.me, cfg)
    if opener is None:       # keep the bot's own (manage channels) overwrite intact
        ow[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                   manage_channels=True, attach_files=True)
    for target in list(channel.overwrites):
        if getattr(target, "id", None) == old_id and target not in ow:
            try:
                await channel.set_permissions(target, overwrite=None, reason="Build unclaimed")
            except discord.HTTPException as e:
                logger.warning(f"unclaim: couldn't remove the builder: {e}")
    for target, overwrite in ow.items():
        try:
            await channel.set_permissions(target, overwrite=overwrite, reason="Build unclaimed")
        except discord.HTTPException as e:
            logger.warning(f"unclaim: couldn't restore access for {target}: {e}")


async def announce_release(channel, cfg: dict, title: str, text: str):
    ping = f"<@&{cfg['ping_role_id']}>" if cfg["ping_role_id"] else None
    try:
        await channel.send(content=ping, embed=discord.Embed(
            title=title, color=0xFEE75C, description=text + "\nUse `/builder claim` to take it."),
            allowed_mentions=discord.AllowedMentions(roles=True, users=False))
    except discord.HTTPException:
        pass




async def _ticket_staff_ctx(interaction: discord.Interaction, name: str):
    """(db, doc, cfg) for staff inside an open build ticket - or replies and None."""
    if not await check_enabled_or_reply(interaction, name):
        return None
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(interaction.channel.id), "status": "open"})) if db is not None else None
    if not doc:
        await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_ticket_staff(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("Only builders (allow roles) and the build manager can use this."), ephemeral=True)
        return None
    return db, doc, cfg


@builder_group.command(name="close", description="Close this build ticket (not possible once it is paid)")
async def ticket_close(interaction: discord.Interaction):
    ctx = await _ticket_staff_ctx(interaction, "builder close")
    if not ctx:
        return
    await close_build_ticket(interaction, ctx[1])


@builder_group.command(name="rename", description="Rename this build ticket channel")
@app_commands.describe(name="The new channel name")
async def ticket_rename(interaction: discord.Interaction, name: str):
    ctx = await _ticket_staff_ctx(interaction, "builder rename")
    if not ctx:
        return
    new = slug(name)
    if not new:
        return await interaction.response.send_message(embed=err("That isn't a usable channel name."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    old = interaction.channel.name
    try:
        await asyncio.wait_for(interaction.channel.edit(name=new, reason=f"Renamed by {interaction.user}"), timeout=8)
    except asyncio.TimeoutError:
        return await interaction.followup.send(
            embed=err("Discord is rate-limiting channel renames (2 per 10 minutes). Try again in a few minutes."),
            ephemeral=True)
    except discord.HTTPException as e:
        return await interaction.followup.send(embed=err(f"Couldn't rename the channel ({e})."), ephemeral=True)
    await interaction.followup.send(embed=discord.Embed(
        description=f"✏️ Renamed **#{old}** to **#{new}**.", color=0x57F287), ephemeral=True)


@builder_group.command(name="info", description="Show the status of this build ticket")
async def ticket_info(interaction: discord.Interaction):
    ctx = await _ticket_staff_ctx(interaction, "builder info")
    if not ctx:
        return
    db, doc, cfg = ctx
    await interaction.response.defer(ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    price = int((build or {}).get("price") or 0)
    ps = doc.get("payout_status")
    payout = {"pending": "⏳ waiting for the build manager", "tracking": "⏳ being tracked", "paid": "✅ paid",
              "awaiting_ign": "⚠️ builder has no IGN", "none": "— nothing to pay"}.get(ps, "— not started")
    if doc.get("finished"):
        stage = "🏁 finished"
    elif doc.get("finish_status") == "awaiting_confirm":
        stage = "📨 waiting for the customer to confirm delivery"
    elif doc.get("finish_status") == "reviewing":
        stage = "✍️ customer is writing a review"
    elif doc.get("claimed_by"):
        stage = "🔨 being built"
    elif doc.get("paid"):
        stage = "💰 paid, waiting for a builder"
    else:
        stage = "🆕 waiting for payment"
    e = discord.Embed(title=f"🎫 {interaction.channel.name}", color=brand_color(cfg), description=f"**Stage:** {stage}")
    e.add_field(name="🧱 Build", value=(build or {}).get("name") or "—", inline=True)
    e.add_field(name="💰 Price", value=fmt_money(price), inline=True)
    e.add_field(name="💳 Paid", value=("Yes" + (f" by `{doc['paid_by']}`" if doc.get("paid_by") else "")) if doc.get("paid") else "No", inline=True)
    e.add_field(name="🙍 Customer", value=f"<@{doc['opener_id']}>" + (f" (`{doc['ign']}`)" if doc.get("ign") else ""), inline=True)
    e.add_field(name="🛠️ Builder", value=f"<@{doc['claimed_by']}>" if doc.get("claimed_by") else "— nobody", inline=True)
    e.add_field(name=f"🏦 Builder gets ({cfg['fee_percent']}% fee)",
                value=fmt_money(int(doc.get("payout_amount") or payout_amount(price, cfg["fee_percent"]))), inline=True)
    e.add_field(name="💸 Builder payout", value=payout, inline=True)
    if doc.get("adjust_status"):
        d = doc.get("adjust_dir")
        label = {"more": "➕ extra payment", "less": "➖ price drop refund", "refund": "↩️ full refund"}.get(d, "price change")
        e.add_field(name="⏸️ Pending", value=f"{label}: **{fmt_money(doc.get('adjust_delta'))}**", inline=True)
    if interaction.channel.id in CHANNEL_WATCH:
        e.add_field(name="👀 Tracking", value="a payment is being tracked right now", inline=True)
    await interaction.followup.send(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def _ticket_member_edit(interaction: discord.Interaction, member: discord.Member, add: bool):
    name = "bm ticket add" if add else "bm ticket remove"
    if not await check_enabled_or_reply(interaction, name):
        return
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(interaction.channel.id), "status": "open"})) if db is not None else None
    if not doc:
        return await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_manager(interaction.user, cfg):
        return await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
    if member.bot:
        return await interaction.response.send_message(embed=err("That's a bot."), ephemeral=True)
    if not add and (str(member.id) in (doc["opener_id"], doc.get("claimed_by") or "") or is_manager(member, cfg)):
        return await interaction.response.send_message(
            embed=err("You can't remove the customer, the builder or a build manager."), ephemeral=True)
    try:
        if add:
            await interaction.channel.set_permissions(member, overwrite=discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
                reason=f"Added by {interaction.user}")
        else:
            await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed by {interaction.user}")
    except discord.HTTPException as e:
        return await interaction.response.send_message(embed=err(f"Couldn't change access ({e})."), ephemeral=True)
    await interaction.response.send_message(embed=discord.Embed(
        description=f"{'➕ Added' if add else '➖ Removed'} {member.mention} {'to' if add else 'from'} this ticket.",
        color=0x57F287), allowed_mentions=discord.AllowedMentions.none())


@bm_ticket_group.command(name="add", description="(Build manager) Give someone access to this ticket")
@app_commands.describe(member="Who to add")
async def ticket_add(interaction: discord.Interaction, member: discord.Member):
    await _ticket_member_edit(interaction, member, True)


@bm_ticket_group.command(name="remove", description="(Build manager) Take someone's access to this ticket away")
@app_commands.describe(member="Who to remove")
async def ticket_remove(interaction: discord.Interaction, member: discord.Member):
    await _ticket_member_edit(interaction, member, False)


@bm_ticket_group.command(name="force-close", description="(Build manager) Close + delete this ticket even if it is paid")
@app_commands.describe(reason="Why (saved in the payment log)")
async def ticket_force_close(interaction: discord.Interaction, reason: str = ""):
    if not await check_enabled_or_reply(interaction, "bm ticket force-close"):
        return
    db, guild, channel = interaction.client.db, interaction.guild, interaction.channel
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"})) if db is not None else None
    if not doc:
        return await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    if not is_manager(interaction.user, cfg):
        return await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
    warn = ""
    if doc.get("paid") and not doc.get("refunded") and doc.get("payout_status") != "paid":
        warn = ("\n⚠️ The customer **paid** and hasn't been refunded. Use `/bm refund start` if they should get their money back.")
    if doc.get("payout_status") in ("pending", "tracking"):
        warn += "\n⚠️ A builder payout is still open."
    reason = reason.strip()[:200]

    async def go():
        key = CHANNEL_WATCH.get(channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "closed", "closed_by": str(interaction.user.id),
                                           "force_closed": True, "close_reason": reason}}))
        cid = cfg.get("payment_log_channel_id") or ""
        if cid.isdigit() and guild.get_channel(int(cid)):
            e = discord.Embed(title="🔒 Ticket force-closed", color=0xED4245, timestamp=datetime.now(timezone.utc))
            e.add_field(name="🎫 Ticket", value=f"`#{channel.name}`", inline=True)
            e.add_field(name="🙍 Customer", value=f"<@{doc['opener_id']}>", inline=True)
            e.add_field(name="👤 By", value=interaction.user.mention, inline=True)
            e.add_field(name="💳 Paid", value="Yes" if doc.get("paid") else "No", inline=True)
            if reason:
                e.add_field(name="📝 Reason", value=reason, inline=False)
            try:
                await guild.get_channel(int(cid)).send(embed=e, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass
        try:
            await channel.send(embed=discord.Embed(
                description=f"🔒 Ticket force-closed by {interaction.user.mention}. Deleting in 5 seconds…", color=0xED4245),
                allowed_mentions=discord.AllowedMentions.none())
            await asyncio.sleep(5)
            await channel.delete(reason=f"Force-closed by {interaction.user}")
        except discord.HTTPException:
            pass
        return True, "Ticket closed."

    await interaction.response.send_message(embed=discord.Embed(
        title="Force-close this ticket?", color=0xFEE75C,
        description="This deletes the ticket channel for good." + warn),
        view=ConfirmPriceView(interaction.user.id, go), ephemeral=True)


class ConfirmPriceView(discord.ui.View):
    """Ephemeral Confirm / Cancel for /bm price set (not persistent)."""
    def __init__(self, user_id: int, on_confirm):
        super().__init__(timeout=60)
        self.user_id, self.on_confirm, self.done = user_id, on_confirm, False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.done:
            return
        self.done = True
        await interaction.response.edit_message(
            embed=discord.Embed(description="⏳ Applying…", color=0xFEE75C), view=None)
        ok, msg = await self.on_confirm()
        await interaction.edit_original_response(
            embed=discord.Embed(description=("✅ " if ok else "❌ ") + msg, color=0x57F287 if ok else 0xED4245))

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        await interaction.response.edit_message(
            embed=discord.Embed(description="Cancelled. Nothing was changed.", color=0x99AAB5), view=None)


async def apply_price_override(db, guild: discord.Guild, channel: discord.TextChannel, ticket_id, new_price: int, by):
    """Manager price override. Updates the ticket, the picked quote, the payment record, the payout and the
    builder ledger, then refreshes the embeds. Returns (ok, message)."""
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": ticket_id, "status": "open"}))
    if not doc:
        return False, "This ticket is no longer open."
    if doc.get("payout_status") in ("paid", "tracking"):
        return False, ("The builder payout is already " + ("paid" if doc["payout_status"] == "paid" else "being tracked")
                       + ". Cancel the tracking / fix the Builder tracker entry on the dashboard instead.")
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    old_build = await asyncio.to_thread(ticket_build, db, doc)
    old_price = int((old_build or {}).get("price") or 0)
    if old_price == new_price:
        return False, "The price is already that amount."
    # a payment being tracked right now would confirm the OLD amount: stop it first
    key = CHANNEL_WATCH.get(channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
        await asyncio.sleep(1.5)
    now = datetime.now(timezone.utc)
    sets = {"price_override": new_price}
    if doc.get("quote_price") or doc.get("kind") in ("custom", "digout"):
        sets["quote_price"] = new_price
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$set": sets, "$push": {"price_history": {"action": "set_price", "old": old_price, "new": new_price,
                                                   "by": str(by.id), "at": now}}}))
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    build = await asyncio.to_thread(ticket_build, db, fresh)

    # the picked quote (so the panel shows the real price)
    if fresh.get("quote_id"):
        try:
            await asyncio.to_thread(lambda: db["build_quotes"].update_one(
                {"_id": ObjectId(fresh["quote_id"])}, {"$set": {"price": new_price, "price_before_override": old_price}}))
            q = await asyncio.to_thread(lambda: db["build_quotes"].find_one({"_id": ObjectId(fresh["quote_id"])}))
            if q and fresh.get("quote_message_id"):
                await channel.get_partial_message(int(fresh["quote_message_id"])).edit(
                    embed=quote_embed(fresh["opener_id"], [], chosen=q), view=ChangeQuoteView() if not fresh.get("paid") else None)
        except Exception as e:
            logger.warning(f"set-price: quote panel update failed: {e}")
    # payment already confirmed: what we count as received is the new price (refund handled by hand)
    if fresh.get("paid"):
        await asyncio.to_thread(lambda: db["build_payments"].update_many(
            {"channel_id": str(channel.id), "status": "paid"},
            {"$set": {"amount": new_price, "amount_before_override": old_price}}))
    elif fresh.get("pay_message_id"):       # not paid yet: show the new price on the payment embed
        try:
            await channel.get_partial_message(int(fresh["pay_message_id"])).edit(
                embed=pay_embed_idle(build, cfg), view=PayView(custom=_is_custom(build)))
        except discord.HTTPException as e:
            logger.warning(f"set-price: couldn't refresh the payment embed: {e}")
    # builder side: payout embed (not paid yet) + ledger entry
    if fresh.get("payout_status") == "pending":
        fields = {"payout_price": new_price, "payout_amount": payout_amount(new_price, cfg["fee_percent"])}
        await asyncio.to_thread(lambda: db["build_tickets"].update_one({"_id": fresh["_id"]}, {"$set": fields}))
        if fresh.get("payout_message_id"):
            try:
                await channel.get_partial_message(int(fresh["payout_message_id"])).edit(
                    embed=payout_embed({**fresh, **fields}))
            except discord.HTTPException as e:
                logger.warning(f"set-price: couldn't refresh the payout embed: {e}")
    led = await asyncio.to_thread(lambda: db["builder_ledger"].update_one(
        {"guild_id": fresh["guild_id"], "ticket_id": str(fresh["_id"]), "edited_by": {"$exists": False}, "paid": {"$ne": True}},
        {"$set": {"worth": new_price, "received": payout_amount(new_price, cfg["fee_percent"])}}))
    # announce
    desc = (f"{by.mention} changed the price of **{(build or {}).get('name', 'this build')}**\n"
            f"{fmt_money(old_price)} ➜ **{fmt_money(new_price)}**")
    if fresh.get("finished") or fresh.get("claimed_by"):
        desc += f"\n🛠️ Builder payout is now **{fmt_money(payout_amount(new_price, cfg["fee_percent"]))}** ({cfg["fee_percent"]}% fee)."
    try:
        await channel.send(embed=discord.Embed(title="💲 Price updated", color=0xFEE75C, description=desc),
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass
    cid = cfg.get("payment_log_channel_id") or ""
    if cid.isdigit() and guild.get_channel(int(cid)):
        e = discord.Embed(title="💲 Price override", color=0xFEE75C, timestamp=now)
        e.add_field(name="🎫 Ticket", value=f"{channel.mention} (`#{channel.name}`)", inline=True)
        e.add_field(name="Old ➜ New", value=f"{fmt_money(old_price)} ➜ **{fmt_money(new_price)}**", inline=True)
        e.add_field(name="👤 By", value=by.mention, inline=True)
        e.add_field(name="💳 Customer paid", value="Yes" if fresh.get("paid") else "Not yet", inline=True)
        try:
            await guild.get_channel(int(cid)).send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as ex:
            logger.warning(f"set-price: log send failed: {ex}")
    extra = ""
    if fresh.get("finished") and not led.modified_count:
        extra = " (The builder-tracker entry was edited by hand or is already paid, so I left it alone.)"
    return True, f"Price set to **{fmt_money(new_price)}**." + extra


# ---------------------------------------------------------------------------
# Paid-build price adjustment: /bm price more|less
#   The build is already paid and claimed. The BM raises or lowers the price; the builder stays the same.
#   More -> the customer gets a "pay the extra" embed (tracked like a normal payment).
#   Less -> the build manager gets a "pay the customer back" embed with the /pay command (tracked).
#   The new price is applied when the money is seen in-game (or immediately with force).
# ---------------------------------------------------------------------------

def _fmt_pct(p) -> str:
    try:
        p = float(p)
    except (TypeError, ValueError):
        p = 100.0
    return f"{p:g}%"


def adjust_embed(doc: dict, cfg: dict) -> discord.Embed:
    more = doc.get("adjust_dir") == "more"
    refund = doc.get("adjust_dir") == "refund"
    delta = int(doc.get("adjust_delta") or 0)
    if refund:
        ign = doc.get("ign") or "?"
        e = discord.Embed(
            title="↩️ Refund requested", color=discord.Color.from_rgb(255, 170, 0),
            description=(f"This build is being **refunded** ({_fmt_pct(doc.get('adjust_percent', 100))} of what the customer paid). "
                         "The build manager pays the customer back:\n"
                         f"```/pay {ign} {short_amount(delta)}```\n"
                         "Press **Pay Now** so I can track it. Once the refund is seen, the builder is unclaimed "
                         "(and gets no payout), the customer is told they were refunded and the ticket is closed."))
        e.add_field(name="🧱 Build", value=doc.get("adjust_build_name") or "Build", inline=True)
        e.add_field(name="🙍 Customer IGN", value=f"`{ign}`", inline=True)
        e.add_field(name="💵 Refund amount", value=f"**{fmt_money(delta)}**", inline=True)
        if int(doc.get("adjust_fee") or 0) > 0:
            e.add_field(name="🏦 Kept as our fee", value=f"**{fmt_money(doc.get('adjust_fee'))}**", inline=True)
        e.set_footer(text=f"Requested by {doc.get('adjust_requested_by_name') or 'staff'}")
        return e
    e = discord.Embed(
        title="➕ Extra payment needed" if more else "➖ Refund the customer",
        color=discord.Color.from_rgb(255, 170, 0))
    e.add_field(name="🧱 Build", value=doc.get("adjust_build_name") or "Build", inline=True)
    e.add_field(name="💰 Old price", value=fmt_money(doc.get("adjust_old")), inline=True)
    e.add_field(name="🆕 New price", value=f"**{fmt_money(doc.get('adjust_new'))}**", inline=True)
    if more:
        e.description = ("The price of this build went up. The customer needs to pay the difference.\n"
                         "Press **Start Pay** and I'll verify your IGN, then show the payment command.")
        e.add_field(name="💵 Customer pays extra", value=f"**{fmt_money(delta)}**", inline=False)
    else:
        ign = doc.get("ign") or "?"
        e.description = ("The price of this build went down. The build manager pays the customer back:\n"
                         f"```/pay {ign} {short_amount(delta)}```\nThen press **Pay Now** so I can track it.")
        e.add_field(name="🙍 Customer IGN", value=f"`{ign}`", inline=True)
        e.add_field(name="💵 Refund amount", value=f"**{fmt_money(delta)}**", inline=True)
    e.set_footer(text="The new builder is assigned once this is settled." if doc.get("requote_paid")
                 else "The builder stays the same.")
    return e


def adjust_waiting_embed(doc: dict, payer: str, receiver: str, ends: float) -> discord.Embed:
    delta = int(doc.get("adjust_delta") or 0)
    e = discord.Embed(title="⏳ Waiting for the payment", color=discord.Color.from_rgb(255, 170, 0),
                      description=f"Run this command in-game to pay:\n```/pay {receiver} {short_amount(delta)}```")
    e.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
    e.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
    e.add_field(name="💰 Amount", value=f"**{fmt_money(delta)}**", inline=True)
    e.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
    return e


async def apply_adjust_price(db, guild: discord.Guild, channel: discord.TextChannel, doc: dict, by_id: str):
    """Write the new price (price_override) everywhere it is read and clear the pending adjustment."""
    new_price = int(doc["adjust_new"])
    old_price = int(doc.get("adjust_old") or 0)
    sets = {"price_override": new_price}
    if doc.get("quote_price") or doc.get("kind") in ("custom", "digout"):
        sets["quote_price"] = new_price
    unsets = {k: "" for k in ("adjust_status", "adjust_dir", "adjust_delta", "adjust_old", "adjust_new",
                              "adjust_message_id", "adjust_build_name")}
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$set": sets, "$unset": unsets,
         "$push": {"price_history": {"action": "add_" + str(doc.get("adjust_dir")), "old": old_price,
                                     "new": new_price, "by": by_id, "at": datetime.now(timezone.utc)}}}))
    if doc.get("quote_id"):
        try:
            await asyncio.to_thread(lambda: db["build_quotes"].update_one(
                {"_id": ObjectId(doc["quote_id"])}, {"$set": {"price": new_price, "price_before_override": old_price}}))
            q = await asyncio.to_thread(lambda: db["build_quotes"].find_one({"_id": ObjectId(doc["quote_id"])}))
            if q and doc.get("quote_message_id"):
                await channel.get_partial_message(int(doc["quote_message_id"])).edit(
                    embed=quote_embed(doc["opener_id"], [], chosen=q), view=None)
        except Exception as e:
            logger.warning(f"adjust: quote panel update failed: {e}")
    try:    # builder ledger entry (only exists if the build was finished and not edited / paid)
        await asyncio.to_thread(lambda: db["builder_ledger"].update_one(
            {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"]), "edited_by": {"$exists": False},
             "paid": {"$ne": True}},
            {"$set": {"worth": new_price, "received": payout_amount(new_price, get_fee(db, doc["guild_id"]))}}))
    except Exception as e:
        logger.warning(f"adjust: ledger update failed: {e}")


async def finish_adjust(guild, channel, db, cfg, doc: dict, by_id: str, payer: str, receiver: str, raw: str,
                        forced_by=None):
    """Money seen (or forced): book it, apply the new price, update the embed and tell the ticket."""
    more = doc.get("adjust_dir") == "more"
    delta = int(doc.get("adjust_delta") or 0)
    now = datetime.now(timezone.utc)
    if doc.get("adjust_dir") == "refund":
        return await complete_refund(guild, channel, db, cfg, doc, payer, receiver, raw, forced_by)
    await apply_adjust_price(db, guild, channel, doc, by_id)
    newly_assigned = None
    cur = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or {}
    if cur.get("requote_paid") and cur.get("quote_builder_id") and not cur.get("claimed_by"):
        bid = cur["quote_builder_id"]     # builder swap: the difference is settled, the new builder starts now
        try:
            nb = guild.get_member(int(bid)) or await guild.fetch_member(int(bid))
        except (discord.HTTPException, ValueError):
            nb = None
        if nb:
            r = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                {"_id": doc["_id"], "claimed_by": {"$in": [None]}},
                {"$set": {"claimed_by": bid}, "$unset": {"requote_paid": ""}}))
            if r.modified_count:
                await apply_claim(guild, channel, nb, cur, cfg)
                await asyncio.to_thread(lambda: db["build_payments"].update_many(
                    {"channel_id": str(channel.id), "status": "paid"},
                    {"$set": {"claimed_by": bid, "claimed_by_name": nb.display_name}}))
                newly_assigned = bid
                doc = {**doc, "claimed_by": bid}
    if forced_by is not None:       # no tracked payment: still book it so the recap stays right
        opener = guild.get_member(int(doc["opener_id"]))
        await asyncio.to_thread(lambda: db["build_payments"].insert_one({
            "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
            "build_id": "adjust", "build_name": (doc.get("adjust_build_name") or "Build") + (" (extra)" if more else " (refund)"),
            "payer": payer, "receiver": receiver, "amount": delta if more else -delta,
            "opener_id": doc["opener_id"], "opener_name": opener.display_name if opener else doc["opener_id"],
            "status": "paid", "started_at": now, "finished_at": now, "forced_by": str(forced_by.id)}))
    title = "✅ Extra payment received" if more else "✅ Customer refunded"
    done = discord.Embed(title=title, color=discord.Color.green(), timestamp=now,
                         description=(f"Price: {fmt_money(doc.get('adjust_old'))} ➜ **{fmt_money(doc.get('adjust_new'))}**\n"
                                      f"Builder: <@{doc.get('claimed_by')}> (unchanged)"))
    done.add_field(name="💰 Amount", value=f"**{fmt_money(delta)}**", inline=True)
    done.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
    done.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
    if forced_by is not None:
        done.add_field(name="⚠️ Forced", value=f"Marked as done by {forced_by.mention}", inline=False)
    elif raw:
        done.add_field(name="🧾 Server message", value=f"```{raw}```"[:1024], inline=False)
    if doc.get("adjust_message_id"):
        try:
            await channel.get_partial_message(int(doc["adjust_message_id"])).edit(content=None, embed=done, view=None)
        except discord.HTTPException:
            try:
                await channel.send(embed=done)
            except discord.HTTPException:
                pass
    try:
        if newly_assigned:
            await channel.send(f"<@{newly_assigned}> **Build Has Been paid**, you can start building! "
                               f"(customer: <@{doc['opener_id']}>, price **{fmt_money(doc.get('adjust_new'))}**)",
                               allowed_mentions=discord.AllowedMentions(users=True))
        else:
            await channel.send(f"<@{doc.get('claimed_by')}> the build price was changed to "
                               f"**{fmt_money(doc.get('adjust_new'))}**. Keep building!",
                               allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass
    cid = cfg.get("payment_log_channel_id") or ""
    if cid.isdigit() and guild.get_channel(int(cid)):
        e = discord.Embed(title="➕ Extra payment" if more else "➖ Customer refund", color=0xFEE75C, timestamp=now)
        e.add_field(name="🎫 Ticket", value=f"{channel.mention} (`#{channel.name}`)", inline=True)
        e.add_field(name="Old ➜ New", value=f"{fmt_money(doc.get('adjust_old'))} ➜ **{fmt_money(doc.get('adjust_new'))}**", inline=True)
        e.add_field(name="💰 Amount", value=fmt_money(delta), inline=True)
        e.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
        e.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
        e.add_field(name="🛠️ Builder", value=f"<@{doc.get('claimed_by')}>", inline=True)
        try:
            await guild.get_channel(int(cid)).send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as ex:
            logger.warning(f"adjust: log send failed: {ex}")


async def complete_refund(guild, channel, db, cfg, doc: dict, payer: str, receiver: str, raw: str, forced_by=None):
    """Refund seen: unclaim the builder (no credit / payout), DM the customer, log it and close the ticket.
    Anything not refunded (adjust_fee) simply stays with the server as its fee."""
    delta = int(doc.get("adjust_delta") or 0)
    fee = int(doc.get("adjust_fee") or 0)
    now = datetime.now(timezone.utc)
    builder_id = doc.get("claimed_by")
    if forced_by is not None:
        opener = guild.get_member(int(doc["opener_id"]))
        await asyncio.to_thread(lambda: db["build_payments"].insert_one({
            "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
            "build_id": "adjust", "build_name": (doc.get("adjust_build_name") or "Build") + " (refund)",
            "payer": payer, "receiver": receiver, "amount": -delta,
            "opener_id": doc["opener_id"], "opener_name": opener.display_name if opener else doc["opener_id"],
            "status": "paid", "started_at": now, "finished_at": now, "forced_by": str(forced_by.id)}))
    unsets = {k: "" for k in ("adjust_status", "adjust_dir", "adjust_delta", "adjust_old", "adjust_new",
                              "adjust_message_id", "adjust_build_name", "adjust_requested_by_name",
                              "adjust_paid", "adjust_percent", "adjust_fee")}
    owner_pct = await asyncio.to_thread(get_owner_pct, db, guild.id)
    fee_pct = await asyncio.to_thread(get_fee, db, guild.id)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$unset": unsets, "$set": {"refunded": True, "refunded_at": now, "refund_amount": delta,
                                    "refund_percent": doc.get("adjust_percent", 100),
                                    "refund_fee": fee, "refund_build_name": doc.get("adjust_build_name") or "Build",
                                    "refund_owner_pct": owner_pct, "refund_fee_pct": fee_pct,
                                    "refund_owner_share": refund_owner_share(fee, owner_pct, fee_pct)}}))
    schedule_recap_refresh(guild.id)
    # builder is released (permissions go back to the normal ticket roles)
    if builder_id:
        try:
            await release_claim(guild, channel, db, {**doc, "claimed_by": builder_id}, cfg)
        except Exception as e:
            logger.warning(f"refund: unclaim failed: {e}")
    done = discord.Embed(title="✅ Customer refunded", color=discord.Color.green(), timestamp=now,
                         description=f"<@{doc['opener_id']}> (`{receiver}`) got **{fmt_money(delta)}** back from **{payer}**.")
    if fee > 0:
        done.add_field(name="🏦 Kept as fee", value=f"**{fmt_money(fee)}** ({_fmt_pct(doc.get('adjust_percent', 100))} refunded)", inline=False)
    if forced_by is not None:
        done.add_field(name="⚠️ Forced", value=f"Marked as refunded by {forced_by.mention}", inline=False)
    elif raw:
        done.add_field(name="🧾 Server message", value=f"```{raw}```"[:1024], inline=False)
    if doc.get("adjust_message_id"):
        try:
            await channel.get_partial_message(int(doc["adjust_message_id"])).edit(content=None, embed=done, view=None)
        except discord.HTTPException:
            pass
    cid = cfg.get("payment_log_channel_id") or ""
    if cid.isdigit() and guild.get_channel(int(cid)):
        e = discord.Embed(title="↩️ Full refund" if fee <= 0 else f"↩️ Partial refund ({_fmt_pct(doc.get('adjust_percent', 100))})",
                          color=0xFEE75C, timestamp=now)
        e.add_field(name="🧱 Build", value=doc.get("adjust_build_name") or "Build", inline=True)
        e.add_field(name="💰 Refunded", value=fmt_money(delta), inline=True)
        if fee > 0:
            e.add_field(name="🏦 Kept as fee", value=fmt_money(fee), inline=True)
        e.add_field(name="🎫 Ticket", value=f"`#{channel.name}`", inline=True)
        e.add_field(name="👤 Paid by", value=f"`{payer}`", inline=True)
        e.add_field(name="🎯 Customer", value=f"<@{doc['opener_id']}> (`{receiver}`)", inline=True)
        if builder_id:
            e.add_field(name="🛠️ Builder removed (no payout)", value=f"<@{builder_id}>", inline=True)
        try:
            await guild.get_channel(int(cid)).send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as ex:
            logger.warning(f"refund: log send failed: {ex}")
    try:        # DM the customer
        user = guild.get_member(int(doc["opener_id"])) or await guild.fetch_member(int(doc["opener_id"]))
        await user.send(embed=discord.Embed(
            title="↩️ You have been refunded", color=0x57F287,
            description=(f"You were refunded **{fmt_money(delta)}** for **{doc.get('adjust_build_name') or 'your build'}** "
                         f"in **{guild.name}**." + (f" ({_fmt_pct(doc.get('adjust_percent', 100))} of what you paid.)" if fee > 0 else ""))))
    except (discord.HTTPException, ValueError, TypeError) as e:
        logger.warning(f"refund: couldn't DM the customer: {e}")
    # close the ticket
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"status": "closed", "closed_by": "refund"}}))
    try:
        await channel.send(embed=discord.Embed(
            description="🔒 Customer refunded — ticket closed. Deleting in 5 seconds…", color=0xED4245))
        await asyncio.sleep(5)
        await channel.delete(reason="Build refunded")
    except discord.HTTPException as e:
        logger.warning(f"refund: auto close failed: {e}")


_ADJUST_KEYS = ("adjust_status", "adjust_dir", "adjust_delta", "adjust_old", "adjust_new",
                "adjust_message_id", "adjust_build_name", "adjust_requested_by_name",
                "adjust_paid", "adjust_percent", "adjust_fee")


async def cancel_pending_adjust(db, channel, doc: dict, by):
    """Drop the pending price change / refund (stops a running payment watch first). Returns (ok, message)."""
    key = CHANNEL_WATCH.get(channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
        await asyncio.sleep(1.5)
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    if not fresh.get("adjust_status"):
        return False, "There's nothing pending (it may have just been completed)."
    what = "refund" if fresh.get("adjust_dir") == "refund" else "price change"
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$unset": {k: "" for k in _ADJUST_KEYS}}))
    if fresh.get("requote_paid") and not fresh.get("claimed_by"):
        await _reopen_quote_panel(db, channel, fresh)      # nobody on it: the customer picks again
    if fresh.get("adjust_message_id"):
        try:
            await channel.get_partial_message(int(fresh["adjust_message_id"])).edit(
                content=None, view=None,
                embed=discord.Embed(title=f"🚫 {what.capitalize()} cancelled", color=0x99AAB5,
                                    description=f"Cancelled by {by.mention}. Nothing was changed."))
        except discord.HTTPException:
            pass
    return True, f"Pending {what} cancelled."


async def force_pending_adjust(db, guild, channel, cfg, doc: dict, by):
    """Complete the pending price change / refund without a tracked payment. Returns (ok, message)."""
    key = CHANNEL_WATCH.get(channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
        await asyncio.sleep(1.5)
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    if not fresh.get("adjust_status"):
        return False, "There's nothing pending (it may have just been completed)."
    more = fresh.get("adjust_dir") == "more"
    payer = (fresh.get("ign") or "?") if more else by.display_name
    receiver = cfg["receiver_ign"] if more else (fresh.get("ign") or "?")
    await finish_adjust(guild, channel, db, cfg, fresh, str(by.id), payer, receiver, "", forced_by=by)
    return True, "Done without a payment."


class AdjustModal(discord.ui.Modal, title="Start payment"):
    ign = discord.ui.TextInput(label="Your in-game name (IGN)", placeholder="The account that will pay",
                               min_length=1, max_length=25, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await run_adjust(interaction, str(self.ign.value).strip())


class AdjustView(discord.ui.View):
    """Persistent: Start Pay (customer, extra) / Pay Now (build manager, refund)."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Start Pay", emoji="💸", style=discord.ButtonStyle.success, custom_id="build_adjust_start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        if not doc or doc.get("adjust_status") != "pending":
            return await interaction.response.send_message(embed=err("There's no pending price change."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        more = doc.get("adjust_dir") == "more"
        if more:
            if str(interaction.user.id) != doc["opener_id"] and not is_ticket_staff(interaction.user, cfg):
                return await interaction.response.send_message(
                    embed=err("Only the ticket opener can pay this."), ephemeral=True)
            if not cfg["receiver_ign"]:
                return await interaction.response.send_message(
                    embed=err("The receiver IGN isn't set on the dashboard yet."), ephemeral=True)
        elif not is_manager(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the build manager can pay the refund."), ephemeral=True)
        if interaction.channel.id in CHANNEL_WATCH:
            return await interaction.response.send_message(
                embed=err("A payment is already being tracked in this ticket."), ephemeral=True)
        payer = (doc.get("ign") or "").strip() if more else ""
        if not payer:
            return await interaction.response.send_modal(AdjustModal())
        await interaction.response.defer(ephemeral=True)
        await run_adjust(interaction, payer)


class AdjustWaitingView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cancel", emoji="✖️", style=discord.ButtonStyle.danger, custom_id="build_adjust_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = interaction.client.db
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(interaction.channel.id), "status": "open"}))
        if not doc:
            return await interaction.response.send_message(embed=err("This is not an active build ticket."), ephemeral=True)
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if str(interaction.user.id) != doc["opener_id"] and not is_ticket_staff(interaction.user, cfg):
            return await interaction.response.send_message(
                embed=err("Only the ticket opener or staff can cancel this."), ephemeral=True)
        key = CHANNEL_WATCH.get(interaction.channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
        await interaction.response.edit_message(embed=adjust_embed(doc, cfg), view=AdjustView())


async def run_adjust(interaction: discord.Interaction, payer: str):
    """Verify the payer IGN, follow the receiver and track the extra payment / refund. Interaction already deferred."""
    db, guild, channel = interaction.client.db, interaction.guild, interaction.channel

    cmd_ref = [None]        # set as soon as receiver + amount are known

    async def show(embed):
        embed = add_pay_cmd(embed, cmd_ref[0])
        try:
            await interaction.edit_original_response(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"adjust: couldn't update private message: {e}")

    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"}))
    if not doc or doc.get("adjust_status") != "pending":
        return await show(err("There's no pending price change in this ticket."))
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    more = doc.get("adjust_dir") == "more"
    receiver = cfg["receiver_ign"] if more else (doc.get("ign") or "").strip()
    delta = int(doc.get("adjust_delta") or 0)
    if not receiver or delta <= 0:
        return await show(err("This price change isn't set up for payments."))
    cmd_ref[0] = f"/pay {receiver} {short_amount(delta)}"
    if not fmt_ign_ok(payer):
        return await show(err("That doesn't look like a valid IGN."))
    if payer.lower().lstrip(".") == receiver.lower().lstrip("."):
        return await show(err("The payer can't be the receiver."))

    def busy_reason():
        if channel.id in CHANNEL_WATCH:
            return "A payment is already being tracked in this ticket."
        if payer.lower() in WATCHES:
            return f"A payment from **{payer}** is already being tracked in another ticket."
        if len(WATCHES) >= MAX_BUILD_WATCHES:
            return "Too many payments are being tracked right now. Try again in a minute."
        return None

    reason = busy_reason()
    if reason:
        return await show(err(reason))
    await show(discord.Embed(description=f"🔎 Checking if **{payer}** exists… this can take a few seconds.", color=0x5865F2))
    code = await check_ign(db, payer)
    if code == "not_found":
        return await show(err(f"No player named **{payer}** was found. Check the spelling."))
    if code:
        return await show(err(f"Couldn't verify that IGN: {ERROR_MESSAGES.get(code, code)}"))
    reason = busy_reason()
    if reason:
        return await show(err(reason))

    sids = await _sessions(db)
    if not sids:
        return await show(err("Couldn't start: the worker account isn't linked."))
    started, error = None, "not_ready"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as http:
        for sid in sids:
            try:
                data = await paced(_get, http, f"/follow-start/{sid}", receiver=receiver, seconds=WATCH_SECONDS + 30)
            except Exception as e:
                logger.warning(f"adjust: follow-start failed: {e}")
                error = "not_ready"
                continue
            if data.get("ok"):
                started = (sid, data)
                break
            error = data.get("error_code") or "error"
            if error != "not_ready":
                break
    if not started:
        return await show(err(f"Couldn't start tracking: {ERRORS.get(error, error)}"))
    sid, data = started

    ends = time.time() + WATCH_SECONDS
    cancel = asyncio.Event()
    key = payer.lower()
    WATCHES[key] = {"channel_id": channel.id, "receiver": receiver, "ends": ends, "cancel": cancel}
    CHANNEL_WATCH[channel.id] = key
    opener = guild.get_member(int(doc["opener_id"]))
    log = await asyncio.to_thread(lambda: db["build_payments"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
        "build_id": "adjust", "build_name": (doc.get("adjust_build_name") or "Build") + (" (extra)" if more else " (refund)"),
        "payer": payer, "receiver": receiver, "amount": delta if more else -delta,
        "opener_id": doc["opener_id"], "opener_name": opener.display_name if opener else doc["opener_id"],
        "status": "tracking", "started_at": datetime.now(timezone.utc)}))
    msg = channel.get_partial_message(int(doc["adjust_message_id"])) if doc.get("adjust_message_id") else None
    if msg:
        try:
            await msg.edit(content=None, embed=adjust_waiting_embed(doc, payer, receiver, ends), view=AdjustWaitingView())
        except discord.HTTPException as e:
            logger.warning(f"adjust: couldn't edit the message: {e}")
    await show(discord.Embed(description=f"✅ **{payer}** was found. Send the payment in-game now.", color=0x57F287))
    task = asyncio.create_task(watch_adjust(
        interaction.client, db, guild, channel, msg, doc, cfg, payer, receiver, delta,
        sid, int(data.get("last_id") or 0), ends, cancel, log.inserted_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def watch_adjust(bot, db, guild, channel, msg, doc, cfg, payer, receiver, value,
                       sid, since, ends, cancel, log_id):
    key = payer.lower()
    pl, rl = payer.lower().lstrip("."), receiver.lower().lstrip(".")
    near_misses, misses, paid = [], 0, False

    async def finish(status, **extra):
        try:
            await asyncio.to_thread(lambda: db["build_payments"].update_one(
                {"_id": log_id}, {"$set": {"status": status, "finished_at": datetime.now(timezone.utc), **extra}}))
        except Exception as e:
            logger.warning(f"adjust: couldn't write payment log: {e}")

    async def edit(embed, view):
        if msg is None:
            return
        try:
            await msg.edit(content=None, embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning(f"adjust: couldn't edit message: {e}")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.time() < ends and not paid:
                await asyncio.sleep(POLL_GAP_SECONDS)
                if cancel.is_set():
                    await finish("cancelled")
                    return
                try:
                    data = await _get(http, f"/follow-events/{sid}", since=since)
                except Exception as e:
                    misses += 1
                    logger.warning(f"adjust: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != pl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(value, float(ev["value"]), float(ev["res"])):
                        paid = True
                        await release_follow(bot, sid, receiver, ignore_watch_key=key, who="adjust")
                        await finish("paid", server_message=ev["raw"])
                        fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
                        if fresh.get("adjust_status"):
                            await finish_adjust(guild, channel, db, cfg, fresh, "", payer, receiver, ev["raw"])
                        break
                    near_misses.append(ev["raw"])
        if not paid and not cancel.is_set():
            await finish("not_detected", near_misses=near_misses[-3:])
            fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
            desc = (f"No payment from **{payer}** to **{receiver}** of about **{fmt_money(value)}** "
                    "was seen in 5 minutes. Press the button to try again.")
            if near_misses:
                desc += "\nI did see a payment between them with a different amount:\n```" + "\n".join(near_misses[-3:]) + "```"
            await edit(discord.Embed(title="⌛ Payment not detected", color=discord.Color.red(), description=desc),
                       AdjustView())
    except Exception as e:
        logger.error(f"adjust watch crashed: {e}", exc_info=True)
        await finish("error")
        await edit(adjust_embed(doc, cfg), AdjustView())
    finally:
        WATCHES.pop(key, None)
        if CHANNEL_WATCH.get(channel.id) == key:
            CHANNEL_WATCH.pop(channel.id, None)
        await release_follow(bot, sid, receiver, who="adjust")


class BuildTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CloseBuildButton())


# ---------------------------------------------------------------------------
# Commands (outside the cog): /builder = builders, /bm = build managers
# ---------------------------------------------------------------------------

@bm_group.command(name="panel", description="Post the build panel (builds + prices) in this channel")
async def panel(interaction: discord.Interaction):
    if not await check_enabled_or_reply(interaction, "bm panel"):
        return
    db = interaction.client.db
    if db is None:
        return await interaction.response.send_message(embed=err("Database unavailable."), ephemeral=True)
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_manager(interaction.user, cfg):
        return await interaction.response.send_message(
            embed=err("Only admins or the build manager role can post the build panel."), ephemeral=True)
    builds = await asyncio.to_thread(_get_builds, db, interaction.guild.id)
    ppb = cfg["price_per_block"]
    images = await load_panel_images(db, cfg)
    kwargs = {"files": panel_files(images)} if images else {}
    await interaction.response.send_message(embed=panel_embed(interaction.guild, builds, ppb, cfg, images),
                                            view=BuildPanelView(builds, digout=bool(ppb)), **kwargs)
    msg = await interaction.original_response()
    await asyncio.to_thread(lambda: db["build_panels"].insert_one({
        "guild_id": str(interaction.guild.id), "channel_id": str(msg.channel.id),
        "message_id": str(msg.id), "sig": panel_signature(builds, ppb, cfg)}))


@builder_group.command(name="claim", description="Claim this paid build ticket (allow roles only)")
async def claim(interaction: discord.Interaction):
    if not await check_enabled_or_reply(interaction, "builder claim"):
        return
    db = interaction.client.db
    guild, channel, user = interaction.guild, interaction.channel, interaction.user
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"})) if db is not None else None
    if not doc:
        return await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
    cfg = await asyncio.to_thread(_get_settings, db, guild.id)
    if not is_ticket_staff(user, cfg):
        return await interaction.response.send_message(
            embed=err("Only the allowed roles can claim build tickets."), ephemeral=True)
    if doc.get("kind") == "custom" and not doc.get("quote_id"):
        return await interaction.response.send_message(
            embed=err("The customer hasn't picked a quote yet. Submit a quote with the **Submit Quote** button; "
                      "the chosen builder is assigned automatically once the customer has paid."), ephemeral=True)
    if not doc.get("paid"):
        return await interaction.response.send_message(
            embed=err("This build hasn't been paid yet. It can only be claimed once the customer paid "
                      "(or the build manager used `/bm force pay`)."), ephemeral=True)
    if doc.get("kind") == "custom" and doc.get("quote_builder_id") and doc["quote_builder_id"] != str(user.id):
        return await interaction.response.send_message(
            embed=err(f"The customer chose <@{doc['quote_builder_id']}>'s quote, so only that builder can take this "
                      "custom build."), ephemeral=True)
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "paid": True, "claimed_by": {"$in": [None]}},
        {"$set": {"claimed_by": str(user.id)}}))
    if not res.modified_count:
        fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or {}
        return await interaction.response.send_message(
            embed=err(f"This ticket is already claimed by <@{fresh.get('claimed_by')}>."), ephemeral=True)

    await interaction.response.defer()
    await asyncio.to_thread(lambda: db["build_payments"].update_many(
        {"channel_id": str(channel.id), "status": "paid"},
        {"$set": {"claimed_by": str(user.id), "claimed_by_name": user.display_name}}))
    await apply_claim(guild, channel, user, doc, cfg)

    embed = discord.Embed(
        title="🙋 Build claimed", color=0x57F287,
        description=(f"{user.mention} claimed this build.\n"
                     "Only the customer, the claimer and the build manager can see this ticket now."))
    await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@builder_group.command(name="finish", description="Mark this build as delivered (only the builder who claimed it)")
async def finish(interaction: discord.Interaction):
    if not await check_enabled_or_reply(interaction, "builder finish"):
        return
    db = interaction.client.db
    guild, channel, user = interaction.guild, interaction.channel, interaction.user
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(channel.id), "status": "open"})) if db is not None else None
    if not doc:
        return await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
    if not doc.get("claimed_by"):
        return await interaction.response.send_message(
            embed=err("This ticket hasn't been claimed by a builder yet."), ephemeral=True)
    if str(user.id) != doc["claimed_by"]:
        return await interaction.response.send_message(
            embed=err("Only the builder who claimed this ticket can finish it."), ephemeral=True)
    if doc.get("finished"):
        return await interaction.response.send_message(embed=err("This build is already finished."), ephemeral=True)
    if doc.get("finish_status") == "reviewing":
        return await interaction.response.send_message(
            embed=err("The customer is already writing their review."), ephemeral=True)
    if doc.get("adjust_status"):
        return await interaction.response.send_message(
            embed=err("A price change is still waiting for its payment. Finish or cancel it first."), ephemeral=True)
    try:
        customer = guild.get_member(int(doc["opener_id"])) or await guild.fetch_member(int(doc["opener_id"]))
    except discord.HTTPException:
        return await interaction.response.send_message(
            embed=err("The customer is no longer in the server."), ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    e = discord.Embed(
        title="📦 Did you receive your build?", color=COLOR,
        description=(f"**{guild.name}** — {user.mention} says your build "
                     f"**{build['name'] if build else 'order'}** has been delivered.\n\n"
                     "Please confirm below."))
    try:
        dm = await customer.send(embed=e, view=FinishConfirmView())
    except discord.HTTPException:
        return await interaction.followup.send(embed=err(
            f"I couldn't DM {customer.mention}. Ask them to allow DMs from server members, then try again."),
            ephemeral=True)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$set": {"finish_status": "awaiting_confirm", "finish_dm_message_id": str(dm.id),
                  "finish_requested_at": datetime.now(timezone.utc)}}))
    await interaction.followup.send(
        embed=discord.Embed(description=f"📨 Sent a confirmation DM to {customer.mention}.", color=0x57F287),
        ephemeral=True)
    await channel.send(
        f"📨 {user.mention} marked the build as finished. Waiting for {customer.mention} to confirm in their DMs.",
        allowed_mentions=discord.AllowedMentions.none())


async def _claim_ticket(interaction: discord.Interaction, need_manager: bool, name: str):
    """(db, guild, channel, doc, cfg) for an open, unfinished build ticket - or replies and None."""
    if not await check_enabled_or_reply(interaction, name):
        return None
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(interaction.channel.id), "status": "open"})) if db is not None else None
    if not doc:
        await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if need_manager and not is_manager(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
        return None
    if doc.get("finished"):
        await interaction.response.send_message(
            embed=err("This build is already finished, its claim can't be changed."), ephemeral=True)
        return None
    return db, interaction.guild, interaction.channel, doc, cfg


@builder_group.command(name="unclaim", description="Release this build so another builder can claim it")
async def unclaim(interaction: discord.Interaction):
    ctx = await _claim_ticket(interaction, False, "builder unclaim")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    user = interaction.user
    if not doc.get("claimed_by"):
        return await interaction.response.send_message(embed=err("This ticket isn't claimed."), ephemeral=True)
    if str(user.id) != doc["claimed_by"] and not is_manager(user, cfg):
        return await interaction.response.send_message(
            embed=err("Only the builder who claimed this ticket (or the build manager) can unclaim it."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await release_claim(guild, channel, db, doc, cfg)
    reopened = await reopen_quotes_after_release(guild, channel, db, doc, cfg, doc["claimed_by"], by=user)
    await interaction.followup.send(embed=discord.Embed(
        description="🔓 Unclaimed. The quotes are open again." if reopened else "🔓 Unclaimed.", color=0x57F287), ephemeral=True)
    if not reopened:
        await announce_release(channel, cfg, "🔓 Build unclaimed", f"<@{doc['claimed_by']}> released this build.")


@bm_group.command(name="assign", description="(Build manager) Force-assign a builder to this ticket")
@app_commands.describe(builder="The builder to assign")
async def assign(interaction: discord.Interaction, builder: discord.Member):
    ctx = await _claim_ticket(interaction, True, "bm assign")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if builder.bot:
        return await interaction.response.send_message(embed=err("Bots can't be builders."), ephemeral=True)
    if str(builder.id) == doc["opener_id"]:
        return await interaction.response.send_message(
            embed=err("The customer can't be assigned as the builder."), ephemeral=True)
    if doc.get("claimed_by") == str(builder.id):
        return await interaction.response.send_message(
            embed=err(f"{builder.mention} is already assigned to this ticket."), ephemeral=True)
    if doc.get("kind") == "custom" and (not doc.get("quote_id") or not doc.get("paid")):
        return await interaction.response.send_message(
            embed=err("A custom build can't be assigned until the customer has picked a quote **and** paid for it "
                      "(`/bm force pay` counts as paid)."), ephemeral=True)
    await interaction.response.defer()
    prev = doc.get("claimed_by")
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"claimed_by": str(builder.id), "assigned_by": str(interaction.user.id)}}))
    await asyncio.to_thread(lambda: db["build_payments"].update_many(
        {"channel_id": str(channel.id), "status": "paid"},
        {"$set": {"claimed_by": str(builder.id), "claimed_by_name": builder.display_name}}))
    await apply_claim(guild, channel, builder, doc, cfg)
    desc = (f"{interaction.user.mention} assigned {builder.mention} to this build"
            + (f" (replacing <@{prev}>)." if prev else ".")
            + "\nOnly the customer, the builder and the build manager can see this ticket now.")
    if not doc.get("paid") and doc.get("kind") != "custom":
        desc += "\n⚠️ This build hasn't been paid yet."
    await interaction.followup.send(
        embed=discord.Embed(title="🙋 Build assigned", color=0x57F287, description=desc),
        allowed_mentions=discord.AllowedMentions(users=True))


@bm_group.command(name="unassign", description="(Build manager) Force-unclaim a builder from this ticket")
@app_commands.describe(builder="The builder currently on this ticket")
async def unassign(interaction: discord.Interaction, builder: discord.Member):
    ctx = await _claim_ticket(interaction, True, "bm unassign")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("claimed_by") != str(builder.id):
        return await interaction.response.send_message(
            embed=err(f"{builder.mention} isn't the builder on this ticket."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await release_claim(guild, channel, db, doc, cfg)
    reopened = await reopen_quotes_after_release(guild, channel, db, doc, cfg, str(builder.id), by=interaction.user)
    await interaction.followup.send(embed=discord.Embed(
        description="🔓 Unassigned. The quotes are open again." if reopened else "🔓 Unassigned.", color=0x57F287), ephemeral=True)
    if not reopened:
        await announce_release(channel, cfg, "🔓 Builder unassigned",
                               f"{interaction.user.mention} removed {builder.mention} from this build.")


@bm_group.command(name="reset-quote", description="(Build manager) Undo the customer's quote pick so they can choose again")
async def reset_quote_cmd(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm reset-quote")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("kind") != "custom" or not doc.get("quote_id"):
        return await interaction.response.send_message(
            embed=err("This ticket has no accepted custom-build quote."), ephemeral=True)
    problem = quote_reset_blocker(doc)
    if problem:
        return await interaction.response.send_message(embed=err(problem), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ok, msg = await reset_quote(db, guild, channel, doc, interaction.user)
    await interaction.followup.send(
        embed=discord.Embed(description=("✅ " if ok else "❌ ") + msg, color=0x57F287 if ok else 0xED4245),
        ephemeral=True)


@bm_group.command(name="cancel-pay", description="(Build manager) Cancel the payment step and reopen quote picking")
@app_commands.describe(keep_quote="Keep the accepted quote, only stop the running payment (default: reset it)")
async def cancel_pay_cmd(interaction: discord.Interaction, keep_quote: bool = False):
    ctx = await _manager_ticket(interaction, "bm cancel-pay")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("paid"):
        return await interaction.response.send_message(
            embed=err("This ticket is already paid. Use `/bm refund start` or `/bm price set` instead."), ephemeral=True)
    tracking = channel.id in CHANNEL_WATCH
    if doc.get("kind") == "custom" and doc.get("quote_id") and not keep_quote:
        await interaction.response.defer(ephemeral=True)       # quote picked, payment may or may not have started
        ok, msg = await reset_quote(db, guild, channel, doc, interaction.user)
        return await interaction.followup.send(
            embed=discord.Embed(description=("✅ Payment cancelled. " if ok else "❌ ") + msg,
                                color=0x57F287 if ok else 0xED4245), ephemeral=True)
    if not tracking:
        return await interaction.response.send_message(
            embed=err("No payment is running in this ticket. Use `/bm cancel-pay` after a quote was picked to reset it."),
            ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    key = CHANNEL_WATCH.get(channel.id)
    if key and key in WATCHES:
        WATCHES[key]["cancel"].set()
        await asyncio.sleep(1.5)
    build = await asyncio.to_thread(ticket_build, db, doc)
    if doc.get("pay_message_id") and build:
        try:
            await channel.get_partial_message(int(doc["pay_message_id"])).edit(
                embed=pay_embed_idle(build, cfg), view=PayView(custom=_is_custom(build)))
        except (discord.HTTPException, ValueError) as e:
            logger.warning(f"cancel-pay: couldn't restore the payment embed: {e}")
    await interaction.followup.send(
        embed=discord.Embed(description="✅ Payment tracking cancelled. The customer can press **Start Pay** again.",
                            color=0x57F287), ephemeral=True)


@bm_price_group.command(name="set", description="(Build manager) Override this ticket's price (works after payment too)")
@app_commands.describe(amount="New price, e.g. 250k, 1.5m, 2b")
async def set_price(interaction: discord.Interaction, amount: str):
    ctx = await _manager_ticket(interaction, "bm price set")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    new_price = parse_price(amount)
    if new_price is None:
        return await interaction.response.send_message(
            embed=err("Invalid price. Use something like `250k`, `5m` or `1.5b`."), ephemeral=True)
    if doc.get("kind") == "custom" and not doc.get("quote_id"):
        return await interaction.response.send_message(
            embed=err("No quote was accepted yet. The customer has to pick one first."), ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    old_price = int((build or {}).get("price") or 0)
    if doc.get("payout_status") == "paid":
        return await interaction.response.send_message(embed=err(
            f"The builder was already paid for this ticket ({fmt_money(payout_amount(old_price, cfg["fee_percent"]))}). "
            "Fix that entry in the dashboard's Builder tracker instead."), ephemeral=True)
    if doc.get("payout_status") == "tracking":
        return await interaction.response.send_message(embed=err(
            "A builder payout is being tracked right now. Cancel it first, then change the price."), ephemeral=True)
    lines = [f"**{fmt_money(old_price)}** ➜ **{fmt_money(new_price)}**",
             f"Builder payout becomes **{fmt_money(payout_amount(new_price, cfg["fee_percent"]))}** ({cfg["fee_percent"]}% fee)."]
    if doc.get("paid"):
        lines.append("💳 The customer already paid: the recap will now count the new price as received. "
                     "Refund / collect the difference by hand.")
    elif channel.id in CHANNEL_WATCH:
        lines.append("⚠️ A payment is being tracked and will be cancelled.")
    view = ConfirmPriceView(interaction.user.id,
                            lambda: apply_price_override(db, guild, channel, doc["_id"], new_price, interaction.user))
    await interaction.response.send_message(
        embed=discord.Embed(title="Change the price?", color=0xFEE75C, description="\n".join(lines)),
        view=view, ephemeral=True)


async def _price_adjust(interaction: discord.Interaction, mode: str, amount: str = None):
    """Shared by /bm price more|less|cancel|force."""
    ctx = await _manager_ticket(interaction, f"bm price {mode}")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if mode in ("cancel", "force"):
        if not doc.get("adjust_status"):
            return await interaction.response.send_message(embed=err("There's no pending price change."), ephemeral=True)
        key = CHANNEL_WATCH.get(channel.id)
        if key and key in WATCHES:
            WATCHES[key]["cancel"].set()
            await asyncio.sleep(1.5)
        await interaction.response.defer(ephemeral=True)
        fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
        if not fresh.get("adjust_status"):
            return await interaction.followup.send(embed=err("That price change was already completed."), ephemeral=True)
        if mode == "force":
            more = fresh.get("adjust_dir") == "more"
            await finish_adjust(guild, channel, db, cfg, fresh, str(interaction.user.id),
                                fresh.get("ign") or "?" if more else interaction.user.display_name,
                                cfg["receiver_ign"] if more else (fresh.get("ign") or "?"), "",
                                forced_by=interaction.user)
            return await interaction.followup.send(
                embed=discord.Embed(description="✅ Price change applied without a payment.", color=0x57F287), ephemeral=True)
        unsets = {k: "" for k in ("adjust_status", "adjust_dir", "adjust_delta", "adjust_old", "adjust_new",
                                  "adjust_message_id", "adjust_build_name")}
        await asyncio.to_thread(lambda: db["build_tickets"].update_one({"_id": doc["_id"]}, {"$unset": unsets}))
        if fresh.get("requote_paid") and not fresh.get("claimed_by"):
            await _reopen_quote_panel(db, channel, fresh)      # nobody on it: the customer picks again
        if fresh.get("adjust_message_id"):
            try:
                await channel.get_partial_message(int(fresh["adjust_message_id"])).edit(
                    embed=discord.Embed(title="🚫 Price change cancelled", color=0x99AAB5,
                                        description=f"Cancelled by {interaction.user.mention}. The price stays the same."),
                    view=None)
            except discord.HTTPException:
                pass
        return await interaction.followup.send(
            embed=discord.Embed(description="✅ Pending price change cancelled.", color=0x57F287), ephemeral=True)

    if not doc.get("paid"):
        return await interaction.response.send_message(
            embed=err("This build isn't paid yet. Use `/bm price set` before payment."), ephemeral=True)
    if not doc.get("claimed_by"):
        return await interaction.response.send_message(
            embed=err("No builder has claimed this ticket yet."), ephemeral=True)
    if doc.get("finished") or doc.get("finish_status") or doc.get("payout_status"):
        return await interaction.response.send_message(
            embed=err("This build is already being finished, so the price can't be changed with `/bm price more|less`."),
            ephemeral=True)
    if doc.get("adjust_status"):
        return await interaction.response.send_message(
            embed=err("A price change is already pending. Finish it or use **Cancel pending change** first."),
            ephemeral=True)
    if channel.id in CHANNEL_WATCH:
        return await interaction.response.send_message(
            embed=err("A payment is being tracked in this ticket right now."), ephemeral=True)
    delta = parse_price(amount) if amount else None
    if not delta or delta <= 0:
        return await interaction.response.send_message(
            embed=err("Give a positive amount like `250k`, `5m` or `1.5b`."), ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    old_price = int((build or {}).get("price") or 0)
    more = mode == "more"
    new_price = old_price + delta if more else old_price - delta
    if new_price <= 0:
        return await interaction.response.send_message(
            embed=err(f"That would take the price to {fmt_money(new_price)}. The current price is {fmt_money(old_price)}."),
            ephemeral=True)
    if not more and not (doc.get("ign") or "").strip():
        return await interaction.response.send_message(
            embed=err("I don't have the customer's IGN for this ticket, so I can't build the refund command."),
            ephemeral=True)
    if more and not cfg["receiver_ign"]:
        return await interaction.response.send_message(
            embed=err("The receiver IGN isn't set on the dashboard yet."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    fields = {"adjust_status": "pending", "adjust_dir": mode, "adjust_delta": delta, "adjust_old": old_price,
              "adjust_new": new_price, "adjust_build_name": (build or {}).get("name") or "Build"}
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "adjust_status": {"$exists": False}}, {"$set": fields}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("A price change is already pending."), ephemeral=True)
    full = {**doc, **fields}
    if more:
        content, mentions = f"<@{doc['opener_id']}>", discord.AllowedMentions(users=True)
    else:
        content = f"<@&{cfg['manager_role_id']}>" if cfg["manager_role_id"] else None
        mentions = discord.AllowedMentions(roles=True)
    msg = await channel.send(content=content, embed=adjust_embed(full, cfg), view=AdjustView(), allowed_mentions=mentions)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"adjust_message_id": str(msg.id)}}))
    await interaction.followup.send(embed=discord.Embed(
        description=(f"✅ {'Customer has to pay' if more else 'You have to refund'} **{fmt_money(delta)}**. "
                     f"Price: {fmt_money(old_price)} ➜ **{fmt_money(new_price)}**. The builder stays the same."),
        color=0x57F287), ephemeral=True)


@bm_price_group.command(name="more", description="Paid build costs more: the customer pays the extra (builder stays)")
@app_commands.describe(amount="How much to add, e.g. 250k, 1.5m")
async def bm_price_more(interaction: discord.Interaction, amount: str):
    await _price_adjust(interaction, "more", amount)


@bm_price_group.command(name="less", description="Paid build costs less: you pay the customer the difference (builder stays)")
@app_commands.describe(amount="How much to take off, e.g. 250k, 1.5m")
async def bm_price_less(interaction: discord.Interaction, amount: str):
    await _price_adjust(interaction, "less", amount)


@bm_price_group.command(name="cancel", description="Cancel the pending price change / extra payment")
async def bm_price_cancel(interaction: discord.Interaction):
    await _price_adjust(interaction, "cancel")


@bm_price_group.command(name="force", description="Apply the pending price change without a tracked payment")
async def bm_price_force(interaction: discord.Interaction):
    await _price_adjust(interaction, "force")


@bm_refund_group.command(name="start", description="(Build manager) Refund the customer (all or a %), unclaim the builder, close the ticket")
@app_commands.describe(percentage="% to refund, 1-100 (default 100). The rest is kept as our fee, builder gets nothing")
async def bm_refund_start(interaction: discord.Interaction, percentage: app_commands.Range[float, 1, 100] = 100):
    ctx = await _manager_ticket(interaction, "bm refund start")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    user = interaction.user
    if not doc.get("paid"):
        return await interaction.response.send_message(
            embed=err("This ticket isn't paid, so there is nothing to refund. Just close it."), ephemeral=True)
    if doc.get("finished") or doc.get("payout_status"):
        return await interaction.response.send_message(
            embed=err("This build is already finished, so it can't be refunded here."), ephemeral=True)
    if doc.get("adjust_status"):
        return await interaction.response.send_message(
            embed=err("A price change / refund is already pending. Use `/bm price cancel` or `/bm refund cancel` first."),
            ephemeral=True)
    if channel.id in CHANNEL_WATCH:
        return await interaction.response.send_message(
            embed=err("A payment is being tracked in this ticket right now."), ephemeral=True)
    if not (doc.get("ign") or "").strip():
        return await interaction.response.send_message(
            embed=err("I don't have the customer's IGN for this ticket, so I can't build the refund command."),
            ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    def paid_total():
        r = list(db["build_payments"].aggregate([
            {"$match": {"channel_id": str(channel.id), "status": "paid"}},
            {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}]))
        return int(r[0]["sum"]) if r else 0
    paid = await asyncio.to_thread(paid_total)
    build = await asyncio.to_thread(ticket_build, db, doc)
    if paid <= 0:       # force-paid / no tracked payment: use the build price
        paid = int((build or {}).get("price") or 0)
    if paid <= 0:
        return await interaction.followup.send(embed=err("There's no amount to refund."), ephemeral=True)
    pct = float(percentage)
    amount = paid if pct >= 100 else int(paid * pct / 100)
    if amount <= 0:
        return await interaction.followup.send(embed=err("That percentage is too small to refund anything."), ephemeral=True)
    fee = paid - amount
    fields = {"adjust_status": "pending", "adjust_dir": "refund", "adjust_delta": amount,
              "adjust_paid": paid, "adjust_percent": pct, "adjust_fee": fee,
              "adjust_build_name": (build or {}).get("name") or "Build",
              "adjust_requested_by_name": user.display_name}
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "adjust_status": {"$exists": False}}, {"$set": fields}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("A refund is already pending."), ephemeral=True)
    msg = await channel.send(embed=adjust_embed({**doc, **fields}, cfg), view=AdjustView())
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"adjust_message_id": str(msg.id)}}))
    extra = f" **{fmt_money(fee)}** is kept as our fee." if fee > 0 else ""
    await interaction.followup.send(embed=discord.Embed(
        description=(f"✅ Refund of **{fmt_money(amount)}** ({_fmt_pct(pct)} of {fmt_money(paid)}) requested.{extra} "
                     "The builder gets no payout. Press **Pay Now** in the ticket to track it."),
        color=0x57F287), ephemeral=True)


@bm_refund_group.command(name="cancel", description="(Build manager) Cancel the pending refund (the ticket stays open)")
async def cancel_refund(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm refund cancel")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("adjust_dir") != "refund" or not doc.get("adjust_status"):
        return await interaction.response.send_message(embed=err("There's no pending refund in this ticket."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    ok, msg = await cancel_pending_adjust(db, channel, doc, interaction.user)
    await interaction.followup.send(
        embed=discord.Embed(description=("✅ " if ok else "❌ ") + msg, color=0x57F287 if ok else 0xED4245), ephemeral=True)


@bm_refund_group.command(name="force", description="(Build manager) Mark the pending refund as paid: unclaims, DMs the customer, closes the ticket")
async def force_refund(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm refund force")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("adjust_dir") != "refund" or not doc.get("adjust_status"):
        return await interaction.response.send_message(
            embed=err("There's no pending refund. Use `/bm refund start` first."), ephemeral=True)
    view = ConfirmPriceView(interaction.user.id,
                            lambda: force_pending_adjust(db, guild, channel, cfg, doc, interaction.user))
    await interaction.response.send_message(embed=discord.Embed(
        title="Force the refund?", color=0xFEE75C,
        description=(f"This marks **{fmt_money(doc.get('adjust_delta'))}** as refunded **without** checking a payment, "
                     "then unclaims the builder, DMs the customer and **deletes this ticket**.")),
        view=view, ephemeral=True)


async def _manager_ticket(interaction: discord.Interaction, name: str):
    """(db, guild, channel, doc, cfg) for a build manager inside an open build ticket - or replies and None."""
    if not await check_enabled_or_reply(interaction, name):
        return None
    db = interaction.client.db
    doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
        {"channel_id": str(interaction.channel.id), "status": "open"})) if db is not None else None
    if not doc:
        await interaction.response.send_message(
            embed=err("This command can only be used inside a build ticket."), ephemeral=True)
        return None
    cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
    if not is_manager(interaction.user, cfg):
        await interaction.response.send_message(
            embed=err("Only admins or the build manager role can use this."), ephemeral=True)
        return None
    return db, interaction.guild, interaction.channel, doc, cfg


@bm_quote_group.command(name="add", description="(Build manager) Add a quote for a builder on this custom ticket")
@app_commands.describe(builder="The builder the quote is from", amount="Price, e.g. 250k, 1.5m, 2b",
                       note="Optional note shown on the quote")
async def bm_quote_add(interaction: discord.Interaction, builder: discord.Member, amount: str, note: str = ""):
    ctx = await _manager_ticket(interaction, "bm quote add")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("kind") != "custom":
        return await interaction.response.send_message(embed=err("This isn't a custom build ticket."), ephemeral=True)
    if doc.get("quote_id"):
        return await interaction.response.send_message(
            embed=err("A quote was already accepted. Use `/bm reset-quote` first."), ephemeral=True)
    price = parse_price(amount)
    if price is None:
        return await interaction.response.send_message(
            embed=err("Invalid amount. Use something like `250k`, `5m` or `1.5b`."), ephemeral=True)
    if str(builder.id) == doc["opener_id"]:
        return await interaction.response.send_message(
            embed=err("The customer can't quote their own ticket."), ephemeral=True)
    mine = await asyncio.to_thread(lambda: db["build_quotes"].find_one(
        {"channel_id": str(channel.id), "builder_id": str(builder.id)}))
    if not mine:
        count = await asyncio.to_thread(lambda: db["build_quotes"].count_documents({"channel_id": str(channel.id)}))
        if count >= MAX_QUOTES:
            return await interaction.response.send_message(
                embed=err(f"This ticket already has {MAX_QUOTES} quotes."), ephemeral=True)
    await asyncio.to_thread(lambda: db["build_quotes"].update_one(
        {"channel_id": str(channel.id), "builder_id": str(builder.id)},
        {"$set": {"guild_id": str(guild.id), "builder_name": builder.display_name, "price": price,
                  "note": note.strip()[:150], "updated_at": datetime.now(timezone.utc)},
         "$setOnInsert": {"created_at": datetime.now(timezone.utc)}}, upsert=True))
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$pull": {"quote_declined_by": str(builder.id)}}))
    await interaction.response.send_message(
        embed=discord.Embed(description=f"✅ Quote of **{fmt_money(price)}** {'updated' if mine else 'added'} for {builder.mention}.",
                            color=0x57F287), ephemeral=True)
    await refresh_quote_panel(channel, db, doc)


@bm_quote_group.command(name="accept", description="(Build manager) Accept a builder's quote on behalf of the customer")
@app_commands.describe(builder="The builder whose quote to accept")
async def bm_quote_accept(interaction: discord.Interaction, builder: discord.Member):
    ctx = await _manager_ticket(interaction, "bm quote accept")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("kind") != "custom":
        return await interaction.response.send_message(embed=err("This isn't a custom build ticket."), ephemeral=True)
    if doc.get("quote_id"):
        return await interaction.response.send_message(
            embed=err("A quote was already accepted. Use `/bm reset-quote` to pick again."), ephemeral=True)
    quote = await asyncio.to_thread(lambda: db["build_quotes"].find_one(
        {"channel_id": str(channel.id), "builder_id": str(builder.id)}))
    if not quote:
        return await interaction.response.send_message(
            embed=err(f"{builder.mention} has no quote on this ticket. Use `/bm quote add` first."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "quote_id": {"$in": [None]}},
        {"$set": {"quote_id": str(quote["_id"]), "quote_price": int(quote["price"]),
                  "quote_builder_id": quote["builder_id"], "quote_chosen_at": datetime.now(timezone.utc)}}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("A quote was already chosen."), ephemeral=True)
    if doc.get("quote_message_id"):
        try:
            await channel.get_partial_message(int(doc["quote_message_id"])).edit(
                embed=quote_embed(doc["opener_id"], [], chosen=quote),
                view=None if doc.get("paid") else ChangeQuoteView())
        except discord.HTTPException as e:
            logger.warning(f"quote panel close failed: {e}")
    if doc.get("paid") and doc.get("requote_paid"):
        await settle_requote_pick(guild, channel, db, cfg, doc, quote, builder)
    else:
        build = {"_id": "custom", "name": "Custom Build", "price": int(quote["price"])}
        pay_msg = await channel.send(embed=pay_embed_idle(build, cfg), view=PayView(custom=True))
        await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"]}, {"$set": {"pay_message_id": str(pay_msg.id)}}))
    await interaction.followup.send(
        embed=discord.Embed(description=f"✅ Accepted {builder.mention}'s quote of **{fmt_money(int(quote['price']))}**.",
                            color=0x57F287), ephemeral=True)


@bm_group.command(name="repost-quote", description="(Build manager) Post the quote panel again at the bottom of this ticket")
async def repost_quote_cmd(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm repost-quote")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("kind") != "custom":
        return await interaction.response.send_message(
            embed=err("This isn't a custom build ticket, so there's no quote panel."), ephemeral=True)
    if doc.get("awaiting_file") or not doc.get("quote_message_id"):
        return await interaction.response.send_message(
            embed=err("There's no quote panel yet: the customer hasn't sent the `.litematic` "
                      "(use `/bm skip-file` to start the quotes without it)."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if doc.get("quote_id"):         # a quote was picked: show the accepted-quote panel
        try:
            q = await asyncio.to_thread(lambda: db["build_quotes"].find_one({"_id": ObjectId(doc["quote_id"])}))
        except Exception:
            q = None
        if not q:
            return await interaction.followup.send(embed=err("I can't find the accepted quote anymore."), ephemeral=True)
        embed = quote_embed(doc["opener_id"], [], chosen=q)
        view = ChangeQuoteView() if not quote_reset_blocker(doc) else None
    else:
        quotes = await asyncio.to_thread(lambda: list(
            db["build_quotes"].find({"channel_id": str(channel.id)}).sort("created_at", 1)))
        embed, view = quote_embed(doc["opener_id"], quotes), QuotePanelView(quotes)
    try:
        new = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        return await interaction.followup.send(embed=err(f"Couldn't post the panel ({e})."), ephemeral=True)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"quote_message_id": str(new.id)}}))
    try:                            # the old panel is no longer needed
        await channel.get_partial_message(int(doc["quote_message_id"])).delete()
    except (discord.HTTPException, ValueError):
        pass
    await interaction.followup.send(embed=discord.Embed(description="✅ Quote panel reposted.", color=0x57F287),
                                    ephemeral=True)


@bm_group.command(name="skip-file", description="(Build manager) Start the quotes without the customer's .litematic")
async def skip_file_cmd(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm skip-file")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if doc.get("kind") != "custom":
        return await interaction.response.send_message(
            embed=err("Only custom build tickets wait for a `.litematic`."), ephemeral=True)
    if not doc.get("awaiting_file"):
        return await interaction.response.send_message(
            embed=err("This ticket isn't waiting for a file (it was already sent or skipped)."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "awaiting_file": True},
        {"$set": {"awaiting_file": False, "file_skipped_by": str(interaction.user.id),
                  "file_received_at": datetime.now(timezone.utc)}}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("This ticket was just updated. Try again."), ephemeral=True)
    await open_quotes(channel, db, doc, cfg,
                      f"⏭️ {interaction.user.mention} skipped the `.litematic` upload for {'<@' + doc['opener_id'] + '>'}.")
    schedule_recap_refresh(guild.id)
    await interaction.followup.send(embed=discord.Embed(
        description="✅ File step skipped. The quote panel is up.", color=0x57F287), ephemeral=True)


@bm_force_group.command(name="finish", description="(Build manager) Finish this build now, without the customer's approval")
async def force_finish(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm force finish")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if not doc.get("claimed_by"):
        return await interaction.response.send_message(
            embed=err("This ticket hasn't been claimed by a builder yet."), ephemeral=True)
    if doc.get("finished"):
        return await interaction.response.send_message(embed=err("This build is already finished."), ephemeral=True)
    if doc.get("adjust_status"):
        return await interaction.response.send_message(
            embed=err("A price change is still waiting for its payment. Finish or cancel it first."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if not await asyncio.to_thread(begin_finish, db, doc["_id"], None, True):
        return await interaction.followup.send(embed=err("This build is already finished."), ephemeral=True)
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    build = await asyncio.to_thread(ticket_build, db, fresh)
    bm = guild.get_member(int(fresh["claimed_by"]))
    try:
        await asyncio.to_thread(ledger_record_finish, db, fresh, build, None, bm.display_name if bm else "")
    except Exception as e:
        logger.warning(f"builder ledger (force finish) failed: {e}")
    # instant automatic 5-star review (no prompt, no 24h wait)
    try:
        await post_review(guild, db, fresh, 5, AUTO_REVIEW_TEXT, auto=True)
    except Exception as e:
        logger.error(f"force-finish auto review failed: {e}", exc_info=True)
    # tell the customer their build was auto-confirmed as received
    note = ""
    try:
        customer = guild.get_member(int(fresh["opener_id"])) or await guild.fetch_member(int(fresh["opener_id"]))
        await customer.send(embed=discord.Embed(
            title="✅ Build auto-confirmed as received", color=0x57F287,
            description=(f"**{guild.name}** — your build **{build['name'] if build else 'order'}** has been "
                         "**auto confirmed as received** by the build manager.\n\n"
                         f"We've left a {stars(5)} review for you. Thanks for your order! 💚")))
    except discord.HTTPException:
        note = "\n(I couldn't DM the customer, but the 5★ review was posted.)"
    await finalize_finish(guild, db, fresh, cfg, forced_by=interaction.user)
    await interaction.followup.send(embed=discord.Embed(
        description="✅ Build force-finished. 5★ review posted and the customer was notified." + note,
        color=0x57F287), ephemeral=True)


@bm_force_group.command(name="pay", description="(Build manager) Mark this ticket as paid without a payment")
async def force_pay(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm force pay")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    user = interaction.user
    if doc.get("paid"):
        return await interaction.response.send_message(embed=err("This ticket is already paid."), ephemeral=True)
    build = await asyncio.to_thread(ticket_build, db, doc)
    if not build:
        return await interaction.response.send_message(embed=err(
            "This ticket has no price yet (a custom build needs an accepted quote first)."), ephemeral=True)
    if channel.id in CHANNEL_WATCH:
        return await interaction.response.send_message(embed=err(
            "A payment is being tracked in this ticket right now. Cancel it first (or wait for it to end)."),
            ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(timezone.utc)
    price = int(build.get("price") or 0)
    res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"], "paid": {"$ne": True}},
        {"$set": {"paid": True, "paid_by": f"forced ({user.display_name})", "paid_at": now,
                  "paid_forced_by": str(user.id)}}))
    if not res.modified_count:
        return await interaction.followup.send(embed=err("This ticket is already paid."), ephemeral=True)
    receiver = cfg["receiver_ign"] or "—"
    opener = guild.get_member(int(doc["opener_id"]))
    await asyncio.to_thread(lambda: db["build_payments"].insert_one({
        "guild_id": str(guild.id), "channel_id": str(channel.id), "channel_name": channel.name,
        "build_id": str(build["_id"]), "build_name": build["name"],
        "payer": f"forced ({user.display_name})", "receiver": receiver, "amount": price,
        "opener_id": doc["opener_id"], "opener_name": opener.display_name if opener else doc["opener_id"],
        "status": "paid", "forced": True, "forced_by": str(user.id),
        "server_message": f"Force-marked as paid by {user.display_name}",
        "started_at": now, "finished_at": now}))
    # the payment panel in the ticket is no longer needed
    if doc.get("pay_message_id"):
        try:
            await channel.get_partial_message(int(doc["pay_message_id"])).edit(
                embed=discord.Embed(title="✅ Marked as paid", color=discord.Color.green(),
                                    description=f"{user.mention} marked this build as paid."), view=None)
        except discord.HTTPException as e:
            logger.warning(f"force pay: couldn't edit the payment message: {e}")
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    if fresh.get("quote_builder_id") and not fresh.get("claimed_by"):
        bid = fresh["quote_builder_id"]
        try:
            builder = guild.get_member(int(bid)) or await guild.fetch_member(int(bid))
        except (discord.HTTPException, ValueError):
            builder = None
        if builder:
            r2 = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                {"_id": doc["_id"], "claimed_by": {"$in": [None]}}, {"$set": {"claimed_by": bid}}))
            if r2.modified_count:
                await apply_claim(guild, channel, builder, fresh, cfg)
                await asyncio.to_thread(lambda: db["build_payments"].update_many(
                    {"channel_id": str(channel.id), "status": "paid"},
                    {"$set": {"claimed_by": bid, "claimed_by_name": builder.display_name}}))
                try:
                    await channel.send(embed=discord.Embed(
                        title="🙋 Build claimed", color=0x57F287,
                        description=(f"{builder.mention} is now assigned to this build.\n"
                                     "Only the customer, the builder and the build manager can see this ticket now.")),
                        allowed_mentions=discord.AllowedMentions(users=True))
                except discord.HTTPException as e:
                    logger.warning(f"force pay: couldn't send claim message: {e}")
            fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or fresh
    claimer = fresh.get("claimed_by")
    if claimer:
        text = (f"<@{claimer}> **Build Has Been paid**, you can start building! (customer: <@{fresh['opener_id']}>)")
        mentions = discord.AllowedMentions(users=True)
    else:
        ping = f"<@&{cfg['ping_role_id']}> " if cfg["ping_role_id"] else ""
        text = f"{ping}**Build Has Been paid** please use `/builder claim`"
        mentions = discord.AllowedMentions(roles=True)
    try:
        await channel.send(text, allowed_mentions=mentions)
    except discord.HTTPException as e:
        logger.warning(f"force pay: couldn't send paid message: {e}")
    await send_payment_log(channel, cfg, fresh, build, f"forced ({user.display_name})", receiver, price,
                           "", forced_by=user)
    await interaction.followup.send(embed=discord.Embed(
        description=f"✅ Marked as paid ({fmt_money(price)}).", color=0x57F287), ephemeral=True)


@bm_force_group.command(name="unpay", description="Undo a wrong payment mark: the ticket goes back to waiting for payment")
async def force_unpay(interaction: discord.Interaction):
    ctx = await _manager_ticket(interaction, "bm force unpay")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    if not doc.get("paid"):
        return await interaction.response.send_message(embed=err("This ticket isn't marked as paid."), ephemeral=True)
    if doc.get("claimed_by"):
        return await interaction.response.send_message(
            embed=err("A builder is on this ticket. Use `/bm unassign` first. If the customer really paid and wants "
                      "out, use `/bm refund start`."), ephemeral=True)
    if doc.get("finished") or doc.get("adjust_status") or channel.id in CHANNEL_WATCH:
        return await interaction.response.send_message(
            embed=err("Finish / cancel whatever is pending in this ticket first."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]},
        {"$set": {"paid": False}, "$unset": {"paid_by": "", "paid_at": "", "paid_forced_by": ""}}))
    await asyncio.to_thread(lambda: db["build_payments"].update_many(
        {"channel_id": str(channel.id), "status": "paid"},
        {"$set": {"status": "reversed", "reversed_by": str(interaction.user.id)}}))
    build = await asyncio.to_thread(ticket_build, db, doc)
    if doc.get("pay_message_id") and build:
        try:
            await channel.get_partial_message(int(doc["pay_message_id"])).edit(
                content=None, embed=pay_embed_idle(build, cfg), view=PayView(custom=_is_custom(build)))
        except discord.HTTPException as e:
            logger.warning(f"unpay: couldn't restore the payment embed: {e}")
    await channel.send(embed=discord.Embed(
        title="↩️ Payment mark removed", color=0xFEE75C,
        description=f"{interaction.user.mention} marked this ticket as **not paid**. Press **Start Pay** to pay."),
        allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send(embed=discord.Embed(description="✅ Done.", color=0x57F287), ephemeral=True)


@bm_payout_group.command(name="set", description="Change what the builder gets for this finished build (before it is paid)")
@app_commands.describe(amount="What the builder should receive, e.g. 400k, 1.2m")
async def bm_payout_set(interaction: discord.Interaction, amount: str):
    ctx = await _manager_ticket(interaction, "bm payout set")
    if not ctx:
        return
    db, guild, channel, doc, cfg = ctx
    value = parse_price(amount)
    if not value or value <= 0:
        return await interaction.response.send_message(
            embed=err("Give a positive amount like `250k` or `1.5m`."), ephemeral=True)
    if doc.get("payout_status") != "pending":
        return await interaction.response.send_message(embed=err(
            "There's no unpaid payout embed in this ticket (the build must be finished and the payout not started/paid)."),
            ephemeral=True)
    price = int(doc.get("payout_price") or 0)
    pct = round((price - value) * 100 / price, 1) if price else 0
    pct = int(pct) if float(pct).is_integer() else pct
    await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(lambda: db["build_tickets"].update_one(
        {"_id": doc["_id"]}, {"$set": {"payout_amount": value, "payout_fee_percent": pct, "payout_overridden_by": str(interaction.user.id)}}))
    await asyncio.to_thread(lambda: db["builder_ledger"].update_one(
        {"guild_id": doc["guild_id"], "ticket_id": str(doc["_id"]), "edited_by": {"$exists": False}, "paid": {"$ne": True}},
        {"$set": {"received": value}}))
    fresh = await asyncio.to_thread(lambda: db["build_tickets"].find_one({"_id": doc["_id"]})) or doc
    if fresh.get("payout_message_id"):
        try:
            await channel.get_partial_message(int(fresh["payout_message_id"])).edit(embed=payout_embed(fresh))
        except discord.HTTPException as e:
            logger.warning(f"payout set: couldn't refresh the payout embed: {e}")
    await channel.send(f"💲 {interaction.user.mention} set the builder payout to **{fmt_money(value)}**.",
                       allowed_mentions=discord.AllowedMentions.none())
    await interaction.followup.send(embed=discord.Embed(
        description=f"✅ The builder will receive **{fmt_money(value)}**.", color=0x57F287), ephemeral=True)


@builder_group.command(name="file", description="Send the build file again")
async def builder_file(interaction: discord.Interaction):
    ctx = await _ticket_staff_ctx(interaction, "builder file")
    if not ctx:
        return
    db, doc, cfg = ctx
    build = await asyncio.to_thread(ticket_build, db, doc)
    if not build or not build.get("file_id"):
        return await interaction.response.send_message(
            embed=err("This ticket has no stored build file (custom builds use the customer's own upload)."), ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await send_build_file(interaction.channel, db, build)
    await interaction.followup.send(embed=discord.Embed(description="✅ Sent.", color=0x57F287), ephemeral=True)


def _stage_of(d: dict) -> str:
    if d.get("adjust_dir") == "refund" and d.get("adjust_status"):
        return "refund"
    if d.get("finished"):
        return "payout"
    if d.get("finish_status") in ("awaiting_confirm", "reviewing"):
        return "confirm"
    if d.get("claimed_by"):
        return "building"
    if d.get("paid"):
        return "waiting_builder"
    return "unpaid"


@builder_group.command(name="queue", description="Paid builds waiting for a builder + the builds you're working on")
async def builder_queue(interaction: discord.Interaction):
    ctx = await _ticket_staff_ctx(interaction, "builder queue")
    if not ctx:
        return
    db, _doc, cfg = ctx
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    docs = await asyncio.to_thread(lambda: list(db["build_tickets"].find(
        {"guild_id": gid, "status": "open", "finished": {"$ne": True}})))
    mine = [d for d in docs if d.get("claimed_by") == str(interaction.user.id)]
    free = [d for d in docs if d.get("paid") and not d.get("claimed_by")]

    async def line(d):
        b = await asyncio.to_thread(ticket_build, db, d)
        return f"<#{d['channel_id']}> — {(b or {}).get('name', 'Build')} ({fmt_money((b or {}).get('price'))})"
    e = discord.Embed(title="📋 Builder queue", color=brand_color(cfg))
    e.add_field(name=f"🆕 Waiting for a builder ({len(free)})",
                value="\n".join([await line(d) for d in free[:15]])[:1024] or "Nothing right now.", inline=False)
    e.add_field(name=f"🔨 Your builds ({len(mine)})",
                value="\n".join([await line(d) for d in mine[:15]])[:1024] or "None.", inline=False)
    e.set_footer(text="Claim one with /builder claim inside the ticket")
    await interaction.followup.send(embed=e, ephemeral=True)


@bm_group.command(name="queue", description="Every open ticket grouped by what it's waiting for")
async def bm_queue(interaction: discord.Interaction):
    ctx = await _bm_gate(interaction, "bm queue")
    if not ctx:
        return
    db, cfg = ctx
    await interaction.response.defer(ephemeral=True)
    docs = await asyncio.to_thread(lambda: list(db["build_tickets"].find(
        {"guild_id": str(interaction.guild.id), "status": "open"})))
    groups = {"refund": [], "payout": [], "confirm": [], "building": [], "waiting_builder": [], "unpaid": []}
    for d in docs:
        groups[_stage_of(d)].append(d)
    titles = {"refund": "↩️ Refund waiting for you", "payout": "💸 Finished — builder payout",
              "confirm": "📨 Waiting for the customer to confirm", "building": "🔨 Being built",
              "waiting_builder": "🆕 Paid — no builder yet", "unpaid": "💳 Waiting for payment"}
    e = discord.Embed(title="📋 Open tickets", color=brand_color(cfg), description=f"{len(docs)} open")
    for k in ("refund", "payout", "confirm", "building", "waiting_builder", "unpaid"):
        rows = []
        for d in groups[k][:12]:
            who = f" · <@{d['claimed_by']}>" if d.get("claimed_by") else ""
            extra = ""
            if k == "payout" and d.get("payout_status") == "awaiting_ign":
                extra = " ⚠️ no IGN"
            rows.append(f"<#{d['channel_id']}>{who}{extra}")
        if rows:
            more = f"\n…and {len(groups[k]) - 12} more" if len(groups[k]) > 12 else ""
            e.add_field(name=f"{titles[k]} ({len(groups[k])})", value=("\n".join(rows) + more)[:1024], inline=False)
    await interaction.followup.send(embed=e, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

def _ensure_indexes(db):
    """Indexes for the recap queries (it runs ~8 aggregations per refresh). Safe to run on every start."""
    if db is None:
        return
    try:
        db["build_payments"].create_index([("guild_id", 1), ("status", 1)])
        db["build_payments"].create_index([("channel_id", 1), ("status", 1)])
        db["build_tickets"].create_index([("guild_id", 1), ("status", 1), ("claimed_by", 1)])
        db["build_tickets"].create_index([("channel_id", 1)])
        db["builder_ledger"].create_index([("guild_id", 1), ("paid", 1)])
        db["build_payouts"].create_index([("guild_id", 1), ("status", 1)])
        db["owner_payouts"].create_index([("guild_id", 1), ("status", 1)])
    except Exception as e:
        logger.warning(f"recap indexes: {e}")


class Builds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(BuildPanelView(register=True))
        self.bot.add_view(BuildTicketView())
        self.bot.add_view(QuotePanelView(register=True))
        self.bot.add_view(ChangeQuoteView())
        self.bot.add_view(PayView(custom=True))
        self.bot.add_view(WaitingView(custom=True))
        self.bot.add_view(FinishConfirmView())
        self.bot.add_view(ReviewView())
        self.bot.add_view(PayoutView())
        self.bot.add_view(PayoutWaitingView())
        self.bot.add_view(AdjustView())
        self.bot.add_view(AdjustWaitingView())
        self.bot.add_view(RecapView())
        self.bot.add_view(OwnerSettleWaitingView())
        self.bot.tree.add_command(builder_group, override=True)
        self.bot.tree.add_command(bm_group, override=True)
        global _BOT
        _BOT = self.bot
        await asyncio.to_thread(_ensure_indexes, getattr(self.bot, "db", None))
        self.refresh_panels.start()
        self.refresh_recap_panels.start()
        self.auto_reviews.start()

    async def cog_unload(self):
        self.refresh_panels.cancel()
        self.refresh_recap_panels.cancel()
        self.auto_reviews.cancel()
        self.bot.tree.remove_command("builder")
        self.bot.tree.remove_command("bm")












    async def _help_gate(self, interaction: discord.Interaction, name: str, manager_only: bool):
        """cfg if the user may use this help command; else replies and None."""
        if not await check_enabled_or_reply(interaction, name):
            return None
        db = interaction.client.db
        if db is None or interaction.guild is None:
            await interaction.response.send_message(embed=err("Database unavailable."), ephemeral=True)
            return None
        cfg = await asyncio.to_thread(_get_settings, db, interaction.guild.id)
        if manager_only:
            ok, who = is_manager(interaction.user, cfg), "admins or the build manager role"
        else:       # builders (allowed roles); build managers can use it too
            ok, who = is_ticket_staff(interaction.user, cfg), "builders and the build manager"
        if not ok:
            await interaction.response.send_message(embed=err(f"Only {who} can use this."), ephemeral=True)
            return None
        return cfg

    @app_commands.command(name="bhelp", description="(Builders) How to take and finish builds")
    @app_commands.guild_only()
    async def bhelp(self, interaction: discord.Interaction):
        cfg = await self._help_gate(interaction, "bhelp", manager_only=False)
        if cfg is None:
            return
        e = discord.Embed(title="🛠️ Builder help", color=brand_color(cfg),
                          description="Everything for builders lives under `/builder`. Run it inside the ticket unless noted.")
        e.add_field(name="📥 Get a build", inline=False, value=(
            "`/builder queue` — paid builds waiting for a builder + yours (works anywhere)\n"
            "`/builder claim` — claim a **paid** ticket\n"
            "**Submit Quote** / **Withdraw Quote** buttons — custom builds; if the customer picks you, "
            "you're auto-claimed once they pay"))
        e.add_field(name="🔨 While building", inline=False, value=(
            "`/builder info` — price, paid, stage, your payout, anything pending\n"
            "`/builder file` — send the build file again\n"
            "`/builder rename <name>` — rename the channel"))
        e.add_field(name="✅ Finish & get paid", inline=False, value=(
            "`/builder finish` — you only. The customer confirms in DMs and reviews\n"
            f"You get the price minus the **{cfg['fee_percent']}%** fee. The build manager pays you in the ticket, you get a DM"))
        e.add_field(name="🎮 Your account", inline=False, value=(
            "`/builder ign set <ign>` · `/builder ign change <ign>` — payout IGN (verified)\n"
            "`/builder stats` — builds done, earned, still owed, rating"))
        e.add_field(name="🆘 Something's wrong?", inline=False, value=(
            "Can't finish it → `/builder unclaim` (back to the pool)\n"
            "Customer wants their money back → ask the build manager\n"
            "Ticket not needed and unpaid → `/builder close`\n"
            "Wrong price, payment not detected, stuck payout → tell the build manager"))
        e.set_footer(text="Build managers: /bmhelp")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="bmhelp", description="(Build managers) All build manager commands")
    @app_commands.guild_only()
    async def bmhelp(self, interaction: discord.Interaction):
        cfg = await self._help_gate(interaction, "bmhelp", manager_only=True)
        if cfg is None:
            return
        e = discord.Embed(title="🧰 Build manager help", color=brand_color(cfg),
                          description="Everything for build managers lives under `/bm`. You can also use all of `/bhelp`.")
        e.add_field(name="📊 Overview", inline=False, value=(
            "`/bm queue` — every open ticket by what it's waiting for\n"
            "`/bm recap` — posts the live recap panel (received, our fee, owner paid / left, builds left to pay, "
            "refunds, builders in progress). It updates itself; buttons settle the owner and list the builds\n"
            "`/builder stats [builder]` — a builder's totals and what they're owed\n"
            "`/builder info` — one ticket in detail · `/bm panel` — post the build panel here\n"
            "Fee %, roles, channels: dashboard"))
        e.add_field(name="💳 Payment problems", inline=False, value=(
            "Paid but not detected → `/bm force pay`\n"
            "Marked paid by mistake → `/bm force unpay` (no builder on it)\n"
            "Wrong price before they pay → `/bm price set <amount>`\n"
            "Price changes after they paid → `/bm price more <amount>` (customer pays extra) or "
            "`/bm price less <amount>` (you pay the difference). Builder stays\n"
            "That change is stuck → `/bm price cancel` or `/bm price force`"))
        e.add_field(name="↩️ Refunds", inline=False, value=(
            "`/bm refund start [percentage]` — you pay the customer (100% by default, or a % — the rest is our fee), builder unclaimed with no payout, customer DMed, ticket closed\n"
            "`/bm refund cancel` — keep the ticket\n"
            "`/bm refund force` — you already paid by hand: same steps, no tracking"))
        e.add_field(name="👷 Builder problems", inline=False, value=(
            "`/bm assign <builder>` — put someone on it (replaces the current one)\n"
            "`/bm unassign <builder>` — take a builder off\n"
            "`/bm cancel-pay [keep_quote]` — stop a running payment; by default also resets the quote so the customer picks again\n"
            "`/bm reset-quote` — customer picks a different quote\n"
            "`/bm repost-quote` — post the quote panel again at the bottom of the ticket\n"
            "`/bm skip-file` — start the quotes without waiting for the customer's `.litematic`\n"
            "`/bm ign set <builder> <ign>` · `/bm ign remove <builder>` — payout IGN"))
        e.add_field(name="🏁 Finishing & paying builders", inline=False, value=(
            "Customer never confirms → `/bm force finish` (instant 5★ auto review)\n"
            "**Pay Now** on the payout embed — pay the builder\n"
            "Different amount for the builder → `/bm payout set <amount>` (before paying)\n"
            "Paid the builder by hand → `/bm force builder-paid`\n"
            "Paid the owner by hand → `/bm force owner-paid [amount]` (the recap's **Settle Owner Amount** button tracks it for you)"))
        e.add_field(name="🎫 Ticket control", inline=False, value=(
            "`/bm ticket add <member>` · `/bm ticket remove <member>` — access\n"
            "`/builder rename` · `/builder close` (unpaid only)\n"
            "`/bm ticket force-close [reason]` — delete even if paid (warns if the customer wasn't refunded)"))
        await interaction.response.send_message(embed=e, ephemeral=True)



    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        """Closed (or manually deleted) ticket -> remove the build category if it's now empty."""
        if isinstance(channel, discord.CategoryChannel) or not getattr(channel, "category_id", None):
            return
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            await delete_category_if_empty(channel.guild, db, channel.category_id)
        except Exception as e:
            logger.warning(f"build category cleanup: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Custom build tickets: when the ticket creator sends their .litematic, ping the ping role."""
        if message.author.bot or message.guild is None or not message.attachments:
            return
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        doc = await asyncio.to_thread(lambda: db["build_tickets"].find_one(
            {"channel_id": str(message.channel.id), "status": "open", "kind": "custom",
             "awaiting_file": True, "opener_id": str(message.author.id)}))
        if not doc:
            return
        files = [a for a in message.attachments if a.filename.lower().endswith(".litematic")]
        if not files:
            return await message.reply("⚠️ Please send a `.litematic` file.", mention_author=False)
        res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
            {"_id": doc["_id"], "awaiting_file": True},
            {"$set": {"awaiting_file": False, "file_name": files[0].filename,
                      "file_received_at": datetime.now(timezone.utc)}}))
        if not res.modified_count:
            return
        cfg = await asyncio.to_thread(_get_settings, db, message.guild.id)
        await open_quotes(message.channel, db, doc, cfg,
                          f"📥 **Litematic file received** from {message.author.mention}: `{files[0].filename}`")

    # ---- build manager overrides -------------------------------------------



    # ---- automatic 5★ review when the customer doesn't review in 24h ----------
    @tasks.loop(minutes=5)
    async def auto_reviews(self):
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            due = await asyncio.to_thread(lambda: list(db["build_tickets"].find(
                {"review_status": "pending", "review_due_at": {"$lte": datetime.now(timezone.utc)}}).limit(25)))
        except Exception as e:
            logger.warning(f"auto review lookup failed: {e}")
            return
        for doc in due:
            guild = self.bot.get_guild(int(doc["guild_id"]))
            if guild is None:
                continue
            res = await asyncio.to_thread(lambda: db["build_tickets"].update_one(
                {"_id": doc["_id"], "review_status": "pending"},
                {"$set": {"review_status": "auto", "review_rating": 5}}))
            if not res.modified_count:
                continue
            try:
                await post_review(guild, db, doc, 5, AUTO_REVIEW_TEXT, auto=True)
            except Exception as e:
                logger.error(f"auto review failed: {e}", exc_info=True)
            if doc.get("finish_dm_message_id"):        # close the review prompt in the customer's DMs
                try:
                    user = await self.bot.fetch_user(int(doc["opener_id"]))
                    dm = user.dm_channel or await user.create_dm()
                    await dm.get_partial_message(int(doc["finish_dm_message_id"])).edit(
                        embed=discord.Embed(
                            title="💚 Thanks for your order!", color=0x57F287,
                            description=f"The review window closed, so we left a {stars(5)} review for you."),
                        view=None)
                except discord.HTTPException:
                    pass
            await asyncio.sleep(0.5)

    @auto_reviews.before_loop
    async def _before_auto_reviews(self):
        await self.bot.wait_until_ready()

    # Builds are added on the dashboard, so keep already-posted panels in sync.
    @tasks.loop(seconds=45)
    async def refresh_panels(self):
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            panels = await asyncio.to_thread(lambda: list(db["build_panels"].find()))
        except Exception as e:
            logger.warning(f"build panel refresh: {e}")
            return
        by_guild: dict = {}
        for p in panels:
            guild = self.bot.get_guild(int(p["guild_id"]))
            if guild is None:
                continue
            if p["guild_id"] not in by_guild:
                by_guild[p["guild_id"]] = await asyncio.to_thread(_get_builds, db, guild.id)
            builds = by_guild[p["guild_id"]]
            pcfg = await asyncio.to_thread(_get_settings, db, guild.id)
            ppb = pcfg["price_per_block"]
            sig = panel_signature(builds, ppb, pcfg)
            if sig == p.get("sig"):
                continue
            channel = guild.get_channel(int(p["channel_id"]))
            if channel is None:
                await asyncio.to_thread(lambda: db["build_panels"].delete_one({"_id": p["_id"]}))
                continue
            try:
                images = await load_panel_images(db, pcfg)
                await channel.get_partial_message(int(p["message_id"])).edit(
                    embed=panel_embed(guild, builds, ppb, pcfg, images), view=BuildPanelView(builds, digout=bool(ppb)),
                    attachments=panel_files(images))
            except discord.NotFound:
                await asyncio.to_thread(lambda: db["build_panels"].delete_one({"_id": p["_id"]}))
                continue
            except discord.HTTPException as e:
                logger.warning(f"build panel edit failed: {e}")
                continue
            await asyncio.to_thread(lambda: db["build_panels"].update_one({"_id": p["_id"]}, {"$set": {"sig": sig}}))
            await asyncio.sleep(0.5)

    @refresh_panels.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # The live recap panel: money moves from many places, so also re-check it on a timer.
    @tasks.loop(seconds=45)
    async def refresh_recap_panels(self):
        await refresh_recaps(self.bot)

    @refresh_recap_panels.before_loop
    async def _before_refresh_recaps(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Builds(bot))
