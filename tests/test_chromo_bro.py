from pathlib import Path
import contextlib
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chromo_bro as app


def launch_with_marker(config, attempted, launched):
    app.install_pending = lambda _: False
    app.subprocess.Popen = lambda *args, **kwargs: Path(launched).write_text("launched")
    Path(attempted).write_text("attempted")
    app.launch(config, [])


class ReleaseTests(unittest.TestCase):
    def release(self, version, channel="Stable", platform="Windows"):
        return {"version": version, "channel": channel, "platform": platform,
                "hashes": {"chromium": "a" * 40}}

    def test_matches_selected_version_to_windows_stable_source_commit(self):
        releases = [self.release("157.0.8103.9"), self.release("157.0.8103.51"),
                    self.release("159.0.1.1", "Dev"), self.release("158.0.1.1", platform="Linux")]
        self.assertEqual(app.select_release(releases, "157.0.8103.51")["version"], "157.0.8103.51")

    def test_excludes_limited_early_stable_even_with_higher_major_version(self):
        releases = [{"version": "158.0.8127.18", "fraction": 0.005, "pinnable": False},
                    {"version": "157.0.8103.51", "fraction": 1, "pinnable": True},
                    {"version": "157.0.8103.39", "fraction": 0.495, "pinnable": False}]
        self.assertEqual(app.select_stable_version(releases), "157.0.8103.51")

    def test_missing_regular_stable_release_fails_without_guessing(self):
        with self.assertRaises(RuntimeError):
            app.select_stable_version([{"version": "158.0.8127.18", "fraction": 0.005, "pinnable": False}])

    def test_mismatched_feeds_fail_without_building_an_unrelated_commit(self):
        with self.assertRaises(RuntimeError):
            app.select_release([self.release("158.0.8127.18")], "157.0.8103.51")

    def test_rejects_invalid_source_commit(self):
        release = self.release("157.0.8103.51")
        release["hashes"]["chromium"] = "refs/heads/main"
        with self.assertRaises(ValueError):
            app.select_release([release], "157.0.8103.51")

    def test_rejects_shell_metacharacters_in_version(self):
        with self.assertRaises(ValueError):
            app.version_tuple("157.0.8103.51 & echo unsafe")


class PackageTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows sleep inhibition")
    def test_nested_sleep_requests_restore_the_enclosing_request(self):
        state = [0x80000000]
        def set_state(value):
            previous = state[0]
            state[0] = value
            return previous
        with patch("ctypes.windll.kernel32.SetThreadExecutionState", side_effect=set_state):
            with app.stay_awake():
                self.assertEqual(state[0], 0x80000001)
                with app.stay_awake():
                    self.assertEqual(state[0], 0x80000001)
                self.assertEqual(state[0], 0x80000001)
            self.assertEqual(state[0], 0x80000000)

    def test_no_pending_install_does_not_inhibit_sleep(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(app, "stay_awake") as awake:
                self.assertFalse(app.install_pending({"build_root": root, "install_root": root / "installed"}))
            awake.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows package permissions")
    def test_sandbox_permissions_cover_nested_files_without_changing_siblings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser = root / "browser"
            (browser / "nested").mkdir(parents=True)
            (browser / "chrome.exe").write_bytes(b"browser")
            resource = browser / "nested/resource.pak"
            resource.write_bytes(b"resource")
            private = root / "private.txt"
            private.write_text("outside the browser package")
            def powershell(script):
                return app.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-Command", script], capture=True, timeout=30)
            private_literal = str(private).replace("'", "''")
            sibling_acl = ("([IO.File]::GetAccessControl('" + private_literal + "'))"
                           ".GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access)")
            before = powershell(sibling_acl)
            app.prepare_browser_permissions(browser)
            paths = ",".join("'" + str(path).replace("'", "''") + "'"
                             for path in (browser / "chrome.exe", resource))
            script = ("$ErrorActionPreference='Stop'; "
                      "$rx=[int][Security.AccessControl.FileSystemRights]::ReadAndExecute; "
                      "$write=[int][Security.AccessControl.FileSystemRights]::Write; "
                      "foreach ($path in @(" + paths + ")) { "
                      "foreach ($sid in @('S-1-15-2-1','S-1-15-2-2')) { "
                      "$rights=0; $security=[IO.File]::GetAccessControl($path); "
                      "foreach ($ace in $security.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) { "
                      "if ($ace.IdentityReference.Value "
                      "-eq $sid -and $ace.AccessControlType -eq 'Allow') { "
                      "$rights=$rights -bor [int]$ace.FileSystemRights } }; "
                      "if (($rights -band $rx) -ne $rx -or ($rights -band $write) -ne 0) { "
                      "throw 'AppContainer must receive read/execute without write access' } } }; "
                      + sibling_acl)
            self.assertEqual(powershell(script), before)

    def test_does_not_sync_source_while_orphaned_compiler_uses_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "checkout/src"
            source.mkdir(parents=True)
            marker = source / "existing-source.txt"
            marker.write_text("source used by the existing compiler")
            config = {"build_root": root}
            release = {"version": "157.0.8103.51", "hashes": {"chromium": "a" * 40}}
            with patch.object(app, "compiler_worker_running", return_value=True):
                with self.assertRaises(app.OperationInProgress):
                    app.build(config, release)
            self.assertEqual(marker.read_text(), "source used by the existing compiler")

    def test_launcher_waits_for_installer_before_creating_browser_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "installed/bin").mkdir(parents=True)
            (root / "installed/bin/chrome.exe").write_bytes(b"browser")
            config = {"build_root": root, "install_root": root / "installed"}
            attempted, launched = root / "attempted", root / "launched"
            worker = multiprocessing.Process(target=launch_with_marker,
                                             args=(config, str(attempted), str(launched)))
            try:
                with app.exclusive_lock(root / "locks/install.lock"):
                    worker.start()
                    deadline = time.monotonic() + 10
                    while not attempted.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(attempted.exists())
                    time.sleep(0.2)
                    self.assertFalse(launched.exists())
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
                self.assertTrue(launched.exists())
            finally:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()

    def test_detects_corrupt_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = Path(temporary)
            executable = browser / "chrome.exe"
            executable.write_bytes(b"original browser")
            manifest = app.make_manifest(browser)
            app.verify_manifest(browser, manifest)
            executable.write_bytes(b"tampered browser")
            with self.assertRaises(RuntimeError):
                app.verify_manifest(browser, manifest)

    def test_rejects_manifest_directory_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = Path(temporary) / "browser"
            browser.mkdir()
            (browser / "chrome.exe").write_bytes(b"browser")
            manifest = app.make_manifest(browser)
            manifest["../private.txt"] = "a" * 64
            with self.assertRaises(ValueError):
                app.verify_manifest(browser, manifest)

    def test_defers_install_when_browser_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "installed"
            (install / "profile").mkdir(parents=True)
            marker = install / "profile/keep.txt"
            marker.write_text("my profile")
            app.write_json(root / "pending.json", {"version": "157.0.8103.51"})
            config = {"build_root": root, "install_root": install}
            with patch.object(app, "browser_running", return_value=True):
                self.assertFalse(app.install_pending(config))
            self.assertEqual(marker.read_text(), "my profile")
            self.assertTrue((root / "pending.json").exists())

    def test_rejects_pending_build_outside_owned_build_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app.write_json(root / "pending.json", {"build_dir": str(root / "outside")})
            config = {"build_root": root, "install_root": root / "installed"}
            with patch.object(app, "browser_running", return_value=False):
                with self.assertRaises(ValueError):
                    app.install_pending(config)

    def prepare_install(self, root):
        install = root / "installed"
        (install / "bin").mkdir(parents=True)
        (install / "bin/chrome.exe").write_bytes(b"previous")
        (install / "profile").mkdir()
        (install / "profile/keep.txt").write_text("my profile")
        package = root / "builds/new"
        (package / "browser").mkdir(parents=True)
        (package / "browser/chrome.exe").write_bytes(b"new browser")
        manifest = {"version": "157.0.8103.51", "source_commit": "a" * 40,
                    "files": app.make_manifest(package / "browser")}
        app.write_json(package / "manifest.json", manifest)
        app.write_json(root / "pending.json", {
            "build_dir": str(package), "version": manifest["version"],
            "source_commit": manifest["source_commit"],
            "manifest_sha256": app.sha256(package / "manifest.json")})
        return {"build_root": root, "install_root": install}

    def test_install_preserves_and_backs_up_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            install = config["install_root"]
            with patch.object(app, "browser_running", return_value=False), patch.object(app, "smoke_test"):
                self.assertTrue(app.install_pending(config))
            self.assertEqual((install / "profile/keep.txt").read_text(), "my profile")
            self.assertEqual((install / "bin/chrome.exe").read_bytes(), b"new browser")
            backup = Path(app.read_json(install / "chromo-bro-installed.json")["backup"])
            self.assertEqual((backup / "profile/keep.txt").read_text(), "my profile")
            self.assertEqual((backup / "bin/chrome.exe").read_bytes(), b"previous")
            self.assertFalse((root / "pending.json").exists())

    def test_install_keeps_pc_awake_through_copy_backup_and_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            active = [False]
            @contextlib.contextmanager
            def awake():
                active[0] = True
                try:
                    yield
                finally:
                    active[0] = False
            original_copy = app.shutil.copytree
            original_rename = Path.rename
            def copy(*args, **kwargs):
                self.assertTrue(active[0])
                return original_copy(*args, **kwargs)
            def rename(path, target):
                self.assertTrue(active[0])
                return original_rename(path, target)
            with patch.object(app, "stay_awake", awake), patch.object(app, "browser_running", return_value=False), \
                    patch.object(app, "smoke_test"), patch.object(app.shutil, "copytree", copy), \
                    patch.object(Path, "rename", rename):
                self.assertTrue(app.install_pending(config))
            self.assertFalse(active[0])

    def test_permission_failure_preserves_existing_browser_and_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            with patch.object(app, "browser_running", return_value=False), \
                    patch.object(app, "prepare_browser_permissions", side_effect=PermissionError("ACL rejected")):
                with self.assertRaises(PermissionError):
                    app.install_pending(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"previous")
            self.assertEqual((config["install_root"] / "profile/keep.txt").read_text(), "my profile")
            self.assertTrue((root / "pending.json").exists())
            self.assertFalse((config["install_root"] / "chromo-bro-installed.json").exists())

    def test_insufficient_install_space_fails_before_staging_or_profile_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            usage = app.shutil.disk_usage(root)._replace(free=0)
            with patch.object(app, "browser_running", return_value=False), \
                    patch.object(app.shutil, "disk_usage", return_value=usage):
                with self.assertRaisesRegex(RuntimeError, "2 GB reserve"):
                    app.install_pending(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"previous")
            self.assertEqual((config["install_root"] / "profile/keep.txt").read_text(), "my profile")
            self.assertFalse(list(config["install_root"].glob("bin-next-*")))
            self.assertFalse((config["install_root"] / "backups").exists())
            self.assertTrue((root / "pending.json").exists())

    def test_restores_previous_browser_if_directory_switch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            original_rename = Path.rename
            def fail_stage(path, target):
                if path.name.startswith("bin-next-"):
                    raise PermissionError("simulated locked directory")
                return original_rename(path, target)
            with patch.object(app, "browser_running", return_value=False), patch.object(app, "smoke_test"), \
                    patch.object(Path, "rename", fail_stage):
                with self.assertRaises(PermissionError):
                    app.install_pending(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"previous")
            self.assertTrue((root / "pending.json").exists())

    def test_does_not_downgrade_profile_from_newer_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            (config["install_root"] / "profile/Last Version").write_text("158.0.8127.19")
            with patch.object(app, "browser_running", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "downgrade"):
                    app.install_pending(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"previous")

    def test_recovers_pending_marker_after_successful_install_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.prepare_install(root)
            pending = app.read_json(root / "pending.json")
            with patch.object(app, "browser_running", return_value=False), patch.object(app, "smoke_test"):
                app.install_pending(config)
                app.write_json(root / "pending.json", pending)
                self.assertFalse(app.install_pending(config))
            self.assertFalse((root / "pending.json").exists())


if __name__ == "__main__":
    unittest.main()
