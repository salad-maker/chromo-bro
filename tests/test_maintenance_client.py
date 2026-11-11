from pathlib import Path
import io
import json
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import chromo_bro as core
import chromo_client as client
import test_chromo_bro as baseline


class MaintenanceTests(unittest.TestCase):
    def backup(self, root, number):
        folder = root / "backups" / str(number)
        (folder / "bin").mkdir(parents=True)
        (folder / "bin/chrome.exe").write_bytes(b"browser")
        (folder / "profile").mkdir()
        version = "157.0.8103." + str(number)
        (folder / "profile/Last Version").write_text(version)
        core.write_json(folder / "backup.json", {"owner": "chromo-bro", "complete": True,
                        "version": version, "profile_version": version,
                        "completed_at": str(number), "files": core.tree_manifest(folder)})
        return folder

    def test_retains_two_verified_matching_sets_and_preserves_unmanaged_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folders = [self.backup(root, number) for number in (1, 2, 3)]
            unknown = root / "backups/user-copy"
            unknown.mkdir()
            (unknown / "notes.txt").write_text("keep")
            self.assertEqual(core.prune_backups({"install_root": root}), ["1"])
            self.assertFalse(folders[0].exists())
            self.assertTrue(all(folder.exists() for folder in folders[1:]))
            self.assertEqual((unknown / "notes.txt").read_text(), "keep")

    def test_modified_backup_does_not_displace_two_valid_rollback_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folders = [self.backup(root, number) for number in (1, 2, 3)]
            (folders[2] / "notes.txt").write_text("user addition")
            self.assertEqual(core.prune_backups({"install_root": root}), [])
            self.assertTrue(all(folder.exists() for folder in folders))

    def test_cleans_only_owned_stages_and_rejects_ledger_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"install_root": root, "build_root": root / "downloads"}
            owned = root / ("bin-next-" + "a" * 32)
            owned.mkdir()
            (owned / "chrome.exe").write_bytes(b"partial copy")
            unknown = root / ("bin-next-" + "b" * 32)
            unknown.mkdir()
            (root / "profile").mkdir()
            (root / "profile/keep.txt").write_text("keep")
            ledger = core.state_root(config) / "stages"
            core.write_json(ledger / "owned.json", {"owner": "chromo-bro", "name": owned.name})
            core.write_json(ledger / "bad.json", {"owner": "chromo-bro", "name": "../profile"})
            core.cleanup_stages(config)
            self.assertFalse(owned.exists())
            self.assertTrue(unknown.exists())
            self.assertEqual((root / "profile/keep.txt").read_text(), "keep")

    def test_cleanup_cannot_delete_an_outside_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "owned").mkdir()
            (root / "private").mkdir()
            with self.assertRaises(ValueError):
                core.safe_remove_tree(root / "private", root / "owned")
            self.assertTrue((root / "private").exists())

    def interrupted(self, root, swapped):
        config = {"install_root": root / "installed", "build_root": root / "downloads"}
        install = config["install_root"]
        backup = install / "backups/20261110-010000-abcdef"
        (backup / "bin").mkdir(parents=True)
        (backup / "bin/chrome.exe").write_bytes(b"old browser")
        (backup / "profile").mkdir()
        (backup / "profile/Last Version").write_text("157.0.8103.39")
        (install / "profile").mkdir()
        (install / "profile/Last Version").write_text("157.0.8103.39")
        stage = install / ("bin-next-" + "c" * 32)
        target = install / "bin" if swapped else stage
        target.mkdir()
        (target / "chrome.exe").write_bytes(b"new browser")
        manifest = {"version": "157.0.8103.51", "source_commit": "a" * 40,
                    "files": core.make_manifest(target)}
        pending = {"version": manifest["version"], "source_commit": manifest["source_commit"],
                   "manifest_sha256": "b" * 64, "build_dir": str(root / "downloads/builds/new")}
        core.write_json(config["build_root"] / "pending.json", pending)
        core.write_json(core.state_root(config) / "stages/job.json", {
            "owner": "chromo-bro", "name": stage.name, "backup": backup.name,
            "backup_verified": True, "old": {"version": "157.0.8103.39"}, "manifest": manifest,
            "pending": pending, "pending_path": str(config["build_root"] / "pending.json")})
        return config, stage, backup

    def test_recovers_interruption_before_new_browser_was_swapped(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, stage, backup = self.interrupted(Path(temporary), False)
            with patch.object(core, "browser_running", return_value=False):
                core.cleanup_stages(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"old browser")
            self.assertFalse(stage.exists())
            self.assertEqual((config["install_root"] / "profile/Last Version").read_text(), "157.0.8103.39")

    def test_finishes_interrupted_swap_without_rolling_back_a_migrated_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, stage, backup = self.interrupted(Path(temporary), True)
            (config["install_root"] / "profile/Last Version").write_text("157.0.8103.51")
            with patch.object(core, "browser_running", return_value=False):
                core.cleanup_stages(config)
            installed = core.read_json(config["install_root"] / "chromo-bro-installed.json")
            self.assertEqual(installed["version"], "157.0.8103.51")
            self.assertTrue(installed["recovered"])
            self.assertEqual((config["install_root"] / "profile/Last Version").read_text(), "157.0.8103.51")
            self.assertEqual((backup / "profile/Last Version").read_text(), "157.0.8103.39")
            self.assertFalse((config["build_root"] / "pending.json").exists())

    def test_recovery_refuses_to_downgrade_a_newer_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, stage, backup = self.interrupted(Path(temporary), False)
            (config["install_root"] / "profile/Last Version").write_text("158.0.1.1")
            with patch.object(core, "browser_running", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "newer"):
                    core.cleanup_stages(config)
            self.assertTrue(stage.exists())
            self.assertTrue((backup / "bin/chrome.exe").exists())

    def test_metadata_write_failure_restores_browser_and_leaves_profile_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = baseline.PackageTests().prepare_install(root)
            original_write = core.write_json
            def fail_installed(path, value):
                if Path(path).name == "chromo-bro-installed.json" and value.get("version") == "157.0.8103.51":
                    raise OSError("simulated metadata failure")
                return original_write(path, value)
            with patch.object(core, "browser_running", return_value=False), patch.object(core, "smoke_test"), patch.object(core, "write_json", fail_installed):
                with self.assertRaises(OSError):
                    core.install_pending(config)
            self.assertEqual((config["install_root"] / "bin/chrome.exe").read_bytes(), b"previous")
            self.assertEqual((config["install_root"] / "profile/keep.txt").read_text(), "my profile")
            self.assertTrue((root / "pending.json").exists())
            self.assertFalse(list(config["install_root"].glob("bin-next-*")))

    def test_failure_notifications_are_deduplicated_and_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Chromo.exe").write_bytes(b"launcher")
            config = {"install_root": root}
            with patch.object(core.subprocess, "Popen") as notify:
                core.record_failure(config, "update", RuntimeError("network unavailable"))
                core.record_failure(config, "update", RuntimeError("network unavailable"))
            self.assertEqual(notify.call_count, 1)
            self.assertEqual(core.read_json(core.state_root(config) / "last-error.json")["message"], "network unavailable")


class ClientTests(unittest.TestCase):
    def test_source_updates_use_bundled_interpreter_with_builder_checkout_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = root / "source/local.json"
            core.write_json(builder, {"install_root": str(root), "build_root": str(root / "source/build")})
            core.write_json(root / "runtime/client.json", {"mode": "source", "builder_config": str(builder)})
            core.write_json(root / "chromo-bro-installed.json", {"version": "157.0.8103.51"})
            with patch.object(core, "run") as run, patch.object(client.logging, "FileHandler"), patch.object(client.logging, "basicConfig"), patch("builtins.print"):
                self.assertEqual(client.main(["--root", str(root), "update"]), 0)
            arguments = run.call_args.args[0]
            self.assertEqual(Path(arguments[1]), builder.parent / "chromo_bro.py")
            self.assertEqual(Path(arguments[0]).name.lower(), "python.exe")
            self.assertEqual(arguments[-1], str(builder))

    def test_offline_builder_does_not_make_status_depend_on_source_drive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core.write_json(root / "runtime/client.json", {"mode": "source", "builder_config": str(root / "offline/local.json")})
            core.write_json(root / "chromo-bro-installed.json", {"version": "157.0.8103.51"})
            config = client.client_config(root)
            value = client.collect_status(config)
            self.assertFalse(value["builder_available"])
            self.assertEqual(value["browser_version"], "157.0.8103.51")
            self.assertIn("browser can still launch", client.status_text(value))

    def test_only_configured_github_repository_assets_are_allowed(self):
        client.validate_asset_url("https://github.com/salad-maker/chromo-bro/releases/download/v1/browser.zip", client.REPOSITORY)
        for url in ("http://github.com/salad-maker/chromo-bro/releases/download/v1/a.zip", "https://example.com/a.zip", "https://github.com/other/repo/releases/download/v1/a.zip"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                client.validate_asset_url(url, client.REPOSITORY)

    def test_unsafe_zip_paths_and_symlinks_fail_before_extraction(self):
        names = ["../escape.txt", "/absolute.txt", "C:/escape.txt", "NUL", "browser/a."]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, name in enumerate(names):
                archive = root / (str(index) + ".zip")
                with zipfile.ZipFile(archive, "w") as output:
                    output.writestr(name, b"bad")
                destination = root / ("out" + str(index))
                with self.subTest(name=name), self.assertRaises(ValueError):
                    client.extract_zip(archive, destination)
                self.assertFalse(destination.exists())
            archive = root / "link.zip"
            with zipfile.ZipFile(archive, "w") as output:
                entry = zipfile.ZipInfo("browser/link")
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                output.writestr(entry, "../../private")
            with self.assertRaises(ValueError):
                client.extract_zip(archive, root / "link-out")
            archive = root / "backslash.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("browser/escape", b"bad")
            archive.write_bytes(archive.read_bytes().replace(b"browser/escape", b"browser\\escape"))
            with self.assertRaises(ValueError):
                client.extract_zip(archive, root / "backslash-out")

    def test_case_colliding_zip_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "duplicate.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("browser/chrome.exe", b"one")
                output.writestr("browser/CHROME.EXE", b"two")
            with self.assertRaises(ValueError):
                client.extract_zip(archive, root / "out")

    def test_tampered_download_cannot_replace_existing_file(self):
        class Response(io.BytesIO):
            def geturl(self):
                return "https://github.com/salad-maker/chromo-bro/releases/download/v1/a.zip"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "package.zip"
            destination.write_bytes(b"existing good package")
            with patch.object(client.urllib.request, "urlopen", return_value=Response(b"tampered")):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    client.download("https://github.com/test", destination, "a" * 64)
            self.assertEqual(destination.read_bytes(), b"existing good package")
            self.assertFalse(list(root.glob("*.part-*")))

    def test_low_disk_space_is_rejected_before_browser_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = client.client_config(root)
            release = {"version": "157.0.8103.51", "browser_unpacked_bytes": 1000}
            asset = {"size": 1000}
            usage = core.shutil.disk_usage(root)._replace(free=0)
            with patch.object(client, "latest_published", return_value=(release, asset)), patch.object(client.shutil, "disk_usage", return_value=usage), patch.object(client, "download") as download:
                with self.assertRaisesRegex(RuntimeError, "reserve"):
                    client.update_release(config)
            download.assert_not_called()

    def test_cleanup_preserves_pending_modified_and_unmanaged_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = client.client_config(root)
            packages = []
            for name in ("old", "pending", "modified", "unmanaged"):
                directory = config["build_root"] / "builds" / name
                directory.mkdir(parents=True)
                (directory / "manifest.json").write_text("metadata")
                if name != "unmanaged":
                    core.write_json(directory / ".download.json", {"owner": "chromo-bro", "files": core.tree_manifest(directory)})
                packages.append(directory)
            (packages[2] / "backup.json").write_text("user-added file")
            core.write_json(config["build_root"] / "pending.json", {"build_dir": str(packages[1])})
            self.assertEqual(client.cleanup_downloads(config), ["old"])
            self.assertFalse(packages[0].exists())
            self.assertTrue(all(directory.exists() for directory in packages[1:]))

    def test_zip_unpacked_limit_is_checked_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("browser/chrome.exe", b"a" * 100)
            with self.assertRaisesRegex(ValueError, "too large"):
                client.extract_zip(archive, root / "out", maximum_unpacked=50)
            self.assertFalse((root / "out").exists())

    def test_release_manifest_must_match_github_asset_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = client.client_config(root)
            release = {"assets": [
                {"name": "release-manifest.json", "digest": "sha256:" + "a" * 64,
                 "browser_download_url": "https://github.com/" + client.REPOSITORY + "/releases/download/v1/release-manifest.json"},
                {"name": "browser.zip", "digest": "sha256:" + "b" * 64, "size": 100,
                 "browser_download_url": "https://github.com/" + client.REPOSITORY + "/releases/download/v1/browser.zip"}]}
            manifest = {"schema": 1, "platform": "win64", "channel": "Stable", "version": "157.0.8103.51",
                        "source_commit": "a" * 40, "browser_asset": {"name": "browser.zip", "sha256": "c" * 64, "size": 100}}
            def fetch_manifest(url, destination, digest, **kwargs):
                core.write_json(destination, manifest)
            with patch.object(core, "fetch_json", return_value=release), patch.object(client, "download", side_effect=fetch_manifest):
                with self.assertRaisesRegex(ValueError, "disagree"):
                    client.latest_published(config)


if __name__ == "__main__":
    unittest.main()
