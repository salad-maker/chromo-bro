"""Portable Chromo launcher, release downloader, and readable local status."""
import argparse
import ctypes
import datetime as dt
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
import zipfile

import chromo_bro as core

APP_VERSION = "1.0.0"
REPOSITORY = "salad-maker/chromo-bro"
COMMANDS = {"launch", "status", "update", "apply", "cleanup", "verify", "setup"}


def client_config(root):
    root = Path(root).resolve()
    config = core.read_json(root / "runtime/client.json", {})
    config.update({"install_root": root, "build_root": root / ".chromo/downloads"})
    config.setdefault("mode", "release")
    config.setdefault("repository", REPOSITORY)
    config.setdefault("keep_backups", 2)
    return config


def effective_config(client):
    builder = client.get("builder_config")
    if client.get("mode") == "source" and builder and Path(builder).is_file():
        result = core.load_config(builder)
        if result["install_root"] != client["install_root"]:
            raise ValueError("Builder configuration points to a different installation")
        return result
    return client


def validate_asset_url(url, repository):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password:
        raise ValueError("Release assets must come from GitHub over HTTPS")
    if not parsed.path.startswith("/" + repository + "/releases/download/"):
        raise ValueError("Release asset is outside the configured repository")


def download(url, destination, digest, maximum=2 * 1024**3, on_progress=None):
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Missing SHA-256 download digest")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part-" + uuid.uuid4().hex)
    size = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "chromo-bro/" + APP_VERSION})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                raise ValueError("Refusing an insecure download redirect")
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > maximum:
                    raise ValueError("Download exceeds the package size limit")
                output.write(block)
                if on_progress is not None:
                    on_progress(size)
        if core.sha256(temporary) != digest:
            raise RuntimeError("Downloaded package SHA-256 does not match")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def extract_zip(archive, destination, maximum_unpacked=4 * 1024**3):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Extraction destination already exists")
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 20000:
            raise ValueError("Release ZIP has too many entries")
        total = 0
        seen = set()
        for entry in entries:
            relative = PurePosixPath(entry.filename)
            raw_name = entry.orig_filename
            key = entry.filename.rstrip("/").casefold()
            reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM" + str(i) for i in range(1, 10)} | {"LPT" + str(i) for i in range(1, 10)}
            if (not relative.parts or relative.is_absolute() or ".." in relative.parts or
                    "\\" in raw_name or ":" in raw_name or any(ord(c) < 32 for c in raw_name) or key in seen or
                    any(part.rstrip(" .") != part or part.split(".")[0].upper() in reserved for part in relative.parts) or
                    ((entry.external_attr >> 16) & 0o170000) == 0o120000):
                raise ValueError("Unsafe or duplicate path in release ZIP")
            seen.add(key)
            target = (destination / str(relative)).resolve()
            if destination not in target.parents:
                raise ValueError("ZIP path escapes the extraction directory")
            total += entry.file_size
            if total > maximum_unpacked:
                raise ValueError("Unpacked release is too large")
        destination.mkdir(parents=True)
        for entry in entries:
            target = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def latest_published(client):
    repository = client["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid release repository")
    release = core.fetch_json("https://api.github.com/repos/" + repository + "/releases/latest")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("Refusing a draft or prerelease")
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    asset = assets.get("release-manifest.json")
    if not asset:
        raise RuntimeError("The latest release has no verified package manifest")
    validate_asset_url(asset["browser_download_url"], repository)
    digest = asset.get("digest", "").removeprefix("sha256:")
    manifest_path = core.state_root(client) / "latest-release.json"
    download(asset["browser_download_url"], manifest_path, digest, maximum=1024 * 1024)
    manifest = core.read_json(manifest_path)
    core.version_tuple(manifest["version"])
    if manifest.get("schema") != 1 or manifest.get("platform") != "win64" or manifest.get("channel") != "Stable" or not core.SHA_RE.fullmatch(manifest.get("source_commit", "")):
        raise ValueError("Invalid release metadata")
    browser = manifest["browser_asset"]
    payload = assets.get(browser["name"])
    if not payload or payload.get("digest") != "sha256:" + browser["sha256"] or payload.get("size") != browser.get("size"):
        raise ValueError("Release metadata and GitHub asset digest disagree")
    validate_asset_url(payload["browser_download_url"], repository)
    return manifest, payload


def cleanup_downloads(client):
    if client.get("mode") != "release":
        return []
    with core.exclusive_lock(core.install_lock(client)):
        pending = core.read_json(client["build_root"] / "pending.json", {})
        root = client["build_root"] / "builds"
        removed = []
        for directory in root.glob("*"):
            if not directory.is_dir() or directory.is_symlink() or str(directory.resolve()) == pending.get("build_dir"):
                continue
            marker = core.read_json(directory / ".download.json", {})
            if marker.get("owner") != "chromo-bro":
                continue
            actual = core.tree_manifest(directory)
            actual.pop(".download.json", None)
            if actual != marker.get("files"):
                core.LOG.warning("Preserving modified download cache: %s", directory)
                continue
            core.safe_remove_tree(directory, root)
            removed.append(directory.name)
        return removed


def update_release(client):
    with core.exclusive_lock(core.state_root(client) / "locks/download.lock"), core.stay_awake():
        core.about_status(client, "checking")
        manifest, asset = latest_published(client)
        version = manifest["version"]
        core.record_check(client, version, "published Chromo release")
        installed = core.read_json(client["install_root"] / "chromo-bro-installed.json", {})
        if installed.get("version") and core.version_tuple(installed["version"]) >= core.version_tuple(version):
            core.about_status(client, "current")
            return "Already running the latest published browser: " + installed["version"]
        root = client["build_root"]
        with core.exclusive_lock(core.install_lock(client)):
            pending = core.read_json(root / "pending.json", {})
        if pending.get("version") != version:
            unpacked = manifest.get("browser_unpacked_bytes", 0)
            if not isinstance(unpacked, int) or not 0 < unpacked <= 4 * 1024**3:
                raise ValueError("Invalid unpacked browser size")
            profile = client["install_root"] / "profile"
            profile_size = sum(path.stat().st_size for path in profile.rglob("*") if path.is_file()) if profile.exists() else 0
            required = asset["size"] + 2 * unpacked + profile_size + 2 * 1024**3
            if shutil.disk_usage(client["install_root"]).free < required:
                raise RuntimeError("Not enough space to download, stage, and back up the update while keeping a 2 GiB reserve")
            zip_path = root / ("browser-" + version + ".zip")
            core.about_status(client, "updating")
            report = core.about_progress(client)
            download(asset["browser_download_url"], zip_path, manifest["browser_asset"]["sha256"],
                     maximum=asset["size"], on_progress=lambda size: report(size, asset["size"]))
            build_dir = root / "builds" / (version + "-" + uuid.uuid4().hex[:8])
            staged = False
            try:
                extract_zip(zip_path, build_dir, maximum_unpacked=unpacked + 1024**2)
                binary_manifest = core.read_json(build_dir / "manifest.json")
                if binary_manifest["version"] != version or binary_manifest["source_commit"] != manifest["source_commit"]:
                    raise ValueError("Browser payload has different release metadata")
                core.verify_manifest(build_dir / "browser", binary_manifest["files"])
                core.smoke_test(build_dir / "browser", version)
                pending = {"version": version, "source_commit": manifest["source_commit"], "build_dir": str(build_dir),
                           "manifest_sha256": core.sha256(build_dir / "manifest.json")}
                core.write_json(build_dir / ".download.json", {"owner": "chromo-bro", "files": core.tree_manifest(build_dir)})
                with core.exclusive_lock(core.install_lock(client)):
                    core.write_json(root / "pending.json", pending)
                    core.about_status(client, "ready")
                staged = True
            finally:
                zip_path.unlink(missing_ok=True)
                if not staged and build_dir.exists():
                    core.safe_remove_tree(build_dir, root / "builds")
        else:
            core.about_status(client, "ready")
        installed_now = core.install_pending(client)
        if installed_now:
            cleanup_downloads(client)
        return ("Installed " if installed_now else "Verified update is pending; close Chromium to apply ") + version


def ensure_installed(client):
    root = client["install_root"]
    if (root / "chromo-bro-installed.json").exists():
        return
    with core.exclusive_lock(core.install_lock(client)):
        manifest = core.read_json(root / "runtime/browser-manifest.json")
        if not manifest:
            raise RuntimeError("The portable browser manifest is missing")
        profile_version = root / "profile/Last Version"
        if profile_version.exists() and core.version_tuple(profile_version.read_text().strip()) > core.version_tuple(manifest["version"]):
            raise RuntimeError("This package is older than the existing profile")
        core.verify_manifest(root / "bin", manifest["files"])
        core.prepare_browser_permissions(root / "bin")
        local = core.state_root(client) / "browser-manifest.json"
        core.write_json(local, manifest)
        core.write_json(root / "chromo-bro-installed.json", {"version": manifest["version"], "source_commit": manifest["source_commit"],
                        "installed_at": core.utc_now(), "manifest_path": str(local), "manifest_sha256": core.sha256(local), "backup": None})


def collect_status(client):
    root = client["install_root"]
    config = effective_config(client)
    state = core.state_root(client)
    installed = core.read_json(root / "chromo-bro-installed.json", {})
    build = core.read_json(state / "builder-status.json", {})
    if not build and config is not client:
        build = core.read_json(config["build_root"] / "status.json", {})
    last_check = core.read_json(state / "last-check.json", {})
    pending = core.read_json(config["build_root"] / "pending.json", {})
    backups = []
    for path in (root / "backups").glob("*/backup.json"):
        item = core.read_json(path, {})
        if item.get("owner") == "chromo-bro" and item.get("complete"):
            backups.append({"version": item.get("version"), "at": item.get("completed_at"), "path": str(path.parent)})
    last_error = core.read_json(state / "last-error.json")
    progress = None
    if config is not client and build.get("phase") == "compiling":
        log = config["build_root"] / "logs/update.log"
        if log.exists():
            with log.open("rb") as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 32768))
                lines = source.read().decode("utf-8", errors="replace").splitlines()
            progress = next((line for line in reversed(lines) if re.search(r"\[\d+/\d+\]", line)), None)
    return {"application_version": APP_VERSION, "browser_version": installed.get("version", "not installed"),
            "mode": client["mode"], "last_check": last_check, "build": build, "build_progress": progress,
            "pending_version": pending.get("version"), "last_error": last_error,
            "free_gib": round(shutil.disk_usage(root).free / 1024**3, 2),
            "backups": sorted(backups, key=lambda item: item["at"] or "", reverse=True),
            "builder_available": config is not client if client["mode"] == "source" else None,
            "profile": str(root / "profile"), "logs": str(state / "logs")}


def status_text(value):
    check = value["last_check"]
    lines = ["Chromo " + value["application_version"], "Chromium: " + value["browser_version"],
             "Updates: " + ("local Stable source builds" if value["mode"] == "source" else "verified published builds"),
             "Last successful check: " + check.get("at", "not checked yet"),
             "Latest checked version: " + check.get("version", "unknown"),
             "Pending update: " + (value["pending_version"] or "none"),
             "Free browser-drive space: " + str(value["free_gib"]) + " GiB",
             "Saved rollback sets: " + str(len(value["backups"]))]
    if value["mode"] == "source":
        lines += ["Builder: " + ("available" if value["builder_available"] else "offline; browser can still launch"),
                  "Build phase: " + value["build"].get("phase", "not started")]
    if value["build_progress"]:
        lines += [value["build_progress"]]
    error = value["last_error"]
    lines += ["Last failure: " + ((error["operation"] + " at " + error["at"] + "\n" + error["message"]) if error else "none")]
    lines += ["Profile: " + value["profile"], "Logs: " + value["logs"]]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--watch", action="store_true")
    options, remaining = parser.parse_known_args(argv)
    command = remaining.pop(0) if remaining and remaining[0] in COMMANDS else "launch"
    client = client_config(options.root)
    logs = core.state_root(client) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(logs / (command + ".log"), encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    operation_started = core.utc_now()
    try:
        if command == "status":
            if options.watch and not options.gui:
                try:
                    import time
                    while True:
                        value = collect_status(client)
                        print(json.dumps(value) if options.json else "\n" + status_text(value) + "\nRefreshing every 5 seconds; Ctrl+C to stop.", flush=True)
                        time.sleep(5)
                except KeyboardInterrupt:
                    return 0
            value = collect_status(client)
            output = json.dumps(value, indent=2) if options.json else status_text(value)
        else:
            ensure_installed(client)
            config = effective_config(client)
            if command == "launch":
                core.launch(config, remaining)
                return 0
            elif command == "apply":
                core.install_pending(config)
                cleanup_downloads(client)
                return 0
            elif command == "cleanup":
                result = core.cleanup(config)
                result["downloads"] = cleanup_downloads(client)
                output = json.dumps(result, indent=2)
            elif command == "verify":
                manifest = core.read_json(core.state_root(client) / "browser-manifest.json") or core.read_json(client["install_root"] / "runtime/browser-manifest.json")
                core.verify_manifest(client["install_root"] / "bin", manifest["files"])
                output = "Verified " + str(len(manifest["files"])) + " browser files."
            elif command == "update":
                if client["mode"] == "source":
                    if config is client:
                        raise RuntimeError("The source build drive is offline. The installed browser remains usable.")
                    # Build resources and GN arguments belong to the source
                    # checkout, while the interpreter is bundled with Chromo.
                    builder = Path(client["builder_config"]).parent / "chromo_bro.py"
                    interpreter = Path(sys.executable).with_name("python.exe")
                    core.run([interpreter, builder, "update", "--config", client["builder_config"]], cwd=builder.parent)
                    output = "Source update check finished. Run chromo status for details."
                else:
                    output = update_release(client)
            elif command == "setup":
                core.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          client["install_root"] / "runtime/Setup-Portable.ps1", "-Root", client["install_root"]])
                output = "Chromo is ready. Use the desktop shortcut or run chromo in a new terminal."
        if options.gui:
            ctypes.windll.user32.MessageBoxW(0, output, "Chromo status" if command == "status" else "Chromo", 0x40)
        elif sys.stdout is not None:
            print(output)
        return 0
    except core.OperationInProgress as error:
        core.LOG.info("%s", error)
        return 0
    except Exception as error:
        core.LOG.exception("Chromo operation failed")
        child_error = core.read_json(core.state_root(client) / "last-error.json", {})
        if not (isinstance(error, subprocess.CalledProcessError) and child_error.get("at", "") >= operation_started):
            core.record_failure(client, command, error)
        if options.gui and command not in ("update", "apply"):
            ctypes.windll.user32.MessageBoxW(0, str(error) + "\nRun chromo status for details.", "Chromo", 0x10)
        elif sys.stderr is not None:
            print("Chromo: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
