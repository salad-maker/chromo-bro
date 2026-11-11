"""Package an already verified browser with a private, pinned Python runtime."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chromo_bro as core
import chromo_client as client

ROOT = Path(__file__).resolve().parent


def make_zip(source, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(Path(source).rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(source).as_posix())


def package(build_dir, output_dir, cache, recipe_commit=None):
    build_dir, output_dir, cache = Path(build_dir).resolve(), Path(output_dir).resolve(), Path(cache).resolve()
    manifest = core.read_json(build_dir / "manifest.json")
    core.version_tuple(manifest["version"])
    if not core.SHA_RE.fullmatch(manifest["source_commit"]):
        raise ValueError("Invalid source commit")
    core.verify_manifest(build_dir / "browser", manifest["files"])
    runtime = core.read_json(ROOT / "python-runtime.json")
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ("python-" + runtime["version"] + "-embedded.zip")
    if not archive.exists() or core.sha256(archive) != runtime["sha256"]:
        client.download(runtime["url"], archive, runtime["sha256"], maximum=100 * 1024**2)
    output_dir.mkdir(parents=True, exist_ok=True)
    version = manifest["version"]
    browser_zip = output_dir / ("chromo-browser-" + version + "-win64.zip")
    portable_zip = output_dir / ("chromo-" + client.APP_VERSION + "-chromium-" + version + "-win64.zip")
    csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not csc.exists():
        raise RuntimeError("The Windows .NET Framework C# compiler is required for packaging")
    with tempfile.TemporaryDirectory(prefix="chromo-package-", dir=cache) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        payload.mkdir()
        shutil.copytree(build_dir / "browser", payload / "browser")
        core.write_json(payload / "manifest.json", manifest)
        shutil.copy2(ROOT / "LICENSE.chromium", payload / "LICENSE.chromium")
        print("Compressing verified browser payload", flush=True)
        make_zip(payload, browser_zip)
        bundle = work / "portable"
        bundle.mkdir()
        shutil.copytree(build_dir / "browser", bundle / "bin")
        app = bundle / "runtime"
        app.mkdir()
        client.extract_zip(archive, app / "python")
        pth_files = list((app / "python").glob("python*._pth"))
        if len(pth_files) != 1:
            raise RuntimeError("Embedded Python does not have its expected isolated path file")
        pth = pth_files[0]
        pth.write_bytes((pth.read_text().rstrip() + "\n..\n").encode("utf-8"))
        for filename in ("chromo_bro.py", "chromo_client.py", "Setup-Portable.ps1"):
            shutil.copy2(ROOT / filename, app / filename)
        core.write_json(app / "client.json", {"mode": "release", "repository": client.REPOSITORY, "keep_backups": 2})
        core.write_json(app / "browser-manifest.json", manifest)
        # Generated from the browser executable, keeping Chromium's existing icon.
        icon = work / "chromo.ico"
        script = ("Add-Type -AssemblyName System.Drawing; $icon=[Drawing.Icon]::ExtractAssociatedIcon('" +
                  str(build_dir / "browser/chrome.exe").replace("'", "''") + "'); "
                  "$stream=[IO.File]::Create('" + str(icon).replace("'", "''") + "'); "
                  "try { $icon.Save($stream) } finally { $stream.Dispose(); $icon.Dispose() }")
        core.run(["powershell.exe", "-NoProfile", "-Command", script], capture=True)
        for name, define in (("Chromo.exe", None), ("Setup.exe", "SETUP")):
            arguments = [csc, "/nologo", "/target:winexe", "/optimize+", "/out:" + str(bundle / name),
                         "/win32manifest:" + str(ROOT / "app.manifest"), "/win32icon:" + str(icon),
                         "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll"]
            if define:
                arguments.append("/define:" + define)
            core.run(arguments + [ROOT / "NativeLauncher.cs"])
        commands = bundle / "commands"
        commands.mkdir()
        (commands / "chromo.cmd").write_bytes(b'@echo off\r\nrem Managed by chromo-bro\r\nsetlocal DisableDelayedExpansion\r\n"%~dp0..\\runtime\\python\\python.exe" "%~dp0..\\runtime\\chromo_client.py" --root "%~dp0.." %*\r\n')
        licenses = bundle / "LICENSES"
        licenses.mkdir()
        shutil.copy2(ROOT / "LICENSE.chromium", licenses / "Chromium.txt")
        shutil.copy2(app / "python/LICENSE.txt", licenses / "Python.txt")
        (bundle / "README.txt").write_text(
            "Chromo " + client.APP_VERSION + " / Chromium " + version + "\n\n"
            "Extract the entire ZIP into a writable folder, then double-click Setup.exe.\n"
            "Use Chromo.exe or the desktop shortcut. In a new terminal: chromo status\n"
            "Updates download verified published builds. Two matching rollback sets are kept.\n"
            "The browser profile lives in this folder's profile directory.\n"
            "Python is included privately; no Python installation or source checkout is required.\n"
            "Unsigned independent Chromium build. Google Sync credentials and Widevine are not included.\n"
            "Chromium and third-party credits: chrome://credits. Python license: runtime/python/LICENSE*.txt.\n",
            encoding="utf-8")
        print("Compressing portable application", flush=True)
        make_zip(bundle, portable_zip)
    result = {"schema": 1, "application_version": client.APP_VERSION, "version": version, "platform": "win64", "channel": "Stable",
              "source_commit": manifest["source_commit"], "recipe_commit": recipe_commit, "build_args": manifest["build_args"], "python_runtime": runtime,
              "browser_unpacked_bytes": sum(path.stat().st_size for path in (build_dir / "browser").rglob("*") if path.is_file()),
              "browser_asset": {"name": browser_zip.name, "sha256": core.sha256(browser_zip), "size": browser_zip.stat().st_size},
              "portable_asset": {"name": portable_zip.name, "sha256": core.sha256(portable_zip), "size": portable_zip.stat().st_size}}
    core.write_json(output_dir / "release-manifest.json", result)
    checksums = []
    for path in (browser_zip, portable_zip, output_dir / "release-manifest.json"):
        checksums.append(core.sha256(path) + "  " + path.name)
    (output_dir / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_dir), "version": version, "assets": [browser_zip.name, portable_zip.name, "release-manifest.json", "SHA256SUMS.txt"]}), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--recipe-commit")
    options = parser.parse_args()
    package(options.build_dir, options.output, options.cache, options.recipe_commit)
