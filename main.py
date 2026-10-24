import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import subprocess, sys, pathlib
import shutil
import threading
import time
from dotenv import load_dotenv
from db import get_bot_token, get_db
from cogs.command_perms_core import (
    get_all_command_configs,
    get_command_prefix,
    TextBridgeInteraction,
    run_app_command_checks,
)

from app import app as flask_app
import app as app_module

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

COGS = [
    'cogs.stats',
    'cogs.pay',
    'cogs.ai_chat',
    'cogs.builds',
]


DEFAULT_PREFIX = '!'


async def dynamic_prefix(bot: "Bot", message: discord.Message):
    """Resolves the set of valid prefixes for this message.

    discord.py only supports one prefix *per message* (it doesn't know
    which command is coming until after the prefix is stripped), so a true
    per-command prefix has to work in two stages:

      1. Here: accept every prefix that any command in this guild has been
         given on the dashboard, plus the bot's default '!'. This makes
         discord.py successfully parse the message and resolve a command
         object no matter which of those prefixes was used.
      2. In `_enforce_command_prefix` below (a global check): once we know
         *which* command was actually resolved, reject the invocation if
         the prefix used in the message doesn't match that specific
         command's configured prefix (or the default, if none is set).

    Without step 2, setting a custom prefix on one command would
    accidentally let every command respond to that prefix too.
    """
    default = commands.when_mentioned_or(DEFAULT_PREFIX)(bot, message)
    db = getattr(bot, "db", None)
    if db is None or message.guild is None:
        return default

    try:
        configs = get_all_command_configs(db, message.guild.id)
    except Exception:
        configs = {}

    prefixes = set(default)
    for cfg in configs.values():
        custom = cfg.get("prefix")
        if custom:
            prefixes.add(custom)
    return list(prefixes)


def _ensure_local_node():
    """Make 'node'/'npm' available on hosts that only ship Python.
    Downloads a portable Node 22 into ./.local/node once, then adds it to PATH."""
    import platform, tarfile, urllib.request, re
    base = pathlib.Path(__file__).parent / ".local" / "node"
    bindir = base / "bin"
    if not (bindir / "node").exists() and not shutil.which("node"):
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            print(f"❌ Unsupported CPU for portable Node: {platform.machine()}")
            return
        try:
            root = "https://nodejs.org/dist/latest-v22.x/"
            sums = urllib.request.urlopen(root + "SHASUMS256.txt", timeout=30).read().decode()
            m = re.search(rf"(node-v[\d.]+-linux-{arch}\.tar\.xz)", sums)
            if not m:
                print("❌ Could not find a Node.js download for this platform.")
                return
            fname = m.group(1)
            print(f"⬇️  Downloading portable Node.js ({fname})...")
            base.mkdir(parents=True, exist_ok=True)
            tmp = base.parent / fname
            urllib.request.urlretrieve(root + fname, tmp)
            with tarfile.open(tmp, "r:xz") as tf:
                for member in tf.getmembers():
                    parts = member.name.split("/", 1)
                    if len(parts) < 2 or not parts[1]:
                        continue
                    # Skip stuff we never need (saves ~60MB of disk)
                    if parts[1].split("/")[0] in ("include", "share") or \
                       parts[1].startswith(("lib/node_modules/corepack", "CHANGELOG", "README", "LICENSE")):
                        continue
                    member.name = parts[1]
                    tf.extract(member, base)
            tmp.unlink(missing_ok=True)
            print("✅ Portable Node.js installed.")
        except Exception as e:
            print(f"❌ Failed to install portable Node.js: {e}")
            return
    if bindir.is_dir():
        os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


class Bot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Store the MC bot subprocess so we can monitor/restart it
        self.mc_proc = None
        self._mc_node_path = None
        self._mc_bot_path = None
        self._mc_env = None
        self._mc_shutting_down = False

        # ── Text-command bridge registry ──────────────────────────────
        # Maps command_name ("mute", "build claim", ...) -> {
        #     "cog": <cog instance>, "app_command": <app_commands.Command>,
        #     "parser": callable(ctx, remainder_str) -> list[args] (may be async)
        # }
        # Populated by cogs via bot.register_text_bridge(...) in their
        # cog_load()/__init__, so a slash command like /mute can also be
        # invoked as a text command (e.g. "!mute" or a custom per-command
        # prefix set on the dashboard), without duplicating its logic.
        self.text_bridges = {}

    def register_text_bridge(self, command_name: str, cog, app_command, parser):
        """Registers a slash command so it can also be invoked as a text
        command. `parser(ctx, remainder)` must return a list of positional
        args matching app_command's parameters (in order, already
        converted to the right types — e.g. a discord.Member, not a raw
        mention string). It may be a regular function or a coroutine.
        Raise ValueError from the parser to send a usage message back."""
        self.text_bridges[command_name.lower()] = {
            "cog": cog,
            "app_command": app_command,
            "parser": parser,
        }

    async def _resolve_text_bridge(self, message: discord.Message):
        """Checks message.content against every registered text bridge,
        honoring each command's own configured prefix (default '!' unless
        overridden on the dashboard). Returns (spec, command_name,
        remainder) on a match, or None."""
        if message.guild is None or not self.text_bridges:
            return None

        content = message.content
        db = getattr(self, "db", None)

        # Longest command name first, so "build claim" is tried before "build".
        for command_name in sorted(self.text_bridges, key=len, reverse=True):
            spec = self.text_bridges[command_name]
            prefix = await asyncio.to_thread(
                get_command_prefix, db, message.guild.id, command_name, default_prefix=DEFAULT_PREFIX
            )
            lead = prefix + command_name
            if content[:len(lead)].lower() != lead.lower():
                continue
            rest = content[len(lead):]
            if rest and not rest[0].isspace():
                # e.g. "!mutey" shouldn't match the "mute" bridge
                continue
            return spec, command_name, rest.strip()
        return None

    async def _invoke_text_bridge(self, message: discord.Message):
        match = await self._resolve_text_bridge(message)
        if match is None:
            return False
        spec, command_name, remainder = match

        ctx = await self.get_context(message)
        interaction = TextBridgeInteraction(ctx, app_command=spec["app_command"])

        if not await run_app_command_checks(interaction, spec["app_command"]):
            return True  # matched a bridge; the check already replied (or silently refused)

        try:
            result = spec["parser"](ctx, remainder)
            if hasattr(result, "__await__"):
                result = await result
            args = result or []
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return True

        try:
            await spec["app_command"].callback(spec["cog"], interaction, *args)
        except Exception as e:
            import traceback
            traceback.print_exception(type(e), e, e.__traceback__)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ An unexpected error occurred while running this command.", ephemeral=True)
            except discord.HTTPException:
                pass
        return True

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Native text commands first (e.g. "!stats", "!sync") — these go
        # through the normal discord.py command framework and the
        # _enforce_command_prefix check below.
        ctx = await self.get_context(message)
        if ctx.valid and ctx.command is not None:
            await self.invoke(ctx)
            return

        # Otherwise, see if this matches a bridged slash command.
        if await self._invoke_text_bridge(message):
            return

    def _spawn_mc_bot(self):
        """Launch the Node MC-bot subprocess. Returns the Popen handle, or
        None if launch failed. Used both at startup and by the crash-restart
        monitor, so both paths behave identically."""
        try:
            proc = subprocess.Popen(
                [self._mc_node_path, str(self._mc_bot_path)],
                env=self._mc_env,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            print(f"✅ MC bot subprocess started (PID {proc.pid})")
            return proc
        except Exception as e:
            print(f"❌ Failed to start MC bot subprocess: {e}")
            return None

    def _ensure_mc_bot_deps(self, mc_bot_dir: pathlib.Path):
        """Make sure mc-bot/node_modules has what index.js needs (currently
        just 'mongodb', for schedule/session persistence). Runs `npm install`
        in that folder if it's missing, so a fresh deploy or a new host
        doesn't require manually remembering to install it."""
        required = ["mongodb", "mineflayer", "express"]
        nm = mc_bot_dir / "node_modules"
        missing = [pkg for pkg in required if not (nm / pkg).exists()]
        # A half-written install (e.g. disk quota hit mid-install) leaves folders
        # behind but with data files missing -> detect it and start clean.
        marker = nm / "minecraft-data" / "minecraft-data" / "data" / "pc" / "common" / "features.json"
        if (nm / "minecraft-data").exists() and not marker.exists():
            print("⚠️  Broken minecraft-data install detected — reinstalling mc-bot dependencies cleanly...")
            shutil.rmtree(nm, ignore_errors=True)
            missing = list(required)
        if not missing:
            return True

        npm_path = shutil.which("npm")
        if not npm_path:
            print(f"⚠️  'npm' not found in PATH — can't auto-install {missing} in mc-bot/. "
                  f"Persistence features will be disabled until it's installed manually.")
            return False

        print(f"📦 Installing missing mc-bot dependencies ({', '.join(missing)})...")
        try:
            result = subprocess.run(
                [npm_path, "install", "--omit=dev", "--omit=optional", "--no-audit",
                 "--no-fund", "--no-package-lock", "mongodb"],
                cwd=str(mc_bot_dir),
                env={**os.environ, "npm_config_cache": "/tmp/npm-cache"},
                capture_output=True,
                text=True,
                timeout=1800,
            )
            shutil.rmtree("/tmp/npm-cache", ignore_errors=True)
            # Trim junk (docs, sourcemaps, tests) to save disk on small hosts
            try:
                for root, dirs, files in os.walk(mc_bot_dir / "node_modules"):
                    for d in list(dirs):
                        if d in (".github", "docs", "test", "tests", "__tests__", "example", "examples"):
                            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                            dirs.remove(d)
                    for f in files:
                        if f.endswith((".md", ".map", ".markdown")):
                            try:
                                os.remove(os.path.join(root, f))
                            except OSError:
                                pass
            except Exception as e:
                print(f"⚠️  node_modules cleanup skipped: {e}")
            if result.returncode == 0:
                print(f"✅ Installed {', '.join(missing)} in mc-bot/.")
                return True
            else:
                print(f"❌ npm install failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}")
        except Exception as e:
            print(f"❌ Failed to run npm install in mc-bot/: {e}")
            shutil.rmtree(nm, ignore_errors=True)  # don't leave a half-written install behind
        return False

    async def setup_hook(self):
        app_module.set_discord_bot(self)

        # --- MongoDB Setup ---
        self.db = get_db()
        if self.db is None:
            print("❌ MONGO_URI environment variable is not set!")
        else:
            try:
                self.db.client.admin.command('ping')
                print("✅ Successfully connected to MongoDB!")
            except Exception as e:
                print(f"❌ Failed to connect to MongoDB: {e}")

        self.tree.on_error = self.on_tree_error

        # ── Spawn MC bot subprocess ──────────────────────────────────────
        mc_bot_path = pathlib.Path(__file__).parent / "mc-bot" / "index.js"
        await asyncio.to_thread(_ensure_local_node)
        node_path = shutil.which("node")

        if not node_path:
            print("⚠️  'node' not found in PATH — MC bot not started. Check nixpacks.toml.")
        elif not mc_bot_path.exists():
            print("⚠️  mc-bot/index.js not found — MC bot not started.")
        else:
            def _launch_mc_bot():
                if not self._ensure_mc_bot_deps(mc_bot_path.parent):
                    print("❌ mc-bot dependencies are not installed — MC bot not started.")
                    return
                mc_env = os.environ.copy()
                mc_env["MC_BOT_PORT"] = "3001"
                mc_env["FLASK_INTERNAL_URL"] = f"http://127.0.0.1:{(os.getenv('SERVER_PORT') or os.getenv('PORT') or 5000)}"

                self._mc_node_path = node_path
                self._mc_bot_path = mc_bot_path
                self._mc_env = mc_env

                self.mc_proc = self._spawn_mc_bot()

                # Background monitor: if the MC bot subprocess ever dies (crash,
                # unhandled exception, OOM, etc.), respawn it automatically with
                # exponential backoff instead of just logging and leaving every
                # linked Minecraft session dead until someone redeploys.
                def monitor_mc_bot():
                    backoff = 2
                    while not self._mc_shutting_down:
                        proc = self.mc_proc
                        if proc is None:
                            return
                        started_at = time.monotonic()
                        ret = proc.wait()
                        if self._mc_shutting_down:
                            print("ℹ️  MC bot subprocess stopped (shutting down).")
                            return

                        ran_for = time.monotonic() - started_at
                        # A crash-loop shouldn't restart every 2s forever; but a
                        # process that ran fine for a while before dying should
                        # reset the backoff instead of inheriting a long delay.
                        if ran_for > 60:
                            backoff = 2

                        print(f"⚠️  MC bot subprocess exited with code {ret} after {ran_for:.0f}s. "
                              f"Restarting in {backoff}s...")
                        time.sleep(backoff)

                        self.mc_proc = self._spawn_mc_bot()
                        if self.mc_proc is None:
                            print("❌ Failed to restart MC bot subprocess.")
                            return

                        backoff = min(backoff * 2, 60)

                threading.Thread(target=monitor_mc_bot, daemon=True).start()

            # Install deps + start the MC bot in the background so Discord and the
            # dashboard come up immediately (the first npm install can take minutes).
            threading.Thread(target=_launch_mc_bot, daemon=True).start()

        for cog in COGS:
            try:
                await self.load_extension(cog)
                print(f'✅ {cog} loaded!')
            except Exception as e:
                print(f'❌ Failed to load {cog}: {e}')

        # --- Start Web Dashboard in Background ---
        def run_web():
            port = int(os.getenv("SERVER_PORT") or os.getenv("PORT") or 5000)
            print(f"🌐 Starting Flask Dashboard on 0.0.0.0:{port}...")
            try:
                flask_app.run(
                    host='0.0.0.0',
                    port=port,
                    debug=False,
                    use_reloader=False
                )
            except Exception as e:
                print(f"❌ Flask failed to start: {e}")

        threading.Thread(target=run_web, daemon=True).start()

    async def on_tree_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error)
        else:
            import traceback
            traceback.print_exception(
                type(error),
                error,
                error.__traceback__
            )
            msg = "❌ An unexpected error occurred while running this command."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


bot = Bot(command_prefix=dynamic_prefix, intents=intents, help_command=None)


@bot.check
async def _enforce_command_prefix(ctx: commands.Context) -> bool:
    """Global check (runs for every prefix command): rejects the
    invocation if the prefix actually typed doesn't match this specific
    command's configured prefix. Paired with dynamic_prefix() above — see
    its docstring for why this two-step approach is needed.

    Mention-prefix invocations ("@Bot stats") are always accepted; being
    explicitly addressed by mention is unambiguous regardless of any
    per-command prefix override.
    """
    if ctx.guild is None or ctx.command is None:
        return True
    if ctx.prefix and ctx.prefix.startswith("<@"):
        return True

    db = getattr(ctx.bot, "db", None)
    expected = await asyncio.to_thread(
        get_command_prefix, db, ctx.guild.id, ctx.command.qualified_name, default_prefix=DEFAULT_PREFIX
    )
    return ctx.prefix == expected


@bot.event
async def on_ready():
    print(f'✅ Bot online: {bot.user} (ID: {bot.user.id})')
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} global command(s): {[f"/{c.name}" for c in synced]}')
    except Exception as e:
        print(f'❌ Global sync failed: {e}')


@bot.command(name='sync')
@commands.has_permissions(administrator=True)
async def sync_commands(ctx):
    msg = await ctx.send(embed=discord.Embed(
        title="🔄 Syncing commands",
        description="Wiping old commands, reloading cogs and syncing with Discord…",
        color=0xFEE75C))
    reloaded, failed = [], []
    try:
        bot.tree.clear_commands(guild=None)
        bot.tree.clear_commands(guild=ctx.guild)
        for cog in COGS:
            try:
                await bot.reload_extension(cog)
                reloaded.append(cog)
                print(f'✅ Reloaded {cog}')
            except Exception as e:
                failed.append((cog, e))
                print(f'❌ Reload failed for {cog}: {e}')
        synced = await bot.tree.sync()
    except Exception as e:
        err = discord.Embed(title="❌ Sync failed", color=0xED4245,
                            description=f"```{str(e)[:3800]}```")
        if reloaded:
            err.add_field(name=f"Reloaded ({len(reloaded)})",
                          value=", ".join(f"`{c.split('.')[-1]}`" for c in reloaded)[:1024], inline=False)
        if failed:
            err.add_field(name=f"Reload failed ({len(failed)})",
                          value="\n".join(f"`{c.split('.')[-1]}`: {str(x)[:100]}" for c, x in failed)[:1024], inline=False)
        return await msg.edit(embed=err)
    names = ", ".join(f"`/{c.name}`" for c in synced) or "none"
    done = discord.Embed(
        title="✅ Commands synced", color=0x57F287,
        description=f"Synced **{len(synced)}** global command{'s' if len(synced) != 1 else ''}.")
    done.add_field(name="Commands", value=names[:1024], inline=False)
    done.add_field(name=f"Reloaded cogs ({len(reloaded)})",
                   value=(", ".join(f"`{c.split('.')[-1]}`" for c in reloaded) or "none")[:1024], inline=False)
    if failed:
        done.color = 0xFEE75C
        done.title = "⚠️ Commands synced (some cogs failed)"
        done.add_field(name=f"Reload failed ({len(failed)})",
                       value="\n".join(f"`{c.split('.')[-1]}`: {str(x)[:100]}" for c, x in failed)[:1024], inline=False)
    await msg.edit(embed=done)


@bot.command(name='reload')
@commands.has_permissions(administrator=True)
async def reload_cog(ctx, cog: str = ''):
    targets = COGS if not cog else [cog]
    results = []
    for c in targets:
        try:
            await bot.reload_extension(c)
            results.append(f'✅ {c}')
        except Exception as e:
            results.append(f'❌ {c}: {e}')
    await ctx.send('\n'.join(results))


@bot.command(name='listcogs')
@commands.has_permissions(administrator=True)
async def list_cogs(ctx):
    loaded = list(bot.extensions.keys())
    await ctx.send(f"Loaded cogs: {', '.join(loaded) if loaded else 'None'}")


def main():
    token = get_bot_token()
    if not token:
        raise RuntimeError('DISCORD_BOT_TOKEN or DISCORD_TOKEN environment variable is not set.')

    async def run_bot():
        async with bot:
            await bot.start(token)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("Bot stopped by user")
    finally:
        # Tell the monitor thread this is a deliberate shutdown (not a
        # crash) so it doesn't try to respawn the MC bot subprocess, then
        # actually stop it so it doesn't linger as an orphan process.
        bot._mc_shutting_down = True
        if bot.mc_proc is not None:
            try:
                bot.mc_proc.terminate()
            except Exception:
                pass


if __name__ == '__main__':
    main()
