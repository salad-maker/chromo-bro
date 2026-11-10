from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chromo_bro as app


class ReleaseTests(unittest.TestCase):
    def release(self, version, channel="Stable", platform="Windows"):
        return {"version": version, "channel": channel, "platform": platform,
                "hashes": {"chromium": "a" * 40}}

    def test_selects_newest_windows_stable_numerically(self):
        releases = [self.release("157.0.8103.9"), self.release("157.0.8103.51"),
                    self.release("159.0.1.1", "Dev"), self.release("158.0.1.1", platform="Linux")]
        self.assertEqual(app.select_release(releases)["version"], "157.0.8103.51")

    def test_rejects_invalid_source_commit(self):
        release = self.release("157.0.8103.51")
        release["hashes"]["chromium"] = "refs/heads/main"
        with self.assertRaises(ValueError):
            app.select_release([release])

    def test_rejects_shell_metacharacters_in_version(self):
        with self.assertRaises(ValueError):
            app.version_tuple("157.0.8103.51 & echo unsafe")


class PackageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
