import asyncio
from datetime import datetime, timezone
import logging
import re
import sys
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.stats import ERROR_MESSAGES, MC_BOT_URL, STATS_WORKER_DISCORD_ID, check_command_perm, lookup_stats

logger = logging.getLogger(__name__)

_ign_re = re.compile(r"^\.?[A-Za-z0-9_]{1,24}$")


def fmt_ign_ok(ign: str) -> bool:
    return bool(_ign_re.match(ign))


def parse_amount(text: str):
    """'10k' -> 10000, '1.5m' -> 1500000, '100m', '1,000' -> 1000. None if invalid."""
    m = re.fullmatch(r"\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kmbt])?\s*", str(text).lower())
    if not m:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}[m.group(2) or ""]
    val = round(float(m.group(1).replace(",", "")) * mult)
    return int(val) if val > 0 else None


class CancelView(discord.ui.View):
    """A single Cancel button. Only the person who ran the command (or a server
    admin) can press it. `on_cancel(interaction)` does the actual cancelling and
    must respond to the interaction."""

    def __init__(self, owner_id: int, on_cancel, timeout: float):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.on_cancel = on_cancel

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = bool(interaction.guild and getattr(interaction.user, "guild_permissions", None)
                        and interaction.user.guild_permissions.administrator)
        if interaction.user.id != self.owner_id and not is_admin:
            await interaction.response.send_message("❌ Only the person who started this can cancel it.", ephemeral=True)
            return
        self.stop()
        await self.on_cancel(interaction)


# /pay - confirm a payment by "following" the receiver in-game.
# The worker account runs `/follow <receiver>`; the server then prints that
# player's transactions in chat ("Wasx_ paid wiacek71 $ 5" or
# "wiacek71 was paid $ 5 by Wasx_"). Amounts are abbreviated (1500470 shows as
# "1.5m"), so the match is on payer + receiver + an approximate amount.
WATCH_SECONDS = 5 * 60
POLL_GAP_SECONDS = 3
MAX_ACTIVE_WATCHES = 8


def amount_matches(value: float, shown: float, res: float) -> bool:
    """Is `value` something the server could have displayed as `shown`?
    The display may truncate (1.56m -> 1.5m) or round (1.56m -> 1.6m), so accept
    anything from half a step below to one full step above the shown number."""
    diff = value - shown
    return -res / 2 <= diff < res + 1e-9


def fmt_money(n: float) -> str:
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            return f"${n / div:,.2f}{suf}"
    return f"${n:,.0f}"


async def _sessions(db):
    if not STATS_WORKER_DISCORD_ID or db is None:
        return []
    try:
        docs = await asyncio.to_thread(
            lambda: list(db["mc_sessions"].find({"discord_id": str(STATS_WORKER_DISCORD_ID)}, {"session_id": 1}))
        )
    except Exception as e:
        logger.error(f"/pay: couldn't read mc_sessions: {e}")
        return []
    return [d["session_id"] for d in docs]


async def _get(session, path, **params):
    async with session.get(f"{MC_BOT_URL}{path}", params=params) as resp:
        return await resp.json()


ERRORS = {
    "not_configured": "the worker account isn't configured.",
    "no_session": "the worker account isn't linked.",
    "not_ready": "the worker account is offline right now.",
    "busy": "the account is already following someone else for another /pay.",
    "not_found": "no player with that name was found.",
    "bad_user": "that doesn't look like a valid Minecraft username.",
    "error": "the bot couldn't send `/follow`.",
}


def ign_cache_add(db, ign: str):
    """Remember an IGN that was just confirmed to exist (collection verified_igns; see cogs/builds.py).
    Never overwrites an existing entry (so admin-added ones stay "admin")."""
    if db is None:
        return
    try:
        db["verified_igns"].update_one(
            {"ign_lower": ign.lower()},
            {"$setOnInsert": {"ign": ign, "source": "bot", "added_at": datetime.now(timezone.utc),
                              "added_by": "bot", "uses": 0}},
            upsert=True)
    except Exception as e:
        logger.warning(f"verified ign cache write failed: {e}")


WORKER_GAP = 1.0               # seconds between console commands sent to the worker account
_worker_lock = None
_worker_last = 0.0


async def paced(fn, *args, **kwargs):
    """Run one worker/console command (IGN lookup, follow-start) so that at least WORKER_GAP
    seconds pass after the previous one finished - across every ticket and command - instead of firing back to back."""
    global _worker_lock, _worker_last
    if _worker_lock is None:
        _worker_lock = asyncio.Lock()
    async with _worker_lock:
        wait = _worker_last + WORKER_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await fn(*args, **kwargs)
        finally:
            _worker_last = time.monotonic()


def receiver_in_use(bot, receiver: str, ignore_watch_key=None) -> bool:
    """True if ANOTHER payment watch still needs the worker account to follow this receiver.
    Looks at every flow: /pay, /fpay and everything in cogs/builds.py (build pays, builder payouts,
    owner settle, extra/refund adjust - they all live in builds.WATCHES)."""
    r = (receiver or "").lower()
    builds = sys.modules.get("cogs.builds")
    if builds is not None:
        for k, w in list(getattr(builds, "WATCHES", {}).items()):
            if k != ignore_watch_key and str(w.get("receiver", "")).lower() == r:
                return True
    for name in ("Pay", "FPay"):
        cog = bot.get_cog(name) if bot is not None else None
        if cog is not None and any(str(w.get("receiver", "")).lower() == r for w in list(cog.watches.values())):
            return True
    return False


async def release_follow(bot, sid, receiver: str, ignore_watch_key=None, who: str = "pay") -> bool:
    """Tell the worker account to stop following `receiver` - unless another payment still needs it.
    Call it the moment a payment is confirmed / cancelled / timed out, in EVERY flow, so the account is free
    for the next payment to a different player. Retries a couple of times; the MC bot only stops if it is still
    following that receiver, so calling it twice is harmless."""
    if receiver_in_use(bot, receiver, ignore_watch_key):
        return False
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
                await _get(http, f"/follow-stop/{sid}", receiver=receiver)
            return True
        except Exception as e:
            logger.warning(f"{who}: follow-stop failed (try {attempt + 1}/3): {e}")
            await asyncio.sleep(1)
    return False


def _ign_cache_get(db, ign: str) -> bool:
    try:
        r = db["verified_igns"].update_one({"ign_lower": ign.lower()},
                                           {"$set": {"last_used_at": datetime.now(timezone.utc)}, "$inc": {"uses": 1}})
        return bool(r.matched_count)
    except Exception as e:
        logger.warning(f"verified ign cache read failed: {e}")
        return False


async def check_ign(db, ign: str):
    """None if the IGN exists, else the error code ("not_found", ...). IGNs already in the verified list
    (collection verified_igns) skip the in-game lookup; new ones are looked up once and then remembered."""
    if db is not None and await asyncio.to_thread(_ign_cache_get, db, ign):
        return None
    stats, code = await paced(lookup_stats, db, ign)
    if code is None:
        await asyncio.to_thread(ign_cache_add, db, ign)
    return code


class Pay(commands.Cog):
    """/pay <payer> <receiver> <amount> - follows the receiver in-game and
    confirms when the server shows 'payer paid receiver $amount'."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watches: dict[tuple, dict] = {}   # (payer, receiver) lower -> info

    @app_commands.command(name="pay", description="Confirm a payment by following the receiver in-game (5 minutes).")
    @app_commands.describe(payer="IGN of the player paying", receiver="IGN of the player receiving",
                           amount="Amount, e.g. 100k, 1.5m, 2b")
    async def pay(self, interaction: discord.Interaction, payer: str, receiver: str, amount: str):
        if not await check_command_perm(interaction, "pay"):
            return
        payer, receiver = payer.strip(), receiver.strip()
        value = parse_amount(amount)
        if not fmt_ign_ok(payer) or not fmt_ign_ok(receiver):
            await interaction.response.send_message("❌ Invalid IGN.", ephemeral=True)
            return
        if payer.lower().lstrip(".") == receiver.lower().lstrip("."):
            await interaction.response.send_message("❌ Payer and receiver can't be the same player.", ephemeral=True)
            return
        if value is None:
            await interaction.response.send_message("❌ Invalid amount. Use something like `250k`, `100m` or `1.5b`.", ephemeral=True)
            return
        key = (payer.lower(), receiver.lower())
        if key in self.watches:
            await interaction.response.send_message("❌ That payment is already being watched.", ephemeral=True)
            return
        if len(self.watches) >= MAX_ACTIVE_WATCHES:
            await interaction.response.send_message("❌ Too many payments are being watched right now. Try again shortly.", ephemeral=True)
            return
        other = next((w for w in self.watches.values() if w["receiver"].lower() != receiver.lower()), None)
        if other:
            await interaction.response.send_message(
                f"❌ I'm already following **{other['receiver']}** for another payment "
                f"(free <t:{int(other['ends'])}:R>). Try again then.", ephemeral=True)
            return

        await interaction.response.defer()
        db = getattr(self.bot, "db", None)
        code = await check_ign(db, payer)          # the payer must exist too (known IGNs skip the lookup)
        if code == "not_found":
            await interaction.followup.send(f"❌ No player named **{payer}** was found. Check the spelling.")
            return
        if code:
            await interaction.followup.send(f"❌ Couldn't verify the payer **{payer}**: {ERROR_MESSAGES.get(code, code)}")
            return
        if key in self.watches:                     # someone started it while we were checking
            await interaction.followup.send("❌ That payment is already being watched.")
            return
        sids = await _sessions(db)
        if not sids:
            await interaction.followup.send(f"❌ Couldn't start: {ERRORS['not_configured' if not STATS_WORKER_DISCORD_ID else 'no_session']}")
            return

        started = None
        err = "not_ready"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for sid in sids:
                try:
                    data = await _get(http, f"/follow-start/{sid}", receiver=receiver, seconds=WATCH_SECONDS + 30)
                except Exception as e:
                    logger.warning(f"/pay: follow-start failed: {e}")
                    err = "not_ready"
                    continue
                if data.get("ok"):
                    started = (sid, data)
                    break
                err = data.get("error_code") or "error"
                logger.warning(f"/pay: follow-start: {err} - {data.get('error')}")
                if err != "not_ready":
                    break
        if not started:
            if err == "not_found":
                await interaction.followup.send(f"❌ No player named **{receiver}** was found. Check the spelling.")
            else:
                await interaction.followup.send(f"❌ Couldn't start following **{receiver}**: {ERRORS.get(err, err)}")
            return
        sid, data = started
        await asyncio.to_thread(ign_cache_add, db, receiver)      # the receiver exists: remember it
        logger.info(f"/pay: following {receiver}; server said: {data.get('chat')}")

        ends = time.time() + WATCH_SECONDS
        cmd_amount = amount.strip().lstrip("$").replace(" ", "")
        embed = discord.Embed(
            title="💸 Payment Request", color=discord.Color.from_rgb(255, 170, 0),
            description=(f"Run this command in-game to pay:\n"
                         f"```/pay {receiver} {cmd_amount}```"))
        embed.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
        embed.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
        embed.add_field(name="💰 Amount", value=f"**{fmt_money(value)}**", inline=True)
        embed.add_field(name="⏳ Time to pay", value=f"**5 minutes** — expires <t:{int(ends)}:R>", inline=False)
        embed.set_footer(text="Waiting for the payment to show up in-game…")
        cancel = asyncio.Event()

        async def on_cancel(i: discord.Interaction):
            cancel.set()
            logger.info(f"/pay cancelled by {i.user} ({payer} -> {receiver})")
            await i.response.edit_message(
                embed=discord.Embed(title="🚫 Cancelled", color=discord.Color.dark_grey(),
                                    description=f"Stopped watching for **{payer}** → **{receiver}**."),
                view=None)

        view = CancelView(interaction.user.id, on_cancel, timeout=WATCH_SECONDS + 30)
        msg = await interaction.followup.send(embed=embed, view=view, wait=True)
        self.watches[key] = {"receiver": receiver, "ends": ends}
        try:
            await self._watch(sid, msg, payer, receiver, value, ends, int(data.get("last_id") or 0), cancel)
        finally:
            self.watches.pop(key, None)
            await release_follow(self.bot, sid, receiver, who="/pay")

    async def _watch(self, sid, msg, payer, receiver, value, ends, since, cancel):
        near_misses = []
        misses = 0
        pl, rl = payer.lower().lstrip("."), receiver.lower().lstrip(".")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            while time.time() < ends:
                await asyncio.sleep(POLL_GAP_SECONDS)
                if cancel.is_set():
                    return   # the Cancel button already updated the message
                try:
                    data = await _get(http, f"/follow-events/{sid}", since=since)
                except Exception as e:
                    misses += 1
                    logger.warning(f"/pay: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    logger.warning("/pay: the follow watch ended on the MC bot side.")
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != pl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(value, float(ev["value"]), float(ev["res"])):
                        logger.info(f"/pay confirmed: {ev['raw']} (wanted {value})")
                        done = discord.Embed(
                            title="✅ Money Received", color=discord.Color.green(),
                            description=f"**{receiver}** received **{fmt_money(value)}** from **{payer}**!")
                        done.add_field(name="👤 Payer", value=f"`{payer}`", inline=True)
                        done.add_field(name="🎯 Receiver", value=f"`{receiver}`", inline=True)
                        done.add_field(name="💰 Amount", value=f"**{fmt_money(value)}**", inline=True)
                        done.add_field(name="🧾 Server message", value=f"```{ev['raw']}```", inline=False)
                        done.set_footer(text="Payment confirmed")
                        await self._edit(msg, done)
                        return
                    near_misses.append(ev["raw"])
                    logger.info(f"/pay: {payer}->{receiver} payment seen but amount differs: {ev['raw']} (wanted {value})")

        desc = f"No payment from **{payer}** to **{receiver}** of about **{fmt_money(value)}** was seen in 5 minutes."
        if near_misses:
            desc += "\nI did see a payment between them with a different amount:\n```" + "\n".join(near_misses[-3:]) + "```"
        if misses:
            desc += f"\n({misses} status check(s) failed while watching.)"
        await self._edit(msg, discord.Embed(title="⌛ Payment not detected", color=discord.Color.red(), description=desc))

    @staticmethod
    async def _edit(msg, e):
        try:
            await msg.edit(embed=e, view=None)
        except discord.HTTPException as ex:
            logger.warning(f"/pay: couldn't edit message: {ex}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Pay(bot))
