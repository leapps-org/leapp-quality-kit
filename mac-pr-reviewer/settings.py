"""The reviewer's settings, read from the kit's config file (see kit_config.py).

The installer copies kit_config.py beside these files, so the installed worker finds it in its own
folder. In a checkout it is found in the folder above.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for folder in (HERE, HERE.parent):
    if (folder / "kit_config.py").is_file():
        sys.path.insert(0, str(folder))
        break
import kit_config  # noqa: E402

DEFAULT_STORE = "~/Library/Application Support/{repo}/PR Review/reviews.sqlite3"
DEFAULT_GUIDES = (".claude/skills/leapp-new-artifact/SKILL.md",)


def for_project(data, project):
    """The settings one repository's worker needs, as a dict."""
    owner = kit_config.need(data, "github_owner")
    owner_id = int(kit_config.need(data, "owner_id"))
    repos = list(kit_config.need(data, "mac_review_repos"))
    if project not in repos:
        raise kit_config.ConfigError(f"{project} is not in mac_review_repos")
    # The same store path the review gate reads, so a finished review here is one the gate sees.
    store = kit_config.expand(str(data.get("mac_review_store") or DEFAULT_STORE).format(repo=project))
    registry = (data.get("core_registries") or {}).get(project.lower())
    upstreams = data.get("mac_review_upstream_owners") or [owner]
    return {
        "project": project,
        "owner": owner,
        "owner_id": owner_id,
        "full_name": f"{owner}/{project}",
        # Repositories where the owner's own PRs are reviewed too.
        "owner_reviewed": project in set(data.get("owner_mac_review_repos") or []),
        "state": store.parent,
        "store": store,
        "registry": str(kit_config.expand(registry)) if registry else None,
        "upstream_owners": [str(name) for name in upstreams],
        "guides": tuple((data.get("mac_review_guides") or {}).get(project) or DEFAULT_GUIDES),
    }
