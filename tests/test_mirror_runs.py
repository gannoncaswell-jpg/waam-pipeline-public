import copy
import datetime as dt
import io
import json
import logging
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from waam_pipeline import mirror_manifest, runs
import mirror_runs
from test_mirror_manifest import populate

NOW = dt.datetime(2026, 10, 2, 21, 4, tzinfo=dt.timezone.utc)
RUN = "2026-10-02_WALL10_r01"
BUILD = "Test_Operator/Saved builds/widewallv4/request.json"


class TestMirrorValidation(unittest.TestCase):
    def setUp(self):
        self.manifest = {"workspace": "alice", "run": RUN, "sealed_at": NOW.isoformat(),
                         "files": [{"name": RUN + suffix, "sha256": "0" * 64, "bytes": 0}
                                   for suffix in ("_run.json", "_files.csv", "_temperatures.csv", "_README.txt")]}
        self.entry = {"src": "alice/_runs/" + RUN + "/" + RUN + "_run.json",
                      "dest": "alice/Runs/" + RUN + "/" + RUN + "_run.json", "sha256": "0" * 64, "bytes": 0}

    def test_sealed_manifest_names_and_limits(self):
        self.assertEqual(mirror_runs.validate_manifest(self.manifest), self.manifest)
        for field, value in (("name", "../outside.txt"), ("name", RUN + "_01_ATTACH_bad.exe"),
                             ("bytes", runs.MAX_FILE_BYTES + 1), ("bytes", True), ("sha256", "bad")):
            manifest = copy.deepcopy(self.manifest)
            manifest["files"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                mirror_runs.validate_manifest(manifest)
        for field, value in (("workspace", "../alice"), ("run", "../run"), ("run", "2026-99-02_WALL10_r01")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                mirror_runs.validate_manifest({**self.manifest, field: value})

    def test_file_entries_refuse_unsafe_paths(self):
        self.assertEqual(mirror_runs.validate_entry(self.entry), self.entry)
        for field, value in (("src", "../etc/passwd"), ("src", "alice/../../etc/passwd"), ("src", "/abs/path"),
                             ("src", "alice/./x"), ("src", "alice/a b"),
                             ("dest", "../outside.txt"), ("dest", "alice/../../x"), ("dest", "C:/x"),
                             ("dest", "alice\\x"), ("dest", "alice/CON.txt"), ("dest", "alice/nul"),
                             ("dest", "alice/x."), ("dest", "alice/ x"), ("dest", ".hidden/x"),
                             ("dest", "alice/x.conflict-1"), ("dest", "a/" * 7 + "x"), ("dest", "a/" + "x" * 241),
                             ("sha256", "0" * 63), ("bytes", -1), ("bytes", mirror_runs.MAX_FILE_BYTES + 1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                mirror_runs.validate_entry({**self.entry, field: value})

    def test_listing_refuses_duplicates_and_the_readme(self):
        other = {**self.entry, "src": self.entry["src"].replace("run.json", "files.csv")}
        for files in ([self.entry, {**other, "dest": self.entry["dest"].upper()}],
                      [self.entry, {**self.entry, "dest": "alice/other.json"}],
                      [{**self.entry, "dest": "README.txt"}]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                mirror_runs.validate_listing({"version": 1, "files": files})
        with self.assertRaises(ValueError):
            mirror_runs.validate_listing({"version": 2, "files": []})

    def test_remote_configuration_and_state_destination(self):
        base = Path(os.environ.get("TMP") or os.environ.get("TEMP") or "/tmp")
        options = SimpleNamespace(dest=str(base / "mirror-validation-dest"),
                                  state=str(base / "mirror-validation-state" / "state.json"),
                                  remote_repo="/srv/waam/waam-pipeline", remote_python="/srv/waam/venv/bin/python",
                                  ssh="waam@127.0.0.1", port=2222, key="key", dry_run=True)
        for field, value in (("remote_repo", "/srv/waam;whoami"), ("remote_python", "python -c bad"),
                             ("ssh", "-o@host"), ("port", 0), ("state", str(Path(options.dest) / "state.json")),
                             ("state", str(base / "state" / "mirror.log")),
                             ("state", str(base / "state" / "MIRROR.LOG.1")),
                             ("state", str(base / "state" / "mirror.lock")),
                             ("staging", str(Path(options.dest) / "staging"))):
            changed = copy.copy(options)
            setattr(changed, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                mirror_runs.Mirror(changed)
        mirror = mirror_runs.Mirror(options)
        self.assertEqual(mirror._module_command("manifest"),
                         "sudo -u waam sh -c 'cd /srv/waam/waam-pipeline && PYTHONPATH=src exec "
                         "/srv/waam/venv/bin/python -m waam_pipeline.runs manifest'")
        self.assertEqual(mirror._module_command("", "waam_pipeline.mirror_manifest"),
                         "sudo -u waam sh -c 'cd /srv/waam/waam-pipeline && PYTHONPATH=src exec "
                         "/srv/waam/venv/bin/python -m waam_pipeline.mirror_manifest '")


class FakeSSH:


    def __init__(self, saves, dest):
        self.saves, self.dest = saves, dest
        self.calls, self.acks = [], []
        self.listing = None
        self.transform = None

    def __call__(self, argv, *, timeout, stdout=None, stdin=None):
        self.calls.append((argv, timeout))
        command = argv[-1]
        if "waam_pipeline.mirror_manifest" in command:
            listing = self.listing or mirror_manifest.build(self.saves)
            return SimpleNamespace(stdout=json.dumps(listing).encode())
        if " tar " in command:
            names = [name.decode() for name in stdin.split(b"\0") if name]
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                for name in names:
                    body = (self.saves / name).read_bytes()
                    member = tarfile.TarInfo(name)
                    member.size = len(body)
                    if self.transform:
                        member, body = self.transform(member, body)
                    archive.addfile(member, io.BytesIO(body) if body is not None else None)
            stdout.write(output.getvalue())
            return SimpleNamespace(stdout=None)
        if " ack " in command:
            run_id = command.rsplit(" ", 1)[-1].rstrip("'")
            self.acks.append(run_id)
            return SimpleNamespace(stdout=b"{}")
        if "waam_pipeline.runs manifest" in command:
            return SimpleNamespace(stdout=json.dumps(runs.sealed_runs(self.saves)).encode())
        raise AssertionError(command)


class TestMirrorEverything(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.saves = self.root / "saves"
        populate(self.saves)
        self.dest = self.root / "OneDrive"
        self.options = SimpleNamespace(dest=str(self.dest), state=str(self.root / "state" / "state.json"),
                                       remote_repo="/srv/waam/waam-pipeline",
                                       remote_python="/srv/waam/venv/bin/python", ssh="waam@127.0.0.1",
                                       port=2222, key="C:/Users/testuser/.ssh/waam_ed25519", dry_run=False)
        self.fake = FakeSSH(self.saves, self.dest)
        self.log = logging.getLogger(f"mirror-test-{id(self)}")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def execute(self):
        self.fake.calls.clear()
        return mirror_runs.Mirror(self.options, runner=self.fake, log=self.log).run()

    def listing(self):
        return mirror_manifest.build(self.saves)["files"]

    def test_copies_every_file_and_acknowledges_the_sealed_run(self):
        self.assertEqual(self.execute(), 0)
        for entry in self.listing():
            with self.subTest(dest=entry["dest"]):
                self.assertEqual((self.dest / entry["dest"]).read_bytes(), (self.saves / entry["src"]).read_bytes())
        self.assertIn("never deletes", (self.dest / "README.txt").read_text())
        self.assertEqual(self.fake.acks, [RUN])
        mirror_runs.verify_run(self.dest / "Test_Operator" / "Runs" / RUN, runs.sealed_runs(self.saves)[0])
        state = json.loads(Path(self.options.state).read_text())
        self.assertEqual(state["runs"][RUN]["to"], mirror_runs.COPY_LABEL)
        self.assertEqual(len(state["files"]), len(self.listing()) + 1)
        self.assertFalse((self.dest / "state.json").exists())
        self.assertEqual(self.execute(), 0)
        commands = [argv[-1] for argv, _ in self.fake.calls]
        self.assertFalse(any(" tar " in command or " ack " in command for command in commands))

    def test_copies_personal_and_library_archive_metadata_sidecars(self):
        self.assertEqual(self.execute(), 0)
        sidecars = [entry for entry in self.listing()
                    if entry["dest"].endswith(".metadata.json")]
        self.assertTrue(any(entry["src"].startswith("Test_Operator/") for entry in sidecars))
        self.assertTrue(any(entry["src"].startswith("library-") for entry in sidecars))
        for entry in sidecars:
            self.assertEqual((self.dest / entry["dest"]).read_bytes(),
                             (self.saves / entry["src"]).read_bytes())

    def test_layer_log_csv_is_manifested_and_copied_with_the_sealed_run(self):
        workspace = "Test_Operator"
        record = runs.start(self.saves, workspace, operator="o@letu.edu", name="DECAY", now=NOW)
        runs.set_layer_row(self.saves, workspace, {
            "layer": 1, "unit": "C", "pre_weld": 25, "readings": {"20": 200, "40": 150},
            "height_mm": 2.4, "height_source": "sense", "width_mm": 6.5}, now=NOW)
        runs.finish(self.saves, workspace, outcome="completed", waiver=True, by="o@letu.edu", now=NOW)
        name = f"{record['run']}_layer_log.csv"
        source = f"{workspace}/_runs/{record['run']}/{name}"
        destination = f"{workspace}/Runs/{record['run']}/{name}"
        entry = next(item for item in mirror_manifest.build(self.saves)["files"] if item["src"] == source)
        self.assertEqual(entry["dest"], destination)
        self.assertEqual(self.execute(), 0)
        self.assertEqual((self.dest / destination).read_bytes(), (self.saves / source).read_bytes())
        self.assertIn(record["run"], self.fake.acks)

    def test_a_changed_server_file_replaces_the_copy_it_wrote(self):
        self.assertEqual(self.execute(), 0)
        (self.saves / "Test_Operator" / "widewallv4" / "request.json").write_text('{"params": {"travel": 800}}')
        self.assertEqual(self.execute(), 0)
        self.assertEqual((self.dest / BUILD).read_text(), '{"params": {"travel": 800}}')
        self.assertFalse((self.dest / (BUILD + ".conflict-1")).exists())

    def test_a_file_changed_in_onedrive_is_kept_and_the_server_copy_lands_beside_it(self):
        self.assertEqual(self.execute(), 0)
        (self.dest / BUILD).write_text("edited in OneDrive")
        (self.saves / "Test_Operator" / "widewallv4" / "request.json").write_text('{"params": {"travel": 800}}')
        self.assertEqual(self.execute(), 1)
        self.assertEqual((self.dest / BUILD).read_text(), "edited in OneDrive")
        self.assertEqual((self.dest / (BUILD + ".conflict-1")).read_text(), '{"params": {"travel": 800}}')
        self.assertEqual(self.execute(), 1)
        self.assertFalse((self.dest / (BUILD + ".conflict-2")).exists())

    def test_a_conflict_in_a_sealed_run_is_never_acknowledged(self):
        folder = self.dest / "Test_Operator" / "Runs" / RUN
        folder.mkdir(parents=True)
        (folder / f"{RUN}_README.txt").write_text("owner's own notes")
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.fake.acks, [])
        self.assertEqual((folder / f"{RUN}_README.txt").read_text(), "owner's own notes")

    def test_never_deletes_and_restores_what_was_deleted_here(self):
        self.assertEqual(self.execute(), 0)
        (self.saves / "Test_Operator" / "widewallv4" / "part.gcode").unlink()
        (self.dest / BUILD).unlink()
        self.assertEqual(self.execute(), 0)
        self.assertTrue((self.dest / "Test_Operator/Saved builds/widewallv4/part.gcode").exists())
        self.assertEqual((self.dest / BUILD).read_bytes(),
                         (self.saves / "Test_Operator" / "widewallv4" / "request.json").read_bytes())

    def test_tampered_bytes_are_never_placed(self):
        def change(member, body):
            if member.name.endswith("request.json"):
                body = b"x" * len(body)
            return member, body

        self.fake.transform = change
        self.assertEqual(self.execute(), 1)
        self.assertFalse((self.dest / BUILD).exists())
        self.assertNotIn(BUILD, json.loads(Path(self.options.state).read_text())["files"])

    def test_unexpected_traversal_and_link_members_are_refused(self):
        def traversal(member, body):
            member.name = "../escape.txt"
            return member, body

        def link(member, body):
            member.type, member.linkname, member.size = tarfile.SYMTYPE, "../../escape.txt", 0
            return member, None

        for transform in (traversal, link):
            self.fake.transform = transform
            with self.subTest(transform=transform.__name__):
                self.assertEqual(self.execute(), 1)
                self.assertFalse((self.root / "escape.txt").exists())
                self.assertFalse(any(path.is_file() and path.name != "README.txt" for path in self.dest.rglob("*")))

    def test_an_unsafe_listing_writes_nothing(self):
        entry = self.listing()[0]
        self.fake.listing = {"version": 1, "files": [{**entry, "dest": "../outside.txt"}]}
        self.assertEqual(self.execute(), 1)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_staging_stays_outside_the_synced_folder(self):
        staged = []
        fake = self.fake

        def runner(argv, *, timeout, stdout=None, stdin=None):
            if " tar " in argv[-1]:
                staged.append(Path(stdout.name).resolve())
            if stdin is None:
                return fake(argv, timeout=timeout, stdout=stdout)
            return fake(argv, timeout=timeout, stdout=stdout, stdin=stdin)

        self.assertEqual(mirror_runs.Mirror(self.options, runner=runner, log=self.log).run(), 0)
        self.assertTrue(staged)
        self.assertFalse(staged[0].is_relative_to(self.dest.resolve()))

    def test_staging_on_another_drive_copies_instead_of_renaming(self):
        staging = str(self.root / "state" / "staging")
        real = {"rename": os.rename, "replace": os.replace}

        def cross_device(name):
            def move(source, target):
                if str(source).startswith(staging):
                    raise OSError(18, "Invalid cross-device link")
                return real[name](source, target)
            return move

        with mock.patch.object(mirror_runs.os, "rename", cross_device("rename")), \
                mock.patch.object(mirror_runs.os, "replace", cross_device("replace")):
            self.assertEqual(self.execute(), 0)
            (self.saves / "Test_Operator" / "widewallv4" / "request.json").write_text('{"travel": 800}')
            self.assertEqual(self.execute(), 0)
        self.assertEqual((self.dest / BUILD).read_text(), '{"travel": 800}')
        self.assertFalse(any(".incoming" in path.name for path in self.dest.rglob("*")))
        self.assertEqual(self.fake.acks, [RUN])

    def test_dry_run_copies_acknowledges_and_records_nothing(self):
        self.options.dry_run = True
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.fake.acks, [])
        self.assertFalse(any(" tar " in argv[-1] for argv, _ in self.fake.calls))
        self.assertFalse(self.dest.exists())
        self.assertFalse(Path(self.options.state).exists())

    def test_a_running_copy_makes_the_next_one_skip(self):
        state = self.root / "state"
        state.mkdir()
        (state / "mirror.lock").write_text("1")
        argv = ["--dest", str(self.dest), "--state", str(state / "state.json")]
        self.assertEqual(mirror_runs.main(argv, runner=self.fake), 0)
        self.assertEqual(self.fake.calls, [])
        os.utime(state / "mirror.lock", (0, 0))
        self.assertEqual(mirror_runs.main(argv, runner=self.fake), 0)
        self.assertTrue((self.dest / BUILD).exists())
        self.assertFalse((state / "mirror.lock").exists())


if __name__ == "__main__":
    unittest.main()
