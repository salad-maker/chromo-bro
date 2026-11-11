# chromo-bro

Chromium Stable for Windows x64, with a portable launcher, visible update status,
verified updates, and matching browser/profile backups. This is an independent,
unsigned Chromium build, rather than Google Chrome or an official distribution.

## Download and use

Download the **portable ZIP** from [Releases](https://github.com/salad-maker/chromo-bro/releases),
extract the entire ZIP into a writable folder, and double-click **Setup.exe**.
Setup adds the **Chromo** desktop shortcut, a **Chromo Status** Start Menu shortcut,
the terminal command, and current-user update tasks. No administrator account,
separate Python installation, build tools, or source checkout is needed to use it.
The private Python runtime is pinned to the official Python.org archive and SHA-256.

Restart your terminal after setup:

```powershell
chromo
chromo about:blank
chromo status
chromo status --watch
chromo status --json
chromo update
chromo cleanup
chromo verify
```

Status shows the installed version, last successful check, pending update, free
space, saved rollback sets, build progress for source builders, and the last
failure. Failures are saved locally and produce a desktop notification, subject
to Windows notification settings. Identical notices are limited to once per six
hours. `status` and launching work while the build drive is offline.

The browser uses `<installation>\profile`. Keep the whole installation folder
together and close Chromium before moving it. Never replace an existing profile
with a profile from a download. Portable release archives contain no user profile.

## Updates and recovery

Portable users download **published, verified browser builds** from this repo's
GitHub Releases. Source builders can continue compiling upstream Stable locally.
No GitHub credentials are needed to download public releases.

- Check daily at **04:00 local time** and five minutes after login, while logged in.
- Try applying verified updates every 15 minutes, and before browser launch.
- Wait for this Chromium installation to close; never terminate the user's browser.
- Verify GitHub asset SHA-256, package metadata, browser files, and a sandboxed
  headless check with a disposable profile before accepting a browser.
- Verify staging again, back up and hash the old profile, then switch browser files.
- Keep **two valid matching browser/profile rollback sets** by default. After a
  successful install, older unchanged managed sets are removed. Modified backups,
  unknown folders, and reparse points are preserved.
- Remove owned abandoned staging files and completed download caches. Check free
  space for downloading, staging, profile backup, and a 2 GiB system-drive reserve.

The installer journals its file switch. An interrupted swap can finish metadata
for the verified new browser, or restore the previous binary before retrying.
It never automatically rolls a migrated user profile back to an older version.
If a manual rollback is necessary, close Chromium and restore the matching `bin`,
`profile`, `installed.json` metadata, and `browser-manifest.json` from the same
backup set, retaining a copy of the current data first.

Automatic system sleep is inhibited during building and installation; the display
can turn off, manual sleep is still allowed, and prior sleep requests are restored.
Tasks store no password and do not wake the PC. The launcher and installer share
a local lock, including while a source drive is disconnected.

Local state, failure details, and runtime logs live in `<installation>\.chromo`.
`backups\<timestamp>\backup.json` records complete sets and file hashes.
The current browser manifest and install metadata remain local after downloads
are cleaned. Source build logs also remain in `<build_root>\logs\update.log`.

The build enables proprietary codec support. Google API credentials, Google Sync
credentials, and Widevine DRM are not bundled. Chromium and third-party credits
are available at `chrome://credits`; Chromium and Python notices ship in LICENSES.

## Build from upstream source

Requirements for builders:

- Windows 10+ x64, Python 3.9+, Git, and 7-Zip.
- Visual Studio 2026 C++ Build Tools with MFC/ATL and the required Windows SDK.
- An NTFS build drive with at least 100 GiB free and a path without spaces.
- Preferably more than 16 GiB RAM. The first full build can take many hours;
  the initial build on a four-core PC took roughly 18 hours through installation.

See the [upstream Windows build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/windows_build_instructions.md).
Copy `config.example.json` to `local.json` and adjust its paths. Local configuration,
source checkouts, profiles, credentials, binaries, archives, and logs stay outside Git.

```powershell
python chromo_bro.py release
python chromo_bro.py update --config local.json
```

The builder selects the newest active, pinnable Windows x64 Stable release from
[Google VersionHistory](https://developer.chrome.com/docs/web-platform/versionhistory/guide),
matches it to the ChromiumDash Windows Stable source commit, syncs pinned DEPS,
and builds an optimized, unbranded `mini_installer` with `args.gn`.
Limited early-Stable rollouts are excluded. Feed disagreements fail safely.
Existing compiled outputs are reused for subsequent builds.

`package_releases: true` also prepares versioned release artifacts after a verified
build. Packaging failure is reported separately and does not prevent the verified
local browser update. Publication is a separate authenticated publisher action.

## Package and publish

Package an existing verified build without recompiling Chromium:

```powershell
python package_release.py --build-dir F:\chromium-build\builds\VERSION-COMMIT --output F:\chromium-build\dist\VERSION --cache F:\chromium-build\package-cache
```

The packager uses Windows' .NET Framework compiler to create small native GUI
launchers and includes a private embedded Python runtime. It produces:

- `chromo-APPVERSION-chromium-VERSION-win64.zip`: complete portable application.
- `chromo-browser-VERSION-win64.zip`: verified browser payload for automatic updates.
- `release-manifest.json`: browser/source/runtime metadata and asset SHA-256 hashes.
- `SHA256SUMS.txt`: download checksums.

To add that runtime to an existing source-built installation without replacing its
browser or profile:

```powershell
python install_runtime.py --package F:\chromium-build\dist\VERSION\chromo-APPVERSION-chromium-VERSION-win64.zip --config local.json
```

This also updates its shortcuts and source-build tasks to use the bundled runtime.
The builder still needs its configured build drive; normal browser launch and
local status work independently. `Install-Automation.ps1` detects this layout.

Publish the four assets with an authenticated [GitHub CLI](https://cli.github.com/):

```powershell
gh auth login --web
.\Publish-Release.ps1 -Directory F:\chromium-build\dist\VERSION
```

Only explicitly selected browser/runtime artifacts are packaged. Profiles,
backups, local configuration, and diagnostic logs are never included.
Published build records in `build-records/` capture the original source commit,
GN arguments, and archive/binary hashes. Runtime updates can be installed from a
new portable package; automatic update tasks replace the browser payload.

## Development and task control

```powershell
python -m unittest discover -s tests -v
.\Stop-Build.ps1 -Config .\local.json
Start-ScheduledTask 'ChromoBro Stable Build'
```

Tests cover release selection, hash tampering, unsafe ZIPs, low disk space, keeping
two matching backups, preserving modified data, source-drive independence,
notification deduplication, profile preservation, installer locking, and interrupted
file-switch recovery. Actual packages receive sandboxed headless smoke tests.

Stop-Build stops this installation's task and compiler process tree while retaining
its cached outputs and schedule. Upstream toolchain changes may require maintenance.
Scripts do not automatically pull themselves from GitHub.
