"""Build upstream Chromium Stable and install verified portable builds on Windows.

Uses the Python standard library. Build and deployment state stay outside git.
Never downloads executable updates from an untrusted third-party build feed.
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parent
RELEASE_URL = ("https://chromiumdash.appspot.com/fetch_releases?"
               "channel=Stable&platform=Windows&num=100")
VERSION_HISTORY_URL = ("https://versionhistory.googleapis.com/v1/chrome/platforms/win64/"
                       "channels/stable/versions/all/releases?filter=endtime%3Dnone")
SOURCE_URL = "https://chromium.googlesource.com/chromium/src.git"
DEPOT_URL = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LOG = logging.getLogger("chromo-bro")
HELPER_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class OperationInProgress(RuntimeError):
    """A scheduled or manual operation is already doing this work."""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def version_tuple(value):
    if not VERSION_RE.fullmatch(value):
        raise ValueError("Invalid Chromium version: " + str(value))
    return tuple(int(part) for part in value.split("."))


def select_stable_version(releases):
    # Early-Stable experiments also have channel=Stable. Google's pinnable
    # releases identify versions eligible for the regular supported release path.
    eligible = []
    for release in releases:
        if release.get("pinnable") is True and float(release.get("fraction", 0)) > 0:
            version_tuple(release["version"])
            eligible.append(release["version"])
    if not eligible:
        raise RuntimeError("Google's VersionHistory feed has no eligible Windows x64 Stable release")
    return max(eligible, key=version_tuple)


def select_release(releases, desired_version):
    version_tuple(desired_version)
    candidates = []
    for release in releases:
        if release.get("channel") != "Stable" or release.get("platform") != "Windows":
            continue
        if release.get("version") != desired_version:
            continue
        version_tuple(release["version"])
        if not SHA_RE.fullmatch(release.get("hashes", {}).get("chromium", "")):
            raise ValueError("Release feed did not provide a valid Chromium commit")
        candidates.append(release)
    if not candidates:
        raise RuntimeError("No Chromium source commit found for Stable " + desired_version)
    return candidates[0]


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "chromo-bro/1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def latest_release():
    history = fetch_json(VERSION_HISTORY_URL)
    releases = history.get("releases", [])
    for _ in range(10):
        token = history.get("nextPageToken")
        if not token:
            break
        history = fetch_json(VERSION_HISTORY_URL + "&pageToken=" + urllib.parse.quote(token, safe=""))
        releases.extend(history.get("releases", []))
    else:
        raise RuntimeError("Unexpectedly large active-release feed")
    desired_version = select_stable_version(releases)
    return select_release(fetch_json(RELEASE_URL), desired_version)


def load_config(path):
    config = read_json(path)
    if config is None:
        raise RuntimeError("Missing local configuration: " + str(path))
    for key in ("build_root", "install_root", "visual_studio", "seven_zip"):
        config[key] = Path(config[key]).resolve()
    if config["build_root"] == config["install_root"]:
        raise ValueError("Build and browser directories must be separate")
    config.setdefault("jobs", 3)
    config.setdefault("sync_jobs", 2)
    config.setdefault("minimum_free_gb", 100)
    for key in ("jobs", "sync_jobs"):
        if not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(key + " must be a positive integer")
    return config


@contextlib.contextmanager
def exclusive_lock(path):
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise OperationInProgress("Another operation holds " + str(path)) from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run(args, cwd=None, env=None, capture=False, timeout=None):
    args = [str(arg) for arg in args]
    LOG.info("Running %s", subprocess.list2cmdline(args))
    if os.name == "nt" and Path(args[0]).suffix.lower() in (".bat", ".cmd"):
        args = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c"] + args
    if capture:
        return subprocess.check_output(args, cwd=cwd, env=env, text=True,
                                       encoding="utf-8", errors="replace", timeout=timeout,
                                       creationflags=HELPER_FLAGS).strip()
    with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace", creationflags=HELPER_FLAGS) as process:
        for line in process.stdout:
            LOG.info(line.rstrip())
        code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, args)


def build_environment(config):
    env = os.environ.copy()
    root = config["build_root"]
    depot = root / "depot_tools"
    git = str(config["git"]) if config.get("git") else shutil.which("git")
    if not git:
        raise RuntimeError("Git for Windows must be installed and on PATH")
    for name in ("temp", "cache/cipd", "cache/vpython"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env["PATH"] = str(depot) + os.pathsep + str(Path(git).parent) + os.pathsep + env["PATH"]
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["vs2026_install"] = str(config["visual_studio"])
    env["GYP_MSVS_OVERRIDE_PATH"] = str(config["visual_studio"])
    env["TEMP"] = env["TMP"] = str(root / "temp")
    env["CIPD_CACHE_DIR"] = str(root / "cache/cipd")
    env["VPYTHON_VIRTUALENV_ROOT"] = str(root / "cache/vpython")
    # Process-local Git settings; do not change the user's global configuration.
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate((("core.longpaths", "true"),
                                          ("core.autocrlf", "false"))):
        env["GIT_CONFIG_KEY_" + str(count + offset)] = key
        env["GIT_CONFIG_VALUE_" + str(count + offset)] = value
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@contextlib.contextmanager
def stay_awake():
    """Prevent automatic system sleep only while this build process is active."""
    import ctypes
    if os.name != "nt":
        yield
        return
    kernel = ctypes.windll.kernel32
    requested = kernel.SetThreadExecutionState(0x80000001)  # CONTINUOUS | SYSTEM_REQUIRED
    try:
        yield
    finally:
        if requested:
            kernel.SetThreadExecutionState(0x80000000)  # Restore the normal idle policy.


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def make_manifest(browser):
    browser = Path(browser)
    if not (browser / "chrome.exe").is_file():
        raise RuntimeError("Packaged browser is missing chrome.exe")
    result = {}
    for path in sorted(browser.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("Symlinks are not allowed in a browser package")
        if path.is_file():
            result[path.relative_to(browser).as_posix()] = sha256(path)
    return result


def verify_manifest(browser, files):
    browser = Path(browser).resolve()
    if "chrome.exe" not in files:
        raise ValueError("Manifest must include chrome.exe")
    for relative, expected_hash in files.items():
        path = (browser / relative).resolve()
        if browser not in path.parents:
            raise ValueError("Manifest path escapes browser directory")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("Invalid file hash in manifest")
        if not path.is_file() or sha256(path) != expected_hash:
            raise RuntimeError("Browser integrity check failed: " + relative)


def smoke_test(browser, version):
    # Never run experimental builds against the real user profile.
    with tempfile.TemporaryDirectory(prefix="chromo-bro-smoke-") as temporary:
        completed = subprocess.run(
            [str(Path(browser) / "chrome.exe"), "--headless", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--user-data-dir=" + temporary,
             "--dump-dom", "data:text/html,<title>chromo-bro-smoke</title><p>ok</p>"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            creationflags=HELPER_FLAGS)
        if completed.returncode or "chromo-bro-smoke" not in completed.stdout:
            raise RuntimeError("Browser smoke test failed: " + completed.stderr[-3000:])
        last_version = Path(temporary) / "Last Version"
        if not last_version.exists() or last_version.read_text().strip() != version:
            raise RuntimeError("Browser reports a version different from the requested release")


def status(config, phase, **details):
    value = {"phase": phase, "updated_at": utc_now(), **details}
    write_json(config["build_root"] / "status.json", value)
    LOG.info("Status: %s", phase)


def build(config, release):
    root = config["build_root"]
    version = release["version"]
    commit = release["hashes"]["chromium"]
    version_tuple(version)
    source = root / "checkout/src"
    depot = root / "depot_tools"
    out = source / "out/ChromoBro"
    root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(root).free / (1024 ** 3)
    if free_gb < config["minimum_free_gb"]:
        raise RuntimeError("Build drive needs at least %s GB free; currently %.1f" %
                           (config["minimum_free_gb"], free_gb))
    if not config["visual_studio"].is_dir():
        raise RuntimeError("Install Visual Studio 2026 C++ Build Tools before building")
    env = build_environment(config)
    if not depot.exists():
        run([config.get("git", "git"), "clone", DEPOT_URL, depot], env=env)
    if not (depot / "python3_bin_reldir.txt").is_file():
        status(config, "bootstrapping-tools", version=version, source_commit=commit)
        run([depot / "bootstrap/win_tools.bat"], cwd=depot, env=env)
    checkout = source.parent
    checkout.mkdir(parents=True, exist_ok=True)
    client = checkout / ".gclient"
    if not client.exists():
        client.write_text('solutions = [{"name": "src", "url": "' + SOURCE_URL +
                          '", "managed": False, "custom_deps": {}, '
                          '"custom_vars": {"checkout_pgo_profiles": False}}]\n'
                          'target_os = ["win"]\n', encoding="utf-8")
    status(config, "syncing", version=version, source_commit=commit)
    run([depot / "gclient.bat", "sync", "--no-history", "--nohooks",
         "--jobs", config["sync_jobs"], "--revision", "src@" + commit], cwd=checkout, env=env)
    actual = run([config.get("git", "git"), "-C", source, "rev-parse", "HEAD"], env=env, capture=True)
    if actual != commit:
        raise RuntimeError("Source checkout does not match Google's release commit")
    status(config, "running-hooks", version=version, source_commit=commit)
    run([depot / "gclient.bat", "runhooks"], cwd=checkout, env=env)
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "args.gn", out / "args.gn")
    status(config, "compiling", version=version, source_commit=commit)
    run([source / "buildtools/win/gn.exe", "gen", out], cwd=source, env=env)
    run([depot / "autoninja.bat", "-C", out, "-j", config["jobs"], "mini_installer"],
        cwd=source, env=env)
    package = out / "chrome.7z"
    if not package.is_file():
        raise RuntimeError("Build did not produce chrome.7z")
    build_dir = root / "builds" / (version + "-" + commit[:12])
    if build_dir.exists():
        # Do not overwrite a previous incomplete or verified package.
        build_dir = build_dir.with_name(build_dir.name + "-" + uuid.uuid4().hex[:8])
    unpacked = build_dir / "unpacked"
    unpacked.mkdir(parents=True)
    run([config["seven_zip"], "x", package, "-o" + str(unpacked), "-y", "-bso0", "-bsp0"])
    candidates = list(unpacked.rglob("chrome.exe"))
    if len(candidates) != 1:
        raise RuntimeError("Expected one browser executable in the packaged build")
    browser = build_dir / "browser"
    shutil.move(str(candidates[0].parent), browser)
    status(config, "verifying", version=version, source_commit=commit)
    smoke_test(browser, version)
    manifest = {
        "version": version, "source_commit": commit, "source_url": SOURCE_URL,
        "built_at": utc_now(), "build_args": (REPO / "args.gn").read_text(),
        "archive_sha256": sha256(package), "files": make_manifest(browser),
    }
    write_json(build_dir / "manifest.json", manifest)
    pending = {"build_dir": str(build_dir), "version": version, "source_commit": commit,
               "manifest_sha256": sha256(build_dir / "manifest.json")}
    write_json(root / "pending.json", pending)
    status(config, "ready-to-install", **pending)
    return pending


def browser_running(config):
    # Query only processes whose executable belongs to this browser installation.
    root = str(config["install_root"]).replace("'", "''")
    script = ("$root='" + root + "\\'; @(Get-CimInstance Win32_Process "
              "-Filter \"name='chrome.exe'\" -ErrorAction Stop | Where-Object { "
              "$_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, "
              "[StringComparison]::OrdinalIgnoreCase) }).Count")
    result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                 capture=True, timeout=30)
    return int(result) > 0


def install_pending(config):
    root = config["build_root"]
    install = config["install_root"]
    with exclusive_lock(root / "locks/install.lock"):
        pending = read_json(root / "pending.json")
        if not pending:
            return False
        if browser_running(config):
            LOG.info("Browser is running; leaving the verified update pending")
            return False
        build_dir = Path(pending["build_dir"]).resolve()
        builds = (root / "builds").resolve()
        if builds not in build_dir.parents:
            raise ValueError("Pending build is outside the build directory")
        manifest_path = build_dir / "manifest.json"
        if sha256(manifest_path) != pending["manifest_sha256"]:
            raise RuntimeError("Pending build manifest has changed")
        manifest = read_json(manifest_path)
        if manifest["version"] != pending["version"] or manifest["source_commit"] != pending["source_commit"]:
            raise RuntimeError("Pending build metadata mismatch")
        installed = read_json(install / "chromo-bro-installed.json", {})
        if installed.get("version"):
            comparison = (version_tuple(pending["version"]), version_tuple(installed["version"]))
            if comparison[0] == comparison[1] and installed.get("manifest_sha256") == pending["manifest_sha256"]:
                # Recover if power was lost after recording the installation but
                # before removing the pending marker.
                verify_manifest(install / "bin", manifest["files"])
                (root / "pending.json").unlink()
                return False
            if comparison[0] <= comparison[1]:
                raise RuntimeError("Refusing to install an older or equal browser version")
        profile_version = install / "profile/Last Version"
        if profile_version.exists():
            last_version = profile_version.read_text(encoding="utf-8").strip()
            if VERSION_RE.fullmatch(last_version) and version_tuple(pending["version"]) < version_tuple(last_version):
                raise RuntimeError("Refusing to downgrade a profile opened by a newer Chromium")
        verify_manifest(build_dir / "browser", manifest["files"])
        install.mkdir(parents=True, exist_ok=True)
        stage = install / ("bin-next-" + uuid.uuid4().hex)
        shutil.copytree(build_dir / "browser", stage)
        verify_manifest(stage, manifest["files"])
        smoke_test(stage, pending["version"])
        # Recheck after staging, in case the user opened Chromium during verification.
        if browser_running(config):
            LOG.info("Browser opened during staging; update remains pending at %s", stage)
            return False
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        backup = install / "backups" / stamp
        backup.mkdir(parents=True)
        profile = install / "profile"
        if profile.exists():
            LOG.info("Backing up profile before migration to %s", pending["version"])
            shutil.copytree(profile, backup / "profile")
        if browser_running(config):
            LOG.info("Browser opened during profile backup; update remains pending")
            return False
        binary = install / "bin"
        previous = backup / "bin"
        if binary.exists():
            binary.rename(previous)
        try:
            stage.rename(binary)
        except OSError:
            if previous.exists() and not binary.exists():
                previous.rename(binary)
            raise
        installed = {**pending, "installed_at": utc_now(), "backup": str(backup)}
        write_json(install / "chromo-bro-installed.json", installed)
        # chrlauncher must not overwrite this self-built browser with Hibbiki builds.
        launcher_ini = install / "chrlauncher.ini"
        if launcher_ini.exists():
            with launcher_ini.open("rb") as handle:
                encoding = "utf-16" if handle.read(2) in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
            text = launcher_ini.read_text(encoding=encoding)
            text = re.sub(r"(?m)^ChromiumAutoDownload=.*$", "ChromiumAutoDownload=false", text)
            text = re.sub(r"(?m)^ChromiumCheckPeriod=.*$", "ChromiumCheckPeriod=0", text)
            shutil.copy2(launcher_ini, backup / "chrlauncher.ini")
            launcher_ini.write_text(text, encoding=encoding)
        (root / "pending.json").unlink()
        status(config, "installed", **installed)
        return True


def update(config):
    root = config["build_root"]
    with exclusive_lock(root / "locks/build.lock"):
        release = latest_release()
        version = release["version"]
        pending = read_json(root / "pending.json", {})
        installed = read_json(config["install_root"] / "chromo-bro-installed.json", {})
        if installed.get("version") and version_tuple(installed["version"]) >= version_tuple(version):
            LOG.info("Already running the latest Stable source version: %s", installed["version"])
            return
        if pending.get("version") != version:
            with stay_awake():
                build(config, release)
        install_pending(config)


def launch(config, urls):
    try:
        install_pending(config)
    except Exception:
        LOG.exception("Could not apply pending update; launching current browser")
    binary = config["install_root"] / "bin/chrome.exe"
    if not binary.is_file():
        raise RuntimeError("No installed browser at " + str(binary))
    subprocess.Popen([str(binary), "--user-data-dir=" + str(config["install_root"] / "profile"),
                      "--no-default-browser-check"] + urls, cwd=binary.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["update", "install-pending", "launch", "status", "release"])
    parser.add_argument("--config", type=Path, default=REPO / "local.json")
    parser.add_argument("urls", nargs="*")
    args = parser.parse_args()
    if args.command == "release":
        print(json.dumps(latest_release(), indent=2))
        return 0
    config = load_config(args.config)
    logs = config["build_root"] / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(logs / (args.command + ".log"), encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    try:
        if args.command == "update":
            update(config)
        elif args.command == "install-pending":
            install_pending(config)
        elif args.command == "launch":
            launch(config, args.urls)
        else:
            print(json.dumps({"build": read_json(config["build_root"] / "status.json"),
                              "installed": read_json(config["install_root"] / "chromo-bro-installed.json"),
                              "pending": read_json(config["build_root"] / "pending.json")}, indent=2))
        return 0
    except OperationInProgress as exc:
        LOG.info("%s; this invocation will skip the overlapping run", exc)
        return 0
    except Exception as exc:
        LOG.exception("Operation failed")
        if args.command == "update":
            status(config, "failed", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
