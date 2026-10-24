"""Stdlib-only setup for the Minecraft bot (portable Node + npm deps).
Runs at the very top of main.py, before discord/flask are imported, so the
npm install has the container's RAM to itself (a 256 MB host OOM-kills it
otherwise: exit -9)."""
import os
import pathlib
import shutil
import subprocess

BASE = pathlib.Path(__file__).parent
NPM_CACHE = str(BASE / ".npm-cache")
FAIL_FLAG = BASE / ".local" / "prune_failed"
PRUNE_RULE = BASE / ".local" / "prune_rule"
PRUNE_RULE_VERSION = "v2"  # bump when the keep-rules change -> forces a clean reinstall


def _npm_env():
    env = dict(os.environ)
    env["npm_config_cache"] = NPM_CACHE
    # keep node's heap small so npm stays inside a 256 MB container
    env["NODE_OPTIONS"] = "--max-old-space-size=140"
    return env

def ensure_local_node():
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


def _dir_size_mb(path) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total / 1048576


def report_disk_usage():
    """Print where the disk space goes (host limit is small)."""
    base = pathlib.Path(__file__).parent
    items = {
        ".local/node (portable Node)": base / ".local" / "node",
        ".local/lib (python packages)": base / ".local" / "lib",
        "mc-bot/node_modules": base / "mc-bot" / "node_modules",
        "mc-bot/cache": base / "mc-bot" / "cache",
        ".npm / .cache": None,
    }
    parts = []
    for label, path in items.items():
        if path is None:
            size = sum(_dir_size_mb(base / d) for d in (".npm", ".cache") if (base / d).exists())
        else:
            size = _dir_size_mb(path) if path.exists() else 0
        parts.append(f"{label}: {size:.0f} MB")
    print("💾 Disk usage -> " + " | ".join(parts) + f" | whole folder: {_dir_size_mb(base):.0f} MB")
    nm = base / "mc-bot" / "node_modules"
    if nm.is_dir():
        sizes = sorted(((_dir_size_mb(p), p.name) for p in nm.iterdir() if p.is_dir()), reverse=True)[:5]
        print("📊 Biggest node_modules -> " + " | ".join(f"{n}: {sz:.0f} MB" for sz, n in sizes))


def _mark_pruned():
    try:
        PRUNE_RULE.parent.mkdir(parents=True, exist_ok=True)
        PRUNE_RULE.write_text(PRUNE_RULE_VERSION)
    except OSError:
        pass


def prune_minecraft_data(mc_bot_dir: pathlib.Path):
    """minecraft-data is ~300+ MB because it ships data for Bedrock and every
    Java version ever. Newer Java versions REUSE files from older version
    folders (see data/dataPaths.json), so we only delete folders that no kept
    version points to. Keeps Java 1.<MC_KEEP_MIN_MINOR>+ (default 20) and every
    'common' folder, then loads every kept version to verify."""
    import json
    import re
    nm = mc_bot_dir / "node_modules"
    data = nm / "minecraft-data" / "minecraft-data" / "data"
    if os.getenv("MC_PRUNE_DATA") == "0" or not data.is_dir():
        return True
    try:
        keep_min = int(os.getenv("MC_KEEP_MIN_MINOR", "20"))
    except ValueError:
        keep_min = 20

    try:
        paths = json.loads((data / "dataPaths.json").read_text())["pc"]
    except Exception as e:
        print(f"ℹ️  Cannot read minecraft-data/dataPaths.json ({e}) — skipping prune.")
        return True

    def _is_recent(v):
        # "1.21.4"-style names need minor >= keep_min; the new "26.1"-style
        # names (Minecraft's 2026 versioning) are always recent.
        m = re.match(r"^(\d+)\.(\d+)", v)
        if not m:
            return False
        major, minor = int(m.group(1)), int(m.group(2))
        return minor >= keep_min if major == 1 else major > 1

    kept = [v for v in paths if _is_recent(v)]
    if not kept:
        print("ℹ️  No recent Java versions found in minecraft-data — skipping prune.")
        return True

    # every folder that a kept version reads from (own folder + shared ones)
    needed = {"common"}
    for v in kept:
        for p in (paths[v] or {}).values():
            if isinstance(p, str):
                parts = p.strip("/").split("/")
                if len(parts) >= 2 and parts[0] == "pc":
                    needed.add(parts[1])
                elif len(parts) == 1:
                    needed.add(parts[0])
    needed.update(kept)

    doomed = []
    pc = data / "pc"
    if pc.is_dir():
        doomed += [d for d in pc.iterdir() if d.is_dir() and d.name not in needed]
    bedrock = data / "bedrock"
    if bedrock.is_dir():
        doomed += [d for d in bedrock.iterdir() if d.is_dir() and d.name != "common"]
    if not doomed:
        _mark_pruned()
        return True

    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)

    script = ("require('mineflayer');const m=require('minecraft-data');"
              "for(const v of process.argv.slice(1)){const d=m(v);"
              "if(!d||!d.blocksByName||!d.windows&&d.windows!==undefined){process.exit(3)}}")
    try:
        r = subprocess.run([shutil.which("node") or "node", "-e", script, *kept],
                           cwd=str(mc_bot_dir), capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0
        err = (r.stderr or "").strip()
    except Exception as e:
        ok, err = False, str(e)
    if ok:
        print(f"✂️  Pruned minecraft-data: removed {len(doomed)} folders, kept {len(kept)} Java versions (1.{keep_min}+ and 26+).")
        _mark_pruned()
        return True

    err = err[:700] + "\n...\n" + err[-700:] if len(err) > 1500 else err
    print(f"❌ Pruning broke mineflayer. Error was:\n{err}")
    shutil.rmtree(nm, ignore_errors=True)
    try:
        FAIL_FLAG.parent.mkdir(parents=True, exist_ok=True)
        FAIL_FLAG.write_text(err)
    except OSError:
        pass
    return False


def ensure_mc_bot_deps(mc_bot_dir: pathlib.Path):
    """Make sure mc-bot/node_modules has what index.js needs (currently
    just 'mongodb', for schedule/session persistence). Runs `npm install`
    in that folder if it's missing, so a fresh deploy or a new host
    doesn't require manually remembering to install it."""
    required = ["mongodb", "mineflayer", "express"]
    nm = mc_bot_dir / "node_modules"
    if FAIL_FLAG.exists() and os.getenv("MC_PRUNE_DATA") != "0":
        print("⛔ MC bot disabled: trimming minecraft-data failed last time (see error above). "
              "Send that error to your developer, then delete .local/prune_failed to retry.")
        return False
    # node_modules trimmed under older rules (e.g. missing 26.x data) -> start clean.
    if nm.exists() and os.getenv("MC_PRUNE_DATA") != "0":
        try:
            current = PRUNE_RULE.read_text().strip() if PRUNE_RULE.exists() else ""
        except OSError:
            current = ""
        if current != PRUNE_RULE_VERSION:
            print("♻️  Old trimmed mc-bot/node_modules detected — deleting it for a clean reinstall...")
            shutil.rmtree(nm, ignore_errors=True)
    missing = [pkg for pkg in required if not (nm / pkg).exists()]
    # A half-written install (e.g. disk quota hit mid-install) leaves folders
    # behind but with data files missing -> detect it and start clean.
    marker = nm / "minecraft-data" / "minecraft-data" / "data" / "pc" / "common" / "features.json"
    if (nm / "minecraft-data").exists() and not marker.exists():
        print("⚠️  Broken minecraft-data install detected — reinstalling mc-bot dependencies cleanly...")
        shutil.rmtree(nm, ignore_errors=True)
        missing = list(required)
    if not missing:
        return prune_minecraft_data(mc_bot_dir) and (nm / "mineflayer").exists()

    npm_path = shutil.which("npm")
    if not npm_path:
        print(f"⚠️  'npm' not found in PATH — can't auto-install {missing} in mc-bot/. "
              f"Persistence features will be disabled until it's installed manually.")
        return False

    print(f"📦 Installing missing mc-bot dependencies ({', '.join(missing)})...")
    try:
        result = subprocess.run(
            [npm_path, "install", "--omit=dev", "--omit=optional", "--no-audit",
             "--no-fund", "--no-progress", "--no-package-lock", "--maxsockets=2", "mongodb"],
            cwd=str(mc_bot_dir),
            env=_npm_env(),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        shutil.rmtree(NPM_CACHE, ignore_errors=True)
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
            return prune_minecraft_data(mc_bot_dir) and (nm / "mineflayer").exists()
        else:
            print(f"❌ npm install failed (exit {result.returncode}):\n{result.stderr.strip()[-2000:]}")
    except Exception as e:
        print(f"❌ Failed to run npm install in mc-bot/: {e}")
        shutil.rmtree(nm, ignore_errors=True)  # don't leave a half-written install behind
    return False



def bootstrap():
    """Make sure Node and mc-bot/node_modules exist. Safe to call every start."""
    if os.getenv("MC_SETUP_DONE") == "1":
        return
    os.environ["MC_SETUP_DONE"] = "1"
    mc_bot_dir = BASE / "mc-bot"
    if not (mc_bot_dir / "index.js").exists():
        return
    try:
        ensure_local_node()
        if shutil.which("node"):
            ensure_mc_bot_deps(mc_bot_dir)
            report_disk_usage()
    except Exception as e:
        print(f"⚠️  MC setup error: {e}")
