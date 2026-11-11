"""Install a packaged runtime beside an existing, verified source-built browser."""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromo_bro as core
import chromo_client as client


def install(package, config_path):
    config_path = Path(config_path).resolve()
    config = core.load_config(config_path)
    root = config["install_root"]
    marker = core.read_json(root / "chromo-bro-installed.json")
    if not marker:
        raise RuntimeError("Install and verify the first source build before deploying its runtime")
    manifest_path = Path(marker.get("manifest_path") or (Path(marker["build_dir"]) / "manifest.json"))
    manifest = core.read_json(manifest_path)
    core.verify_manifest(root / "bin", manifest["files"])
    cache = config["build_root"] / "package-cache"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-install-", dir=cache) as temporary:
        unpacked = Path(temporary) / "package"
        client.extract_zip(package, unpacked)
        # Browser and profile are deliberately not copied from the ZIP.
        with core.exclusive_lock(core.install_lock(config)):
            runtime = root / "runtime"
            escaped = str(runtime).replace("'", "''")
            check = ("$prefix='" + escaped + "\\'; @(Get-CimInstance Win32_Process | Where-Object { "
                     "$_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase) }).Count")
            if int(core.run(["powershell.exe", "-NoProfile", "-Command", check], capture=True)):
                raise RuntimeError("A Chromo runtime operation is active; close its status window or wait for it to finish")
            staged = root / ("runtime-next-" + uuid.uuid4().hex)
            shutil.copytree(unpacked / "runtime", staged)
            if core.tree_manifest(staged) != core.tree_manifest(unpacked / "runtime"):
                core.safe_remove_tree(staged, root)
                raise RuntimeError("Runtime copy verification failed")
            previous = None
            if runtime.exists():
                if runtime.is_symlink() or getattr(runtime.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("Runtime directory is a reparse point")
                previous = core.state_root(config) / ("runtime-previous-" + uuid.uuid4().hex[:8])
                runtime.rename(previous)
            try:
                staged.rename(runtime)
            except OSError:
                if previous:
                    previous.rename(runtime)
                raise
            for name in ("Chromo.exe", "Setup.exe"):
                temporary_exe = root / (name + ".new")
                shutil.copy2(unpacked / name, temporary_exe)
                temporary_exe.replace(root / name)
            (root / "commands").mkdir(exist_ok=True)
            shutil.copy2(unpacked / "commands/chromo.cmd", root / "commands/chromo.cmd")
            shutil.copytree(unpacked / "LICENSES", root / "LICENSES", dirs_exist_ok=True)
            local = core.state_root(config) / "browser-manifest.json"
            shutil.copy2(manifest_path, local) if manifest_path.resolve() != local.resolve() else None
            shutil.copy2(local, runtime / "browser-manifest.json")
            marker["manifest_path"] = str(local)
            core.write_json(root / "chromo-bro-installed.json", marker)
            core.write_json(runtime / "client.json", {"mode": "source", "repository": client.REPOSITORY,
                            "builder_config": str(config_path), "keep_backups": config.get("keep_backups", 2)})
        python = root / "runtime/python/python.exe"
        core.run([python, runtime / "chromo_client.py", "--root", root, "verify"])
        core.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", runtime / "Setup-Portable.ps1", "-Root", root])
        print("Installed the portable runtime, desktop launcher, status view, command, and tasks.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    install(options.package, options.config)
