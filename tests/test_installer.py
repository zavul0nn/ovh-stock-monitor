import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import installer as i


def result(text="", code=0):
    return subprocess.CompletedProcess([], code, text, "")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "installation"

    def tearDown(self): self.temp.cleanup()

    def test_claim_empty_directory_and_repeat(self):
        root, meta = i.claim_root(self.root)
        self.assertEqual(i.claim_root(self.root), (root, meta))
        self.assertTrue(meta["project"].startswith("ovh-stock-"))

    def test_refuse_unmanaged_nonempty_directory(self):
        self.root.mkdir()
        (self.root / "important").write_text("keep")
        with self.assertRaises(i.InstallError): i.claim_root(self.root)
        self.assertEqual((self.root / "important").read_text(), "keep")

    def test_invalid_marker_cannot_adopt_project(self):
        root, meta = i.claim_root(self.root)
        meta["project"] = "production"
        (root / ".ovh-stock-install.json").write_text(json.dumps(meta))
        with self.assertRaises(i.InstallError): i.claim_root(root)

    def test_symlink_refused(self):
        target = Path(self.temp.name) / "real"
        target.mkdir()
        link = Path(self.temp.name) / "link"
        try: link.symlink_to(target, target_is_directory=True)
        except OSError: self.skipTest("OS does not allow test symlinks")
        with self.assertRaises(i.InstallError): i.claim_root(link / "nested")

    def test_data_permissions_only_on_known_files(self):
        root, _ = i.claim_root(self.root)
        data = root / "data"; data.mkdir()
        database = data / "monitor.sqlite3"; database.write_text("keep")
        with patch.object(i.os, "chown", create=True) as chown:
            i.prepare_data(root)
        self.assertEqual({call.args[0] for call in chown.call_args_list}, {data, database})
        self.assertEqual(database.read_text(), "keep")

    def test_unknown_data_file_aborts_before_any_chown(self):
        root, _ = i.claim_root(self.root)
        data = root / "data"; data.mkdir()
        (data / "other-service.db").write_text("keep")
        with patch.object(i.os, "chown", create=True) as chown:
            with self.assertRaises(i.InstallError): i.prepare_data(root)
            chown.assert_not_called()

    def test_apt_plan_rejects_upgrades_and_removals(self):
        for plan in ("Remv containerd [1.0]", "Inst docker-ce [1.0] (2.0 Docker)"):
            with self.assertRaises(i.InstallError): i.check_plan(plan)
        i.check_plan("Inst docker-ce (2.0 Docker)\nConf docker-ce (2.0 Docker)")

    def test_unsafe_apt_plan_never_installs(self):
        with patch.object(i, "run", return_value=result("Remv production-package [1.0]")) as run:
            with self.assertRaises(i.InstallError): i.apt_install(["docker-ce"])
        self.assertEqual(run.call_count, 1)

    def test_existing_docker_never_installs_or_restarts(self):
        with patch.object(i.shutil, "which", return_value="/usr/bin/docker"), patch.object(i, "run", return_value=result()) as run, patch.object(i, "apt_install") as apt:
            i.ensure_engine("noble")
        self.assertEqual(run.call_args_list[0].args[0], [*i.DOCKER, "info"])
        self.assertEqual(run.call_count, 1)
        apt.assert_not_called()

    def test_active_broken_docker_is_not_restarted(self):
        with patch.object(i.shutil, "which", return_value="docker"), patch.object(i, "run", side_effect=[result(code=1), result()]) as run:
            with self.assertRaises(i.InstallError): i.ensure_engine("noble")
        self.assertEqual(run.call_count, 2)

    def test_existing_stopped_docker_only_started(self):
        with patch.object(i.shutil, "which", return_value="docker"), patch.object(i, "run", side_effect=[result(code=1), result(code=3), result("loaded\n"), result(), result()]) as run:
            i.ensure_engine("noble")
        self.assertEqual(run.call_args_list[3].args[0], ["systemctl", "start", "docker"])

    def test_containerd_conflict_not_removed(self):
        with patch.object(i.shutil, "which", return_value=None), patch.object(i, "installed", side_effect=lambda name: name == "containerd"), patch.object(i, "apt_install") as apt:
            with self.assertRaises(i.InstallError): i.ensure_engine("noble")
        apt.assert_not_called()

    def test_existing_plugins_not_replaced(self):
        with patch.object(i, "run", return_value=result()), patch.object(i, "download") as download:
            i.install_plugin("compose"); i.install_plugin("buildx")
        download.assert_not_called()

    def test_plugin_checksum_failure_never_writes(self):
        target = Path(self.temp.name) / "plugin"
        bad = b"0" * 64 + b"  docker-compose-linux-x86_64\n"
        with patch.object(i, "run", return_value=result(code=1)), patch.object(i, "no_symlinks", return_value=target), patch.object(i.platform, "machine", return_value="x86_64"), patch.object(i, "download", side_effect=[b'{"tag_name":"v5.5.0"}', bad, b"binary"]):
            with self.assertRaises(i.InstallError): i.install_plugin("compose")
        self.assertFalse(target.exists())

    def test_verified_missing_plugin_written_once(self):
        target = Path(self.temp.name) / "plugin"
        data = b"binary"
        checksum = hashlib.sha256(data).hexdigest().encode() + b"  docker-compose-linux-x86_64\n"
        with patch.object(i, "run", side_effect=[result(code=1), result()]), patch.object(i, "no_symlinks", return_value=target), patch.object(i.platform, "machine", return_value="x86_64"), patch.object(i, "download", side_effect=[b'{"tag_name":"v5.5.0"}', checksum, data]):
            i.install_plugin("compose")
        self.assertEqual(target.read_bytes(), data)

    def test_existing_configuration_preserved(self):
        root, _ = i.claim_root(self.root)
        original = "TELEGRAM_BOT_TOKEN=123:TEST\nTELEGRAM_ADMIN_IDS=10\n"
        (root / ".env").write_text(original)
        different = root / "other.env"
        different.write_text("TELEGRAM_BOT_TOKEN=456:OTHER\nTELEGRAM_ADMIN_IDS=20\n")
        i.configure(root, different)
        self.assertEqual((root / ".env").read_text(), original)

    def test_noninteractive_credentials_import(self):
        root, _ = i.claim_root(self.root)
        source = root / "input.env"
        source.write_text("TELEGRAM_BOT_TOKEN=123:TEST\nTELEGRAM_ADMIN_IDS=10\n")
        i.configure(root, source)
        self.assertEqual(i.env_values(root / ".env")["TELEGRAM_ADMIN_IDS"], "10")

    def test_missing_credentials_noninteractive_is_clear_error(self):
        root, _ = i.claim_root(self.root)
        with patch.object(i.sys.stdin, "isatty", return_value=False):
            with self.assertRaises(i.InstallError): i.configure(root)

    def test_project_collision_refused(self):
        _, meta = i.claim_root(self.root)
        with patch.object(i, "run", side_effect=[result("other-id\n"), result('{}')]):
            with self.assertRaises(i.InstallError): i.guard_project(meta)

    def test_owned_project_accepted(self):
        _, meta = i.claim_root(self.root)
        with patch.object(i, "run", side_effect=[result("our-id\n"), result(json.dumps({i.LABEL: meta["id"]}))]):
            i.guard_project(meta)

    def test_online_backup_preserves_original(self):
        root, _ = i.claim_root(self.root)
        (root / "data").mkdir()
        path = root / "data" / "monitor.sqlite3"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE important(value TEXT)")
        db.execute("INSERT INTO important VALUES ('keep')"); db.commit()
        i.backup_database(root)
        self.assertEqual(db.execute("SELECT value FROM important").fetchone()[0], "keep")
        db.close()
        backup = next((root / "backups").glob("*.sqlite3"))
        other = sqlite3.connect(backup)
        self.assertEqual(other.execute("SELECT value FROM important").fetchone()[0], "keep")
        other.close()

    def test_compose_targets_dedicated_project(self):
        root, meta = i.claim_root(self.root)
        args = i.compose_args(root, meta, root / "releases" / "test")
        self.assertIn(meta["project"], args)
        self.assertIn("--host", args)
        self.assertNotIn("down", args)

    def test_test_failure_leaves_running_container_untouched(self):
        root, meta = i.claim_root(self.root)
        with patch.object(i, "guard_project"), patch.object(i, "backup_database") as backup, patch.object(i, "run", side_effect=[result(), i.InstallError("test failed")]) as run:
            with self.assertRaises(i.InstallError): i.deploy(root, meta, root / "release")
        self.assertEqual(run.call_count, 2)
        backup.assert_not_called()

    def test_deploy_uses_only_scoped_up(self):
        root, meta = i.claim_root(self.root)
        calls = []
        def fake(args, **kwargs):
            calls.append(args)
            if "ps" in args: return result("our-container\n")
            if "inspect" in args: return result("healthy\n")
            return result()
        with patch.object(i, "guard_project"), patch.object(i, "backup_database"), patch.object(i, "run", side_effect=fake):
            i.deploy(root, meta, root / "release")
        up = next(args for args in calls if "up" in args)
        self.assertEqual(up[-4:], ["up", "-d", "--no-deps", "monitor"])
        self.assertTrue((root / "manage.sh").is_file())
        self.assertFalse(any(token in ("down", "prune", "restart", "rm") for args in calls for token in args))

    def test_remote_docker_env_cannot_redirect_installation(self):
        with patch.dict(i.os.environ, {"DOCKER_HOST": "tcp://production:2375", "COMPOSE_PROJECT_NAME": "production", "BUILDX_BUILDER": "production"}):
            env = i.clean_env()
        self.assertNotIn("DOCKER_HOST", env)
        self.assertNotIn("COMPOSE_PROJECT_NAME", env)
        self.assertEqual(env["NEEDRESTART_MODE"], "l")

    def test_fresh_ubuntu_installs_official_packages_without_removals(self):
        apt = Path(self.temp.name) / "apt"
        (apt / "sources.list.d").mkdir(parents=True)
        calls = []
        def fake(args, **kwargs):
            calls.append(args)
            if args == ["dpkg", "--print-architecture"]: return result("amd64\n")
            return result()
        with patch.object(i.shutil, "which", return_value=None), patch.object(i, "installed", return_value=False), patch.object(i, "download", return_value=b"-----BEGIN PGP PUBLIC KEY BLOCK-----\ntest\n"), patch.object(i, "run", side_effect=fake):
            i.ensure_engine("noble", apt)
        self.assertIn("Suites: noble", (apt / "sources.list.d/ovh-stock-docker.sources").read_text())
        installs = [args for args in calls if args[:2] == ["apt-get", "-y"]]
        self.assertTrue(any("docker-ce" in args and "containerd.io" in args for args in installs))
        self.assertTrue(all("--no-remove" in args and "--no-upgrade" in args for args in installs))
        self.assertFalse(any("restart" in args or "remove" in args for args in calls))

    def test_existing_apt_source_never_overwritten(self):
        apt = Path(self.temp.name) / "apt"
        (apt / "sources.list.d").mkdir(parents=True)
        source = apt / "sources.list.d/docker.list"
        text = "deb https://download.docker.com/linux/ubuntu noble stable"
        source.write_text(text)
        with patch.object(i.shutil, "which", return_value=None), patch.object(i, "installed", return_value=False), patch.object(i, "run") as run:
            with self.assertRaises(i.InstallError): i.ensure_engine("noble", apt)
        self.assertEqual(source.read_text(), text)
        run.assert_not_called()

    def test_staged_compose_has_bind_mount_no_ports_and_owner_label(self):
        source = Path(self.temp.name) / "bundle"
        source.mkdir()
        for name in i.PAYLOAD: (source / name).write_text("fixture")
        (source / "tests").mkdir()
        (source / "tests/test_example.py").write_text("pass")
        root, meta = i.claim_root(self.root)
        staged = i.stage_release(source, root, meta, root / "data")
        service = json.loads((staged / "compose.json").read_text())["services"]["monitor"]
        self.assertEqual(service["volumes"][0]["source"], str(root / "data"))
        self.assertFalse(service["volumes"][0]["bind"]["create_host_path"])
        self.assertEqual(service["labels"][i.LABEL], meta["id"])
        self.assertNotIn("ports", service)
        self.assertNotIn("container_name", service)


if __name__ == "__main__": unittest.main()
