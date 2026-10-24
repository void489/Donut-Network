"""
Conversational AI: @mention the bot (or reply to it) and it chats back.

Uses Groq first, then OpenRouter free models (see utils/llm.py). Chat is
conversational only: it remembers people, can search the web, and can look up
DonutSMP player stats - but ONLY for the bot owner, and in ANY channel (no
stats-channel lock). Everyone else gets normal chat with no stats fetching.
It can't moderate or change anything.

There are NO slash commands for this. AI chat and web search are switched on per
server from the admin panel (Bot Servers page) and are OFF by default. Settings
live in guild_settings.ai_chat {enabled, search_enabled}.
"""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from database import models
from utils import llm, memory
from utils.personality import PERSONALITY, OWNER_SPECIAL_CASE, OWNER_USER_ID, OWNER_NAME
from cogs.stats import lookup_stats, ERROR_MESSAGES

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 4
CHUNK = 1900

NO_ACTIONS = """You cannot moderate or change anything on the server from this chat (no mute,
kick, ban, roles, tickets, giveaways, etc.). If asked, say plainly that you can't do that.
Never claim to have taken an action you didn't take."""

STATS_OWNER = """

STATS: you have a get_player_stats tool that reads a DonutSMP player's live stats (money,
shards, kills, deaths, playtime, blocks placed/broken, mobs killed, online/offline). It works
in ANY channel - there is no designated stats channel for you, so never tell the owner to go
somewhere else. Use it whenever he asks about a player's stats, using the exact Minecraft
username. Report the numbers exactly as the tool returned them; never guess or make up stats.
If the tool returns an error, tell him that error plainly. A lookup can take a little while -
that's normal."""

STATS_OTHERS = """

STATS: you can't look up DonutSMP player stats for this person. If they ask for a player's
stats, tell them you can't fetch stats in chat and to use the /stats command instead. Never
guess or make up stats."""

SYSTEM_PROMPT = PERSONALITY + """

The above is tone only - the rules below always win.

You are {bot_name}, chatting in the Discord server "{guild_name}". Keep replies
very short and low-effort, like a text message (usually one line). Never write long
paragraphs, lists or headers. You may
have a web_search tool for current events or facts you're unsure about; use it
only when it helps. If search errors, say you can't search right now instead of
pretending you looked something up.

{action_rules}

MEMORY: you have a real memory of past conversations. Each message comes with a
memory block: what you remember about the person talking to you (summary, topics
you've discussed and when, facts), who you were talking to in this channel and
what each of you said, and - if they replied to one of your messages - which
message that was and who you originally sent it to. Use it naturally: keep track
of which person said what and don't mix people up, pick up earlier topics when
relevant ("last time we were on X"), and don't repeat things you already told
them. Never invent memories - if the memory block doesn't say it, you don't know
it. You also have recall_user and search_memory tools to look up other people or
past topics."""


MEMORY_RECALL_TOOL = {"type": "function", "function": {
    "name": "recall_user",
    "description": "Look up what you remember about a specific person (topics, summary, facts) by Discord user ID.",
    "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}}}
MEMORY_SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_memory",
    "description": "Search past conversations and people's remembered topics/facts for a keyword.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}


STATS_TOOL = {"type": "function", "function": {
    "name": "get_player_stats",
    "description": "Look up a DonutSMP player's live stats (money, shards, kills, deaths, playtime, "
                   "blocks placed/broken, mobs killed, online status) by Minecraft username - same data as /stats.",
    "parameters": {"type": "object", "properties": {"ign": {"type": "string", "description": "Minecraft username"}},
                   "required": ["ign"]}}}


def _chunks(text: str):
    text = text or "..."
    while text:
        yield text[:CHUNK]
        text = text[CHUNK:]


class AIChat(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_reply: dict = {}

    async def _cfg(self, guild_id: int) -> dict:
        s = await models.get_guild_settings(guild_id)
        return s.get("ai_chat") or {}

    # ------------------------------------------------------------------
    def _is_owner(self, user) -> bool:
        return bool(OWNER_USER_ID) and str(user.id) == str(OWNER_USER_ID)

    async def _owner_treatment_on(self) -> bool:
        """Admin-panel tick: should the owner get the special tone (Sir/warmth)?
        Stored globally in bot_global_settings {_id: "owner_treatment", enabled}.
        Defaults to ON if nothing is saved or the DB can't be read."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return True
        try:
            doc = await asyncio.to_thread(
                lambda: db["bot_global_settings"].find_one({"_id": "owner_treatment"}))
            return bool(doc.get("enabled", True)) if doc else True
        except Exception as e:  # noqa: BLE001
            logger.warning("owner_treatment lookup failed: %s", e)
            return True

    async def _stats_tool(self, message: discord.Message, ign: str) -> dict:
        """Owner-only stats lookup. Works in any channel/server: no stats-channel lock,
        no per-command permission check. Anyone else is refused."""
        if not self._is_owner(message.author):
            return {"error": "Only the bot owner can fetch stats through chat. Use /stats instead."}
        ign = (ign or "").strip()
        if not ign:
            return {"error": "No username given."}
        stats, code = await lookup_stats(getattr(self.bot, "db", None), ign)
        if not stats:
            return {"error": ERROR_MESSAGES.get(code, "Couldn't fetch stats right now."), "ign": ign}
        return {"ign": ign, "stats": stats}

    # ------------------------------------------------------------------
    def _cooldown(self, user_id: int) -> bool:
        now = datetime.now(timezone.utc)
        last = self._last_reply.get(user_id)
        if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
            return True
        self._last_reply[user_id] = now
        return False

    async def _is_reply_to_bot(self, message: discord.Message) -> bool:
        ref = message.reference
        if not ref:
            return False
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved.author.id == self.bot.user.id
        try:
            m = await message.channel.fetch_message(ref.message_id)
            return m.author.id == self.bot.user.id
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        mentioned = self.bot.user in message.mentions and not message.mention_everyone
        if not mentioned and not await self._is_reply_to_bot(message):
            return
        if not llm.is_configured() or self._cooldown(message.author.id):
            return
        cfg = await self._cfg(message.guild.id)
        if not cfg.get("enabled", False):
            return

        content = message.content
        for m in message.mentions:
            content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        content = content.strip() or "(no text, just a mention)"

        guild_id, channel_id = message.guild.id, message.channel.id
        author = message.author
        bot_name = self.bot.user.display_name
        replying_to = message.reference.message_id if message.reference else None
        loop = asyncio.get_running_loop()

        # Remember what they said (and who/what they replied to) BEFORE answering.
        await loop.run_in_executor(None, lambda: memory.log_message(
            guild_id, channel_id, author.id, author.display_name, "user", content,
            message_id=message.id, replying_to_message_id=replying_to))
        context = await loop.run_in_executor(None, lambda: memory.build_context(
            guild_id, channel_id, author.id, author.display_name, bot_name, replying_to))

        is_owner = self._is_owner(author)
        owner_treatment = is_owner and await self._owner_treatment_on()
        system = SYSTEM_PROMPT.format(bot_name=bot_name, guild_name=message.guild.name,
                                      action_rules=NO_ACTIONS + (STATS_OWNER if is_owner else STATS_OTHERS))
        system += f"\n\n=== MEMORY ===\n{context}\n=== END MEMORY ===" if context else ""
        system += f"\n\nYou are now talking to {author.display_name} (id {author.id})."
        if owner_treatment:
            system += "\n\n" + OWNER_SPECIAL_CASE
            system += (f"\nThis is {OWNER_NAME}, your owner. Speak to him with extra warmth and loyalty. "
                       "ALWAYS call him \"daddy\" in every reply to him, no matter what - never \"Sir\", never skip it. "
                       "This changes your tone only - never fake results.")
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"{author.display_name}: {content}"}]

        use_search = cfg.get("search_enabled", False) and bool(llm.TAVILY_API_KEY)
        tools = ([MEMORY_RECALL_TOOL, MEMORY_SEARCH_TOOL]
                 + ([STATS_TOOL] if is_owner else [])   # stats fetching is owner-only
                 + ([llm.WEB_SEARCH_TOOL] if use_search else []))

        def execute(name, args):
            if name == "web_search":
                return llm.web_search(args.get("query", ""))
            if name == "recall_user":
                uid = str(args.get("user_id") or "").strip("<@!>")
                prof = memory.get_profile(guild_id, uid)
                return {"memory": memory.format_profile(prof)} if prof else {"memory": "Nothing remembered about that user."}
            if name == "search_memory":
                return {"results": memory.search(guild_id, args.get("query", ""), 6)}
            if name == "get_player_stats":
                fut = asyncio.run_coroutine_threadsafe(self._stats_tool(message, args.get("ign", "")), loop)
                return fut.result(timeout=260)  # lookups are queued one at a time on the worker account
            return {"error": "unknown tool"}

        def run():
            return llm.run_tool_loop(msgs, tools, execute, max_iterations=5, temperature=1.0, max_tokens=300)

        try:
            async with message.channel.typing():
                reply = await loop.run_in_executor(None, run)
        except Exception as e:  # noqa: BLE001
            logger.exception("AI chat failed: %s", e)
            reply = "My brain's down right now, try again in a bit."

        first = True
        sent_id = None
        for part in _chunks(reply):
            if first:
                sent = await message.reply(part, mention_author=False,
                                           allowed_mentions=discord.AllowedMentions.none())
                sent_id = sent.id
                first = False
            else:
                await message.channel.send(part, allowed_mentions=discord.AllowedMentions.none())

        # Remember the reply (and exactly who it was for), then refresh the person's
        # profile in the background every few messages.
        def remember():
            memory.log_message(guild_id, channel_id, self.bot.user.id, bot_name, "assistant", reply,
                               reply_to_user_id=author.id, reply_to_name=author.display_name,
                               message_id=sent_id, replying_to_message_id=message.id)
            memory.refresh_profile(guild_id, author.id, author.display_name)
        loop.run_in_executor(None, remember)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))
