import asyncio
import logging
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.stats import ERROR_MESSAGES, MC_BOT_URL, STATS_WORKER_DISCORD_ID, check_command_perm
from cogs.pay import parse_amount, fmt_ign_ok, CancelView, check_ign, ign_cache_add, release_follow

logger = logging.getLogger(__name__)

# /fpay - confirm a payment by "following" the receiver in-game.
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
        logger.error(f"/fpay: couldn't read mc_sessions: {e}")
        return []
    return [d["session_id"] for d in docs]


async def _get(session, path, **params):
    async with session.get(f"{MC_BOT_URL}{path}", params=params) as resp:
        return await resp.json()


ERRORS = {
    "not_configured": "the worker account isn't configured.",
    "no_session": "the worker account isn't linked.",
    "not_ready": "the worker account is offline right now.",
    "busy": "the account is already following someone else for another /fpay.",
    "not_found": "no player with that name was found.",
    "bad_user": "that doesn't look like a valid Minecraft username.",
    "error": "the bot couldn't send `/follow`.",
}


class FPay(commands.Cog):
    """/fpay <payer> <receiver> <amount> - follows the receiver in-game and
    confirms when the server shows 'payer paid receiver $amount'."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.watches: dict[tuple, dict] = {}   # (payer, receiver) lower -> info

    @app_commands.command(name="fpay", description="Confirm a payment by following the receiver in-game (5 minutes).")
    @app_commands.describe(payer="IGN of the player paying", receiver="IGN of the player receiving",
                           amount="Amount, e.g. 100k, 1.5m, 2b")
    async def fpay(self, interaction: discord.Interaction, payer: str, receiver: str, amount: str):
        if not await check_command_perm(interaction, "fpay"):
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
                    logger.warning(f"/fpay: follow-start failed: {e}")
                    err = "not_ready"
                    continue
                if data.get("ok"):
                    started = (sid, data)
                    break
                err = data.get("error_code") or "error"
                logger.warning(f"/fpay: follow-start: {err} - {data.get('error')}")
                if err != "not_ready":
                    break
        if not started:
            if err == "not_found":
                await interaction.followup.send(f"❌ No player named **{receiver}** was found. Check the spelling.")
            else:
                await interaction.followup.send(f"❌ Couldn't start following **{receiver}**: {ERRORS.get(err, err)}")
            return
        sid, data = started
        await asyncio.to_thread(ign_cache_add, getattr(self.bot, "db", None), receiver)   # the receiver exists
        logger.info(f"/fpay: following {receiver}; server said: {data.get('chat')}")

        ends = time.time() + WATCH_SECONDS
        embed = discord.Embed(
            title="⏳ Waiting for payment", color=discord.Color.orange(),
            description=(f"Watching **{receiver}** for a payment from **{payer}** of about **{fmt_money(value)}**.\n"
                         f"Send it in-game now. The server rounds amounts (1,500,470 shows as 1.5m), so I match on the "
                         f"names and the rounded amount.\nWatching until <t:{int(ends)}:R>."))
        cancel = asyncio.Event()

        async def on_cancel(i: discord.Interaction):
            cancel.set()
            logger.info(f"/fpay cancelled by {i.user} ({payer} -> {receiver})")
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
            await release_follow(self.bot, sid, receiver, who="/fpay")

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
                    logger.warning(f"/fpay: poll miss #{misses}: {e}")
                    continue
                if not data.get("active", True) and not data.get("events"):
                    logger.warning("/fpay: the follow watch ended on the MC bot side.")
                    break
                for ev in data.get("events", []):
                    since = max(since, int(ev["id"]))
                    if ev["payer"].lower().lstrip(".") != pl or ev["receiver"].lower().lstrip(".") != rl:
                        continue
                    if amount_matches(value, float(ev["value"]), float(ev["res"])):
                        logger.info(f"/fpay confirmed: {ev['raw']} (wanted {value})")
                        await self._edit(msg, discord.Embed(
                            title="✅ Payment confirmed", color=discord.Color.green(),
                            description=(f"**{payer}** paid **{receiver}** — the server showed:\n```{ev['raw']}```"
                                         f"Matches {fmt_money(value)} (the server only shows it rounded).")))
                        return
                    near_misses.append(ev["raw"])
                    logger.info(f"/fpay: {payer}->{receiver} payment seen but amount differs: {ev['raw']} (wanted {value})")

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
            logger.warning(f"/fpay: couldn't edit message: {ex}")


async def setup(bot: commands.Bot):
    await bot.add_cog(FPay(bot))
