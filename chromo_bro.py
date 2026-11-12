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
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))  # Embedded Python also runs builder scripts outside its runtime.
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
    path = Path(path).resolve()
    config = read_json(path)
    if config is None:
        raise RuntimeError("Missing local configuration: " + str(path))
    for key in ("build_root", "install_root", "visual_studio", "seven_zip"):
        if key in config:
            value = Path(config[key])
            config[key] = (value if value.is_absolute() else path.parent / value).resolve()
    if config["build_root"] == config["install_root"]:
        raise ValueError("Build and browser directories must be separate")
    config.setdefault("jobs", 3)
    config.setdefault("sync_jobs", 2)
    config.setdefault("minimum_free_gb", 100)
    config.setdefault("keep_backups", 2)
    for key in ("jobs", "sync_jobs"):
        if not isinstance(config[key], int) or config[key] < 1:
            raise ValueError(key + " must be a positive integer")
    return config


def state_root(config):
    return config["install_root"] / ".chromo"


def install_lock(config):
    return state_root(config) / "locks/install.lock"


def safe_remove_tree(path, parent):
    """Delete only an ordinary directory strictly inside the supplied owned root."""
    path, parent = Path(path).absolute(), Path(parent).resolve()
    if path.is_symlink() or path.resolve() == parent or parent not in path.resolve().parents:
        raise ValueError("Cleanup target is outside its owned directory")
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in ["."] + dirs + files:
            item = Path(directory) / name
            if getattr(item.lstat(), "st_file_attributes", 0) & 0x400 or item.is_symlink():
                raise ValueError("Cleanup will not traverse reparse points")
    shutil.rmtree(path)


def tree_manifest(directory, exclude=()):
    directory = Path(directory)
    result = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse point in backup")
        if path.is_file() and path.relative_to(directory).as_posix() not in exclude:
            result[path.relative_to(directory).as_posix()] = sha256(path)
    return result


def prune_backups(config):
    root = config["install_root"] / "backups"
    keep = max(2, int(config.get("keep_backups", 2)))
    managed = []
    if not root.exists():
        return []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink() or getattr(directory.lstat(), "st_file_attributes", 0) & 0x400:
            continue
        marker = read_json(directory / "backup.json", {})
        if marker.get("owner") == "chromo-bro" and marker.get("complete") is True:
            managed.append((marker.get("completed_at", ""), directory, marker))
    verified = []
    for entry in sorted(managed, key=lambda item: (item[0], item[1].name), reverse=True):
        _, directory, marker = entry
        if (not VERSION_RE.fullmatch(marker.get("version", "")) or
                marker.get("profile_version") not in (None, marker["version"]) or
                tree_manifest(directory, exclude=("backup.json",)) != marker.get("files")):
            LOG.warning("Preserving modified backup: %s", directory)
            continue
        verified.append(entry)
    removed = []
    for _, directory, marker in verified[keep:]:
        safe_remove_tree(directory, root)
        removed.append(directory.name)
    return removed


def cleanup_stages(config):
    ledger_root = state_root(config) / "stages"
    removed = []
    if not ledger_root.exists():
        return removed
    install = config["install_root"].resolve()
    for record in ledger_root.glob("*.json"):
        value = read_json(record, {})
        name = value.get("name", "")
        if value.get("owner") != "chromo-bro" or not re.fullmatch(r"bin-next-[0-9a-f]{32}", name):
            continue
        directory = install / name
        backup_name = value.get("backup")
        backup = install / "backups" / backup_name if backup_name and re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{6}", backup_name) else None
        if backup and (backup / "bin").exists():
            if (backup.is_symlink() or getattr(backup.lstat(), "st_file_attributes", 0) & 0x400 or
                    getattr((backup / "bin").lstat(), "st_file_attributes", 0) & 0x400):
                raise ValueError("Interrupted backup contains a reparse point")
            if browser_running(config):
                raise OperationInProgress("Waiting for Chromium to close before recovering an interrupted update")
            binary = install / "bin"
            manifest, pending = value.get("manifest", {}), value.get("pending", {})
            if binary.exists():
                # Finish an interrupted successful swap without changing profiles.
                verify_manifest(binary, manifest.get("files", {}))
                if manifest.get("version") != pending.get("version"):
                    raise RuntimeError("Interrupted update metadata is inconsistent")
                current_profile = install / "profile/Last Version"
                if current_profile.exists() and version_tuple(current_profile.read_text().strip()) > version_tuple(manifest["version"]):
                    raise RuntimeError("The current profile is newer than the interrupted browser; files were preserved")
                if value.get("backup_verified"):
                    old = value.get("old", {})
                    profile_file = backup / "profile/Last Version"
                    previous_version = profile_file.read_text().strip() if profile_file.exists() else None
                    write_json(backup / "backup.json", {"owner": "chromo-bro", "complete": True,
                               "version": old.get("version") or previous_version or "unknown", "profile_version": previous_version,
                               "completed_at": utc_now(), "files": tree_manifest(backup, exclude=("backup.json",))})
                local = state_root(config) / "browser-manifest.json"
                write_json(local, manifest)
                write_json(install / "chromo-bro-installed.json", {**pending, "installed_at": utc_now(),
                           "manifest_path": str(local), "backup": str(backup), "recovered": True})
                marker = config["build_root"] / "pending.json"
                try:
                    if str(marker) == value.get("pending_path") and read_json(marker, {}).get("manifest_sha256") == pending.get("manifest_sha256"):
                        marker.unlink()
                except OSError:
                    pass
            else:
                old_version = value.get("old", {}).get("version")
                profile_file = install / "profile/Last Version"
                if profile_file.exists() and (not old_version or version_tuple(profile_file.read_text().strip()) > version_tuple(old_version)):
                    raise RuntimeError("Profile is newer than the interrupted rollback browser; files were preserved")
                (backup / "bin").rename(binary)
        if backup and backup.exists() and not (backup / "bin").exists():
            remaining = {entry.name for entry in backup.iterdir()}
            copied_profile, current_profile = backup / "profile", install / "profile"
            same_profile = not copied_profile.exists() or (current_profile.exists() and tree_manifest(copied_profile) == tree_manifest(current_profile))
            if same_profile and remaining <= {"profile", "installed.json", "browser-manifest.json", "chrlauncher.ini", "backup.json"}:
                safe_remove_tree(backup, install / "backups")
        if directory.exists():
            safe_remove_tree(directory, install)
            removed.append(name)
        record.unlink()
    return removed


def cleanup(config):
    with exclusive_lock(install_lock(config)):
        result = {"stages": cleanup_stages(config), "backups": prune_backups(config)}
        write_json(state_root(config) / "last-cleanup.json", {"at": utc_now(), **result})
        return result


def record_check(config, version, source="upstream Stable"):
    write_json(state_root(config) / "last-check.json", {"at": utc_now(), "version": version, "source": source, "success": True})


def about_status(config, phase, progress=0):
    """Publish the small, path-free status read by About Chromium."""
    value = {"schema": 1, "phase": phase, "progress": progress}
    if phase in ("checking", "updating"):
        value["pid"] = os.getpid()
    try:
        write_json(state_root(config) / "about-update.json", value)
    except OSError:
        LOG.warning("Could not write About Chromium status", exc_info=True)


def about_progress(config):
    last_percent = 0

    def report(completed, total):
        nonlocal last_percent
        if total > 0:
            percent = min(99, completed * 100 // total)
            if percent > last_percent:
                last_percent = percent
                about_status(config, "updating", percent)

    return report


def record_failure(config, operation, error):
    if operation in ("update", "apply", "install-pending"):
        about_status(config, "failed")
    root = state_root(config)
    previous = read_json(root / "last-error.json", {})
    message = str(error)[-2000:]
    fingerprint = hashlib.sha256((operation + ":" + message).encode()).hexdigest()
    now = dt.datetime.now(dt.timezone.utc)
    last_notified = previous.get("notified_at")
    recent = last_notified and (now - dt.datetime.fromisoformat(last_notified)).total_seconds() < 6 * 3600
    notify = not (previous.get("fingerprint") == fingerprint and recent)
    event = {"at": utc_now(), "operation": operation, "message": message, "fingerprint": fingerprint,
             "notified_at": previous.get("notified_at")}
    launcher = config["install_root"] / "Chromo.exe"
    if notify and launcher.exists():
        try:
            subprocess.Popen([str(launcher), "--notify", "Chromo " + operation + " failed. Run chromo status for details."],
                             creationflags=HELPER_FLAGS)
            event["notified_at"] = utc_now()
        except OSError:
            LOG.exception("Could not show update notification")
    write_json(root / "last-error.json", event)


@contextlib.contextmanager
def exclusive_lock(path, wait_seconds=0):
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OperationInProgress("Another operation holds " + str(path)) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run(args, cwd=None, env=None, capture=False, timeout=None, on_line=None):
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
            if on_line is not None:
                on_line(line)
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
    """Prevent automatic system sleep only while this operation is active."""
    import ctypes
    if os.name != "nt":
        yield
        return
    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    set_state.argtypes = [ctypes.c_uint]
    set_state.restype = ctypes.c_uint
    previous = set_state(0x80000001)  # CONTINUOUS | SYSTEM_REQUIRED
    try:
        yield
    finally:
        if previous:
            set_state(previous)  # Preserve an enclosing operation's sleep request.


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


def prepare_browser_permissions(browser):
    """Give Windows AppContainer sandboxes read/execute access to this package."""
    browser = Path(browser).resolve()
    if not (browser / "chrome.exe").is_file():
        raise RuntimeError("Cannot prepare permissions without a browser executable")
    if os.name == "nt":
        # Portable archives inherit their destination's ACL. External build
        # drives can lack both normal and restricted AppContainer access.
        # Grant only RX on this browser tree, never on profiles or source.
        run(["icacls.exe", browser, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)",
             "*S-1-15-2-2:(OI)(CI)(RX)", "/T", "/Q"], capture=True, timeout=120)


def smoke_test(browser, version):
    prepare_browser_permissions(browser)
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
    write_json(state_root(config) / "builder-status.json", value)
    LOG.info("Status: %s", phase)


def compiler_worker_running(output_directory):
    directory = str(Path(output_directory).resolve()).replace("'", "''")
    script = ("$directory='" + directory + "'; @(Get-CimInstance Win32_Process "
              "-Filter \"name='siso.exe'\" -ErrorAction Stop | Where-Object { "
              "$_.CommandLine -and $_.CommandLine.IndexOf($directory, "
              "[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count")
    return int(run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture=True, timeout=30)) > 0


@contextlib.contextmanager
def patch_about_page(config, source, env):
    patch = REPO / "patches/about-update.patch"
    git = [config.get("git", "git"), "-C", source, "apply"]
    run([*git, "--check", patch], env=env)
    run([*git, patch], env=env)
    try:
        yield
    finally:
        run([*git, "--reverse", patch], env=env)


def build(config, release):
    root = config["build_root"]
    version = release["version"]
    commit = release["hashes"]["chromium"]
    version_tuple(version)
    source = root / "checkout/src"
    depot = root / "depot_tools"
    out = source / "out/ChromoBro"
    if compiler_worker_running(out):
        raise OperationInProgress("A compiler worker is still using " + str(out))
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
    # Recover our exact temporary patch if a previous build was interrupted.
    patch = REPO / "patches/about-update.patch"
    git = [str(config.get("git", "git")), "-C", str(source), "apply", "--reverse"]
    if source.is_dir() and subprocess.run([*git, "--check", str(patch)], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                          creationflags=HELPER_FLAGS).returncode == 0:
        run([*git, patch], env=env)
    about_status(config, "updating")
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
    report = about_progress(config)

    def compile_progress(line):
        match = re.match(r"\s*\[(\d+)/(\d+)\]", line)
        if match:
            report(int(match[1]), int(match[2]))

    with patch_about_page(config, source, env):
        run([source / "buildtools/win/gn.exe", "gen", out], cwd=source, env=env)
        run([depot / "autoninja.bat", "-C", out, "-j", config["jobs"], "mini_installer"],
            cwd=source, env=env, on_line=compile_progress)
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
    with exclusive_lock(install_lock(config), wait_seconds=300):
        write_json(root / "pending.json", pending)
        about_status(config, "ready")
    if config.get("package_releases", False):
        try:
            status(config, "packaging-release", version=version, source_commit=commit)
            from package_release import package as package_distribution
            package_distribution(build_dir, root / "dist" / version, root / "package-cache")
        except Exception as exc:
            LOG.exception("Distribution packaging failed; verified local update remains available")
            record_failure(config, "distribution", exc)
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
    root, install = config["build_root"], config["install_root"]
    with exclusive_lock(install_lock(config)), contextlib.ExitStack() as operation:
        cleanup_stages(config)
        pending = read_json(root / "pending.json")
        if not pending:
            return False
        if browser_running(config):
            LOG.info("Browser is running; leaving the verified update pending")
            return False
        operation.enter_context(stay_awake())
        build_dir = Path(pending["build_dir"]).resolve()
        if (root / "builds").resolve() not in build_dir.parents:
            raise ValueError("Pending build is outside the build directory")
        manifest_path = build_dir / "manifest.json"
        if sha256(manifest_path) != pending["manifest_sha256"]:
            raise RuntimeError("Pending build manifest has changed")
        manifest = read_json(manifest_path)
        if manifest["version"] != pending["version"] or manifest["source_commit"] != pending["source_commit"]:
            raise RuntimeError("Pending build metadata mismatch")
        old = read_json(install / "chromo-bro-installed.json", {})
        if old.get("version"):
            comparison = version_tuple(pending["version"]), version_tuple(old["version"])
            if comparison[0] == comparison[1] and old.get("manifest_sha256") == pending["manifest_sha256"]:
                verify_manifest(install / "bin", manifest["files"])
                (root / "pending.json").unlink()
                return False
            if comparison[0] <= comparison[1]:
                raise RuntimeError("Refusing to install an older or equal browser version")
        profile = install / "profile"
        profile_version = profile / "Last Version"
        last_version = profile_version.read_text(encoding="utf-8").strip() if profile_version.exists() else None
        if last_version and VERSION_RE.fullmatch(last_version) and version_tuple(pending["version"]) < version_tuple(last_version):
            raise RuntimeError("Refusing to downgrade a profile opened by a newer Chromium")
        verify_manifest(build_dir / "browser", manifest["files"])
        install.mkdir(parents=True, exist_ok=True)
        required = 2 * 1024 ** 3
        for directory in (build_dir / "browser", profile):
            if directory.exists():
                required += sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
        if shutil.disk_usage(install).free < required:
            raise RuntimeError("Browser drive needs %.1f GB free for staging, profile backup, and a 2 GB reserve" % (required / 1024 ** 3))
        stage = install / ("bin-next-" + uuid.uuid4().hex)
        ledger = state_root(config) / "stages" / (stage.name + ".json")
        journal = {"owner": "chromo-bro", "name": stage.name, "created_at": utc_now(), "manifest": manifest,
                   "pending": pending, "pending_path": str(root / "pending.json"), "old": old}
        write_json(ledger, journal)
        backup = None
        committed = False
        swapped = False
        old_local = read_json(state_root(config) / "browser-manifest.json")
        try:
            shutil.copytree(build_dir / "browser", stage)
            verify_manifest(stage, manifest["files"])
            smoke_test(stage, pending["version"])
            if browser_running(config):
                LOG.info("Browser opened during staging; update remains pending")
                return False
            binary = install / "bin"
            if binary.exists():
                stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                backup = install / "backups" / stamp
                backup.mkdir(parents=True)
                journal["backup"] = stamp
                write_json(ledger, journal)
                if profile.exists():
                    LOG.info("Backing up profile before migration to %s", pending["version"])
                    original_profile = tree_manifest(profile)
                    shutil.copytree(profile, backup / "profile")
                    if tree_manifest(backup / "profile") != original_profile:
                        raise RuntimeError("Profile backup verification failed")
                journal["backup_verified"] = True
                write_json(ledger, journal)
                write_json(backup / "installed.json", old)
                if old_local:
                    write_json(backup / "browser-manifest.json", old_local)
                if browser_running(config):
                    LOG.info("Browser opened during profile backup; update remains pending")
                    return False
                binary.rename(backup / "bin")
            try:
                stage.rename(binary)
                swapped = True
            except OSError:
                if backup and (backup / "bin").exists() and not binary.exists():
                    (backup / "bin").rename(binary)
                raise
            launcher_ini = install / "chrlauncher.ini"
            if launcher_ini.exists():
                encoding = "utf-16" if launcher_ini.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
                ini = launcher_ini.read_text(encoding=encoding)
                if backup:
                    shutil.copy2(launcher_ini, backup / "chrlauncher.ini")
                ini = re.sub(r"(?m)^ChromiumAutoDownload=.*$", "ChromiumAutoDownload=false", ini)
                ini = re.sub(r"(?m)^ChromiumCheckPeriod=.*$", "ChromiumCheckPeriod=0", ini)
                launcher_ini.write_text(ini, encoding=encoding)
            if backup:
                write_json(backup / "backup.json", {"owner": "chromo-bro", "complete": True,
                           "version": old.get("version") or last_version or "unknown",
                           "profile_version": last_version, "completed_at": utc_now(), "files": tree_manifest(backup, exclude=("backup.json",))})
            local_manifest = state_root(config) / "browser-manifest.json"
            write_json(local_manifest, manifest)
            installed = {**pending, "installed_at": utc_now(), "backup": str(backup) if backup else None,
                         "manifest_path": str(local_manifest)}
            write_json(install / "chromo-bro-installed.json", installed)
            committed = True
            (root / "pending.json").unlink()
            status(config, "installed", **installed)
            last_check = read_json(state_root(config) / "last-check.json", {})
            if last_check.get("version") and version_tuple(last_check["version"]) <= version_tuple(pending["version"]):
                about_status(config, "current")
            try:
                prune_backups(config)
            except Exception as exc:
                LOG.exception("Backup retention needs attention")
                record_failure(config, "cleanup", exc)
            return True
        except Exception:
            if not committed:
                binary = install / "bin"
                if swapped and binary.exists():
                    binary.rename(stage)
                if backup and (backup / "bin").exists() and not binary.exists():
                    (backup / "bin").rename(binary)
                if old_local:
                    write_json(state_root(config) / "browser-manifest.json", old_local)
                else:
                    (state_root(config) / "browser-manifest.json").unlink(missing_ok=True)
                if old:
                    write_json(install / "chromo-bro-installed.json", old)
                else:
                    (install / "chromo-bro-installed.json").unlink(missing_ok=True)
                if backup and (backup / "chrlauncher.ini").exists():
                    shutil.copy2(backup / "chrlauncher.ini", install / "chrlauncher.ini")
            raise
        finally:
            recovery_needed = not committed and backup and (backup / "bin").exists()
            if not recovery_needed:
                if stage.exists():
                    safe_remove_tree(stage, install)
                ledger.unlink(missing_ok=True)
                if not committed and backup and backup.exists():
                    safe_remove_tree(backup, install / "backups")


def update(config):
    root = config["build_root"]
    with exclusive_lock(root / "locks/build.lock"), stay_awake():
        about_status(config, "checking")
        release = latest_release()
        version = release["version"]
        record_check(config, version)
        pending = read_json(root / "pending.json", {})
        installed = read_json(config["install_root"] / "chromo-bro-installed.json", {})
        if installed.get("version") and version_tuple(installed["version"]) >= version_tuple(version):
            about_status(config, "current")
            LOG.info("Already running the latest Stable source version: %s", installed["version"])
            return
        if pending.get("version") != version:
            build(config, release)
        else:
            about_status(config, "ready")
        install_pending(config)


def launch(config, urls):
    try:
        install_pending(config)
    except OperationInProgress:
        LOG.info("An installation is active; waiting before starting the browser")
    except Exception:
        LOG.exception("Could not apply pending update; launching current browser")
        record_failure(config, "apply", sys.exc_info()[1])
    # Installation and process creation must share a lock. A launcher cannot
    # start the old browser between the installer's final process check and swap.
    with exclusive_lock(install_lock(config), wait_seconds=300):
        binary = config["install_root"] / "bin/chrome.exe"
        if not binary.is_file():
            raise RuntimeError("No installed browser at " + str(binary))
        installed = read_json(config["install_root"] / "chromo-bro-installed.json", {})
        profile_version = config["install_root"] / "profile/Last Version"
        if installed.get("version") and profile_version.exists() and version_tuple(profile_version.read_text().strip()) > version_tuple(installed["version"]):
            raise RuntimeError("The profile was opened by a newer Chromium; refusing to launch an older browser")
        subprocess.Popen([str(binary), "--user-data-dir=" + str(config["install_root"] / "profile"),
                          "--no-default-browser-check"] + urls, cwd=binary.parent,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=HELPER_FLAGS)


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
        record_failure(config, args.command, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
