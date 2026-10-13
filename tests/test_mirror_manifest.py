import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waam_pipeline import job_archive, mirror_manifest, runs, stl_library

NOW = dt.datetime(2026, 10, 2, 21, 4, tzinfo=dt.timezone.utc)
STAMP = "2026-10-03T06:45:30+00:00"
LOCAL = dt.datetime.fromisoformat(STAMP).astimezone(mirror_manifest._zone())
DAY, CLOCK = f"{LOCAL:%Y-%m-%d}", f"{LOCAL:%H%M%S}"


def stl_bytes() -> bytes:
    return b"\0" * 80 + struct.pack("<I", 1) + struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)


def archive(saves, workspace, kind, body, name, **kwargs):
    path = job_archive.preserve(saves, workspace, kind, body, name=name, **kwargs)
    meta = path.parent / "metadata.json"
    record = json.loads(meta.read_text())
    record["created_at"] = STAMP
    meta.write_text(json.dumps(record))
    return path


def populate(saves: Path) -> None:
    archive(saves, "Test_Operator", "jbi", b"/JOB\r\n//NAME WALL\r\n", "WALL.JBI")
    archive(saves, "Test_Operator", "gcode", b"G1 X1\n", "wall")
    archive(saves, "Test_Operator", "vardat", b"///INT\r\n", "layer-touch")
    archive(saves, "sampleuser_at_letu_edu_fd6ee2", "jbi", b"/JOB\r\n//NAME BEN\r\n", "../../evil name?.JBI")
    archive(saves, "guest-123", "jbi", b"/JOB\r\n", "GUEST")
    stl_library.preserve(saves, "test_operator@letu.edu", stl_bytes(), name="cube.stl")
    meta = (saves / stl_library.workspace_for("test_operator@letu.edu") / ".artifacts" / "stl"
            / hashlib.sha256(stl_bytes()).hexdigest() / "metadata.json")
    record = json.loads(meta.read_text())
    record["created_at"] = STAMP
    meta.write_text(json.dumps(record))
    build = saves / "Test_Operator" / "widewallv4"
    build.mkdir(parents=True)
    (build / "request.json").write_text('{"params": {"travel": 133}}')
    (build / "part.gcode").write_text("G1 X1\n")
    runs.start(saves, "Test_Operator", operator="o@letu.edu", name="WALL10", part_hint="WALL10", now=NOW)
    runs.add_file(saves, "Test_Operator", kind="ATTACH", body=b"notes\n", detail="notes.txt", ext=".txt",
                  source="operator", operator="o@letu.edu", now=NOW)
    runs.finish(saves, "Test_Operator", outcome="completed", data_class="preliminary",
                waiver={"reason": "not set up"}, by="o@letu.edu", now=NOW)
    runs.start(saves, "sampleuser_at_letu_edu_fd6ee2", operator="b@letu.edu", name="OPEN", part_hint="OPEN",
               now=NOW)
    junk = saves / "Test_Operator"
    (junk / "secrets.txt").write_text("never listed")
    (junk / ".artifacts" / "jbi" / "notahash").mkdir()
    (junk / ".artifacts" / "jbi" / "notahash" / "job.JBI").write_text("x")
    run = next((junk / "_runs").glob("2026-*"))
    (run / ".pending-x").write_text("partial")


class TestMirrorManifest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.saves = Path(self.temp.name) / "saves"
        populate(self.saves)
        self.listing = mirror_manifest.build(self.saves)
        self.by_dest = {entry["dest"]: entry for entry in self.listing["files"]}

    def test_lists_every_kind_with_readable_names(self):
        dests = set(self.by_dest)
        jbi = hashlib.sha256(b"/JOB\r\n//NAME WALL\r\n").hexdigest()
        self.assertIn(f"Test_Operator/Downloads/{DAY}/{CLOCK}_WALL_{jbi[:8]}.JBI", dests)
        self.assertTrue(any(d.startswith(f"Test_Operator/Downloads/{DAY}/{CLOCK}_wall_") and d.endswith(".gcode")
                            for d in dests))
        self.assertTrue(any(d.startswith(f"Test_Operator/Uploads/VAR.DAT/{DAY}/{CLOCK}_layer-touch_")
                            and d.endswith(".DAT") for d in dests))
        self.assertTrue(any(d.startswith(f"Part library/test_operator@letu.edu/{DAY}/{CLOCK}_cube_")
                            and d.endswith(".stl") for d in dests))
        self.assertIn("Test_Operator/Saved builds/widewallv4/request.json", dests)
        self.assertIn("Test_Operator/Saved builds/widewallv4/part.gcode", dests)
        sealed = [d for d in dests if d.startswith("Test_Operator/Runs/2026-10-02_WALL10_r01/")]
        self.assertIn("Test_Operator/Runs/2026-10-02_WALL10_r01/2026-10-02_WALL10_r01_SEALED.json", sealed)
        self.assertGreaterEqual(len(sealed), 6)
        self.assertTrue(any(d.startswith("sampleuser/Runs/") for d in dests))
        evil = [d for d in dests if d.startswith("sampleuser/Downloads/")]
        self.assertEqual(len(evil), 2)
        payload = next(d for d in evil if not d.endswith(".metadata.json"))
        self.assertRegex(payload, rf"^sampleuser/Downloads/{DAY}/{CLOCK}_evil_name_[0-9a-f]{{8}}\.JBI$")
        self.assertIn(payload + ".metadata.json", evil)

    def test_personal_and_library_payloads_keep_their_validated_metadata(self):
        sidecars = [entry for entry in self.listing["files"]
                    if entry["dest"].endswith(".metadata.json")]
        self.assertEqual(len(sidecars), 5)
        self.assertTrue(any(entry["src"].startswith("Test_Operator/") for entry in sidecars))
        self.assertTrue(any(entry["src"].startswith("library-") for entry in sidecars))
        for entry in sidecars:
            with self.subTest(src=entry["src"]):
                self.assertIn(entry["dest"][:-len(".metadata.json")], self.by_dest)
                record = json.loads((self.saves / entry["src"]).read_text())
                payload = self.by_dest[entry["dest"][:-len(".metadata.json")]]
                self.assertEqual((record["sha256"], record["bytes"]),
                                 (payload["sha256"], payload["bytes"]))

    def test_mismatched_archive_metadata_excludes_payload_and_sidecar(self):
        digest = hashlib.sha256(b"/JOB\r\n//NAME WALL\r\n").hexdigest()
        bundle = self.saves / "Test_Operator" / ".artifacts" / "jbi" / digest
        record = json.loads((bundle / "metadata.json").read_text())
        record["sha256"] = "0" * 64
        (bundle / "metadata.json").write_text(json.dumps(record))
        listing = mirror_manifest.build(self.saves)
        sources = {entry["src"] for entry in listing["files"]}
        self.assertFalse(any(f"/.artifacts/jbi/{digest}/" in "/" + source for source in sources))
        self.assertTrue(any("metadata does not match" in item["why"] for item in listing["skipped"]))

    def test_never_lists_guests_internals_or_unknown_files(self):
        srcs = " ".join(entry["src"] for entry in self.listing["files"])
        for absent in ("guest-123", "secrets.txt", "notahash", "active.json", ".pending-", ".acks"):
            self.assertNotIn(absent, srcs)

    def test_each_entry_carries_its_own_sha256_and_size(self):
        for entry in self.listing["files"]:
            body = (self.saves / entry["src"]).read_bytes()
            self.assertEqual(entry["sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(entry["bytes"], len(body))
        self.assertEqual(len(self.by_dest), len(self.listing["files"]))

    def test_people_are_short_only_when_unambiguous_on_windows(self):
        people = mirror_manifest._people([
            "Test_Operator", "sampleuser_at_letu_edu_fd6ee2",
            "alice_at_letu_edu_aaaaaa", "alice_at_gmail_com_bbbbbb",
            "Carol", "carol_at_letu_edu_cccccc"])
        self.assertEqual(people["Test_Operator"], "Test_Operator")
        self.assertEqual(people["sampleuser_at_letu_edu_fd6ee2"], "sampleuser")
        self.assertEqual(people["alice_at_letu_edu_aaaaaa"], "alice_at_letu_edu_aaaaaa")
        self.assertEqual(people["alice_at_gmail_com_bbbbbb"], "alice_at_gmail_com_bbbbbb")
        self.assertEqual(people["Carol"], "Carol")
        self.assertEqual(people["carol_at_letu_edu_cccccc"], "carol_at_letu_edu_cccccc")
        self.assertEqual(len({name.lower() for name in people.values()}), len(people))

    def test_cli_prints_the_listing(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(mirror_manifest.main(["--saves", str(self.saves)]), 0)
        self.assertEqual(json.loads(output.getvalue())["files"], self.listing["files"])


if __name__ == "__main__":
    unittest.main()
