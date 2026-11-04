#!/usr/bin/env python3
"""The repository's settings on GitHub, written down here with why.

    python3 tools/repo/github_settings.py          what differs from them
    python3 tools/repo/github_settings.py apply    set them

Through gh, logged in as an admin of the repository. `apply` changes only
what differs, and of the checks before main (the second ruleset) requires
only those that have run on main, saying which are left: a check required
before its workflow is there - a new job, or one renamed - would hold every
pull request back, the firmware-update workflow's too. Run it again once
they have run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

REPOSITORY = "adamsthws/firmware-liberator"
GITHUB_ACTIONS = 15368   # the GitHub Actions app: only its runs may pass a required check
ADMIN_ROLE = 5           # the repository's admin role, in a ruleset's bypass list

# Pull requests are squashed - one commit each, as main's history is kept -
# and their branches deleted once merged.
REPOSITORY_SETTINGS = {
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "delete_branch_on_merge": True,
}

# Secret scanning refuses a push holding a known kind of token; Dependabot
# opens a pull request at once for a package with an advisory (its weekly
# updates wait 7 days, .github/dependabot.yml).
SECURITY = {
    "secret_scanning": "enabled",
    "secret_scanning_push_protection": "enabled",
    "dependabot_security_updates": "enabled",
}

# CodeQL's own setup: the Python and the workflows, on each push and pull
# request to main and weekly. (The C under archive/ is history.)
CODE_SCANNING = {"state": "configured", "languages": ["actions", "python"], "query_suite": "default"}

# On/off settings, each at repos/REPOSITORY/<path>: private vulnerability
# reporting, which SECURITY.md asks for; immutable releases - a release's
# tag and files cannot be changed once published, and GitHub signs an
# attestation of them (`gh release verify`).
TOGGLES = ["private-vulnerability-reporting", "immutable-releases"]

# The website, firmwareliberator.com, is a Cloudflare Worker
# (website/README.md) the website workflow deploys from this environment: it
# takes only main, and holds the workflow's CLOUDFLARE_API_TOKEN. GitHub
# Pages, which served it before, is off.
WEBSITE = "https://firmwareliberator.com/"
ENVIRONMENT = "website"
ENVIRONMENT_POLICY = {"protected_branches": False, "custom_branch_policies": True}
ENVIRONMENT_BRANCHES = ["main"]

MAIN = {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}}
# The checks main requires: jobs of the unit tests and emulator, leak-scan
# and lint workflows.
REQUIRED_CHECKS = ["unit (Python 3.12)", "unit (Python 3.10)", "emulator", "scan", "lint"]


def checks_ruleset(names: list[str]) -> dict[str, Any]:
    return {
        "target": "branch", "enforcement": "active", "conditions": MAIN,
        "bypass_actors": [{"actor_id": ADMIN_ROLE, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
        "rules": [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": [{"context": name, "integration_id": GITHUB_ACTIONS} for name in names],
        }}],
    }


RULESETS: dict[str, dict[str, Any]] = {
    # For everyone, admins too: main is never deleted or rewritten. Signed
    # commits are not required: GitHub then lets only a pull request's author
    # squash it, so no one else's could be merged.
    "Protect Main Branch": {
        "target": "branch", "enforcement": "active", "conditions": MAIN, "bypass_actors": [],
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    },
    # What reaches main through a pull request has passed the checks. An admin
    # may push to main directly, as releases are made; the release workflow
    # runs the same checks before it releases.
    "Checks before main": checks_ruleset(REQUIRED_CHECKS),
}


class SettingsError(RuntimeError):
    pass


def api(path: str, method: str = "GET", body: Any = None, ok_missing: bool = False) -> Any:
    command = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json",
               f"repos/{REPOSITORY}{path}"]
    if body is not None:
        command += ["--input", "-"]
    result = subprocess.run(command, input=None if body is None else json.dumps(body),
                            capture_output=True, text=True)
    if result.returncode:
        if ok_missing and "(HTTP 404)" in result.stderr:
            return None
        raise SettingsError(f"gh api -X {method} {path}: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def comparable(ruleset: dict[str, Any]) -> dict[str, Any]:
    """What a ruleset says, as RULESETS writes it, from what GitHub returns."""
    rules = [{key: value for key, value in rule.items() if key in ("type", "parameters")}
             for rule in ruleset.get("rules", [])]
    return {
        "target": ruleset.get("target"), "enforcement": ruleset.get("enforcement"),
        "conditions": ruleset.get("conditions"), "rules": rules,
        "bypass_actors": [{key: actor[key] for key in ("actor_id", "actor_type", "bypass_mode")}
                          for actor in ruleset.get("bypass_actors") or []],
    }


def checks_run_on_main() -> set[str]:
    """Each check run on main's last 30 commits."""
    names: set[str] = set()
    for commit in api("/commits?sha=main&per_page=30"):
        runs = api(f"/commits/{commit['sha']}/check-runs?per_page=100")
        names.update(run["name"] for run in runs["check_runs"])
    return names


def differences() -> list[tuple[str, str, Any]]:
    """(what, how it differs, the change that makes it right)."""
    found: list[tuple[str, str, Any]] = []
    repository = api("")
    for key, want in REPOSITORY_SETTINGS.items():
        if repository.get(key) != want:
            found.append((key, f"is {repository.get(key)}, should be {want}", ("PATCH", "", {key: want})))
    analysis = repository.get("security_and_analysis") or {}
    for key, want in SECURITY.items():
        have = (analysis.get(key) or {}).get("status")
        if have != want:
            found.append((key, f"is {have}, should be {want}",
                          ("PATCH", "", {"security_and_analysis": {key: {"status": want}}})))
    scanning = api("/code-scanning/default-setup")
    have = {key: scanning.get(key) for key in CODE_SCANNING}
    if have["state"] != CODE_SCANNING["state"] or sorted(have["languages"] or []) != CODE_SCANNING["languages"] \
            or have["query_suite"] != CODE_SCANNING["query_suite"]:
        found.append(("CodeQL", f"is {have}, should be {CODE_SCANNING}",
                      ("PATCH", "/code-scanning/default-setup", CODE_SCANNING)))
    for toggle in TOGGLES:
        if not (api(f"/{toggle}", ok_missing=True) or {}).get("enabled"):
            found.append((toggle, "is off, should be on", ("PUT", f"/{toggle}", None)))
    if api("/pages", ok_missing=True) is not None:
        found.append(("Pages", "is on, should be off: the website is a Cloudflare Worker",
                      ("DELETE", "/pages", None)))
    environment = api(f"/environments/{ENVIRONMENT}", ok_missing=True)
    have = (environment or {}).get("deployment_branch_policy")
    if have != ENVIRONMENT_POLICY:
        how = "is missing" if environment is None else f"has the branch policy {have}, should name its branches"
        found.append((f"environment '{ENVIRONMENT}'", how,
                      ("PUT", f"/environments/{ENVIRONMENT}", {"deployment_branch_policy": ENVIRONMENT_POLICY})))
    policies = api(f"/environments/{ENVIRONMENT}/deployment-branch-policies")["branch_policies"] \
        if have == ENVIRONMENT_POLICY else []
    for branch in ENVIRONMENT_BRANCHES:
        if not any(policy["name"] == branch and policy.get("type") == "branch" for policy in policies):
            found.append((f"environment '{ENVIRONMENT}' branch {branch}", "is missing",
                          ("POST", f"/environments/{ENVIRONMENT}/deployment-branch-policies",
                           {"name": branch, "type": "branch"})))
    for policy in policies:
        if policy["name"] not in ENVIRONMENT_BRANCHES or policy.get("type") != "branch":
            found.append((f"environment '{ENVIRONMENT}' {policy.get('type')} {policy['name']}", "should not be there",
                          ("DELETE", f"/environments/{ENVIRONMENT}/deployment-branch-policies/{policy['id']}", None)))
    existing = {ruleset["name"]: ruleset["id"] for ruleset in api("/rulesets")}
    for name, want in RULESETS.items():
        body = {"name": name, **want}
        if name not in existing:
            found.append((f"ruleset '{name}'", "is missing", ("POST", "/rulesets", body)))
        elif comparable(api(f"/rulesets/{existing[name]}")) != comparable(want):
            found.append((f"ruleset '{name}'", "differs", ("PUT", f"/rulesets/{existing[name]}", body)))
    return found


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args not in ([], ["apply"]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        found = differences()
        if not found:
            print(f"{REPOSITORY}: every setting is as tools/repo/github_settings.py says.")
            return 0
        for what, how, _ in found:
            print(f"  {what}: {how}")
        if not args:
            print("python3 tools/repo/github_settings.py apply sets them.")
            return 1
        for what, _, (method, path, body) in found:
            if what == "ruleset 'Checks before main'":
                run = checks_run_on_main()
                ready = [name for name in REQUIRED_CHECKS if name in run]
                if not ready:
                    print(f"  {what}: left for now - none has run on main yet")
                    continue
                if ready != REQUIRED_CHECKS:
                    body = {"name": "Checks before main", **checks_ruleset(ready)}
                    not_run = [name for name in REQUIRED_CHECKS if name not in run]
                    api(path, method, body)
                    print(f"  {what}: set, but for {', '.join(not_run)}, not yet run on main: "
                          "apply again once they have")
                    continue
            api(path, method, body)
            print(f"  {what}: set")
    except SettingsError as error:
        print(f"github_settings: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
