"""Settings wiring: the gate sees finished reviews, the corpus child gets its registry, the
installer writes nothing by default, and vendored upstreams are limited to configured owners."""
import json
import plistlib
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import kit_test_config
from kit_test_config import TEST_CONFIG, start, use
import corpus_check
import install_worker
import mac_review_worker as worker
import settings

pilot = worker.pilot
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-gate"))
import review_gate as gate  # noqa: E402

HEAD = "a" * 40


class StoreShared(unittest.TestCase):
    def test_a_finished_review_is_one_the_gate_sees(self):
        use("ALEAPP")
        gate.configure(TEST_CONFIG)
        self.assertEqual(Path(gate.MAC_STORE.format(repo="ALEAPP")), pilot.STORE)
        pr = {"number": 5, "user": {"id": 42, "login": "contributor"}, "state": "open", "draft": False,
              "head": {"sha": HEAD}, "base": {"repo": {"full_name": pilot.FULL_NAME}}}
        start(self, patch.object(pilot, "api", side_effect=lambda path, **kw: pr if path.endswith("/pulls/5") else []))
        with self.assertRaises(gate.CouldNotCheck):
            gate.mac_review_done("ALEAPP", 5, HEAD)
        with closing(pilot.database()) as db:
            self.assertFalse(gate.mac_review_done("ALEAPP", 5, HEAD))
            self.assertEqual(pilot.finish(db, 5, HEAD, "private notes", []), "Recorded contributor review")
        self.assertTrue(gate.mac_review_done("ALEAPP", 5, HEAD))
        self.assertFalse(gate.mac_review_done("ALEAPP", 5, "b" * 40))

    def test_a_store_shared_by_two_repositories_is_refused(self):
        shared = {**TEST_CONFIG, "mac_review_store": str(kit_test_config.STORE_ROOT / "one.sqlite3")}
        with self.assertRaises(settings.kit_config.ConfigError):
            settings.for_project(shared, "ALEAPP")
        alone = {**shared, "mac_review_repos": ["ALEAPP"]}
        self.assertEqual(settings.for_project(alone, "ALEAPP")["store"].name, "one.sqlite3")

    def test_repo_owners_moves_the_repository_not_the_owner(self):
        moved = settings.for_project({**TEST_CONFIG, "repo_owners": {"aleapp": "example-org"}}, "ALEAPP")
        self.assertEqual(moved["full_name"], "example-org/ALEAPP")
        self.assertEqual(moved["owner"], TEST_CONFIG["github_owner"])
        self.assertEqual(settings.for_project(TEST_CONFIG, "DLEAPP")["full_name"], "example-owner/DLEAPP")
        with self.assertRaises(settings.kit_config.ConfigError):
            settings.for_project({**TEST_CONFIG, "repo_owners": []}, "ALEAPP")

    def test_unlisted_repository_is_refused(self):
        with self.assertRaises(settings.kit_config.ConfigError):
            settings.for_project(TEST_CONFIG, "OTHER")


class CorpusChild(unittest.TestCase):
    def test_the_isolated_child_reads_the_configured_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "history.db"
            with closing(sqlite3.connect(source)) as con:
                con.execute("CREATE TABLE visits(visit_time)")
                con.execute("INSERT INTO visits VALUES(NULL)")
                con.commit()
            with zipfile.ZipFile(root / "sample.zip", "w") as z:
                z.write(source, "user/history.db")
            registry = root / "samples.json"
            registry.write_text(json.dumps({"samples": {"example": {"match": {"zip": "sample.zip"}}}}))
            use("ALEAPP", core_registries={"aleapp": str(registry)})
            self.assertEqual(pilot.SETTINGS["registry"], str(registry))
            path = "scripts/artifacts/demo.py"
            check = dict(path=path, assertion="column_has_null", basename="history.db",
                         table="visits", column="visit_time")
            packet = {"context": {"changed_paths": [path]},
                      "diff": f"diff --git a/{path} b/{path}\n+ history.db visits visit_time"}
            # The real subprocess, not a patched one: proves the child needs no config file.
            result = corpus_check.run_checks(pilot.SETTINGS["registry"], [check], packet)
            self.assertIn("1 true, 0 false", result)
            self.assertIn("Corpus registry unavailable", corpus_check.run_checks(None, [check], packet))
        use()


class Installer(unittest.TestCase):
    def test_plan_covers_each_repository_and_points_at_the_config(self):
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)):
            files, jobs = install_worker.plan(TEST_CONFIG, Path(home) / "config.json", ["/usr/local/bin"])
            self.assertEqual(list(Path(home).iterdir()), [])
        names = {path.name for path, _ in files}
        self.assertTrue({"kit_config.py", "settings.py", "mac_review_worker.py"} <= names)
        plists = [plistlib.loads(data) for path, data in files if path.suffix == ".plist"]
        self.assertEqual(sorted(p["EnvironmentVariables"]["LEAPP_REVIEW_PROJECT"] for p in plists),
                         sorted(TEST_CONFIG["mac_review_repos"]))
        for spec in plists:
            self.assertEqual(spec["EnvironmentVariables"]["LEAPP_QUALITY_CONFIG"], str(Path(home) / "config.json"))
            self.assertEqual(spec["StartInterval"], 300)
        self.assertEqual(len(jobs), len(plists))


class Manifest(unittest.TestCase):
    def test_a_manifest_whose_list_is_not_a_list_leaves_the_patch_visible(self):
        use()
        for value in (5, True, "x", {"path": "a.py"}):
            with self.subTest(value=value), \
                    patch.object(worker, "raw_bytes", return_value=json.dumps({"vendored": value}).encode()):
                self.assertEqual(worker.verified_vendored(HEAD, ["vendor/a.py"]), {})


class Upstreams(unittest.TestCase):
    def tearDown(self):
        use()

    def test_only_configured_owners_are_trusted(self):
        use()
        self.assertTrue(worker.trusted_upstream(f"https://github.com/{kit_test_config.OWNER_LOGIN}/reader"))
        self.assertIsNone(worker.trusted_upstream("https://github.com/someone/reader"))
        self.assertIsNone(worker.trusted_upstream(f"https://github.com/{kit_test_config.OWNER_LOGIN}x/reader"))
        use(mac_review_upstream_owners=["example-org", "example-owner"])
        self.assertTrue(worker.trusted_upstream("https://github.com/example-org/reader.git"))


if __name__ == "__main__":
    unittest.main()
