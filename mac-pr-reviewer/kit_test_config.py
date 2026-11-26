"""Placeholder settings shared by the tests. Nothing here names a real account or path."""
import atexit
import shutil
import tempfile
from pathlib import Path

import review_leapp_pr as pilot

STORE_ROOT = Path(tempfile.mkdtemp(prefix="mac-review-test-"))
atexit.register(shutil.rmtree, STORE_ROOT, True)
OWNER_LOGIN = "example-owner"
OWNER_ID = 1000
TEST_CONFIG = {
    "github_owner": OWNER_LOGIN,
    "owner_id": OWNER_ID,
    "mac_review_repos": ["iLEAPP", "ALEAPP", "RLEAPP", "VLEAPP", "DLEAPP"],
    "owner_mac_review_repos": ["DLEAPP"],
    "mac_review_store": str(STORE_ROOT / "{repo}" / "PR Review" / "reviews.sqlite3"),
    "core_registries": {},
}


def use(project="ALEAPP", **changes):
    """Configure the pilot module for one repository with the placeholder settings."""
    pilot.configure({**TEST_CONFIG, **changes}, project)


def start(case, patcher):
    """TestCase.enterContext for Python 3.10: start a patch and stop it at cleanup."""
    value = patcher.start()
    case.addCleanup(patcher.stop)
    return value


use()
