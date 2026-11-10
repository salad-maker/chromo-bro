# chromo-bro

Build the latest **Windows x64 Chromium Stable** from Google's upstream source,
verify it, and update a local portable installation while reusing its profile.
This is an independent personal build setup, not Google Chrome or an official
Chromium distribution.

## Update process

1. Read Google's [VersionHistory API](https://developer.chrome.com/docs/web-platform/versionhistory/guide)
   and select the newest active, pinnable Windows x64 Stable release. Limited
   early-Stable experiments are excluded.
2. Match it to the [Windows Stable source feed](https://chromiumdash.appspot.com/fetch_releases?channel=Stable&platform=Windows&num=100)
   and pin its exact Chromium commit and dependency revisions. Mismatched feeds
   fail instead of building an unrelated source revision.
3. Build an optimized, unbranded `mini_installer` with the committed `args.gn`.
4. Extract the portable browser, smoke-test it with a disposable profile, verify
   its version, and record SHA-256 hashes of the archive and packaged files.
5. Wait until this Chromium installation is closed. Back up the existing browser
   and profile, verify the staged files again, and switch the `bin` directory.
6. Launch using the existing `profile` directory.

Failed builds or verification leave the current browser in place. The installer
never terminates the user's browser. A binary rollback must use its matching
profile backup because newer versions can migrate profile data. Build artifacts
and backups are retained; review their disk usage periodically.

## Requirements

- Windows 10+, x64; Python 3.9+ and Git for Windows on PATH.
- Visual Studio 2026 C++ Build Tools with MFC/ATL and the required Windows SDK.
- 7-Zip, an NTFS build drive with at least 100 GB free, and preferably more than
  16 GB RAM. Full builds can run for many hours on older PCs.
- A build path without spaces. Caches and source stay on the build drive.

See the [upstream build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/windows_build_instructions.md).
The GN arguments enable proprietary codec support but do not bundle Google API
keys, Google Sync credentials, or Widevine DRM. A self-build does not reproduce
every integration in third-party distributions.

## Local setup

Copy `config.example.json` to **`local.json`** and adjust the paths. The local
configuration is ignored by Git. Source checkouts, profiles, credentials, logs,
and compiled binaries must remain outside this repository. Chromium's large
source repository is fetched separately, rather than copied into this one.

```powershell
python chromo_bro.py release
python chromo_bro.py update --config local.json
python chromo_bro.py status --config local.json
python chromo_bro.py launch --config local.json
```

The first update bootstraps `depot_tools`, syncs the pinned source, compiles and
verifies it, and installs if the browser is closed. Future checks skip compilation
when the latest Stable version is already installed or staged. Compilation
defaults to three concurrent jobs to limit memory pressure on a 16 GB PC.

## Automatic updates

After verifying the first build:

```powershell
.\Install-Automation.ps1 -Config .\local.json -Python C:\Python39\python.exe
```

This creates two current-user, limited-privilege Windows tasks:

- **ChromoBro Stable Build:** check/build daily at 04:00 local time and five
  minutes after login, catching up after missed runs and avoiding overlapping runs.
- **ChromoBro Apply Pending Build:** attempt installation every 15 minutes,
  deferring whenever this Chromium is running.

Tasks run while the account is logged in, store no password, and do not wake the
PC. While a build is active, automatic system sleep is temporarily inhibited;
the display can still turn off and the normal idle policy resumes afterward.
The **Chromium (chromo-bro)** desktop launcher also applies a pending build
before opening the browser. On the first self-build installation, any existing
chrlauncher update checks are disabled so Hibbiki cannot overwrite this build.

Build output and errors go to `<build_root>\logs\update.log`. Machine-readable
state is in `status.json`, `pending.json`, and `chromo-bro-installed.json`.
Each verified build has a `manifest.json` recording its exact upstream commit,
GN arguments, archive hash, and all packaged file hashes.

Remove the tasks without uninstalling the browser:

```powershell
Unregister-ScheduledTask 'ChromoBro Stable Build' -Confirm:$false
Unregister-ScheduledTask 'ChromoBro Apply Pending Build' -Confirm:$false
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

Tests cover release selection, invalid feed values, file tampering, directory
traversal, waiting for a running browser, profile preservation, and restoring
the old browser after a failed directory switch. Actual packages additionally
receive headless smoke tests before acceptance and again before installation.

## Scope

Builds and updates run locally. This does not upload profiles or binaries, use
paid cloud runners, or automatically pull changes to these scripts from GitHub.
Each new release requires a successful compile; upstream toolchain changes may
need maintenance. Trust rests on the upstream source/toolchain and local PC.
