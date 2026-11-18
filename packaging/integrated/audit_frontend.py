"""Run the full npm audit with one exception for a pinned build dependency."""

import json
from pathlib import Path
import shutil
import subprocess
import sys


PICOMATCH_ADVISORY = "GHSA-fc5q-39xc-vjpf"
PICOMATCH_URL = "https://github.com/advisories/" + PICOMATCH_ADVISORY


def known_build_advisory(findings, packages):
    for finding in findings.values():
        nodes = finding.get("nodes")
        if not isinstance(nodes, list) or not nodes or any(
            not isinstance(node, str) or packages.get(node, {}).get("dev") is not True
            for node in nodes
        ):
            return False

    picomatch = findings.get("picomatch", {})
    causes = picomatch.get("via")
    if (picomatch.get("nodes") != ["node_modules/picomatch"]
            or packages["node_modules/picomatch"].get("version") != "4.0.7"
            or not isinstance(causes, list) or len(causes) != 1):
        return False
    advisory = causes[0]
    if not isinstance(advisory, dict) or advisory.get("name") != "picomatch" or advisory.get("url") != PICOMATCH_URL:
        return False

    accepted = {"picomatch"}
    remaining = set(findings) - accepted
    while remaining:
        # npm also reports dependants. Require every reported cause to
        # trace back to the one reviewed advisory.
        dependants = {
            name for name in remaining
            if isinstance(causes := findings[name].get("via"), list) and causes
            and all(isinstance(cause, str) and cause in accepted for cause in causes)
        }
        if not dependants:
            return False
        accepted.update(dependants)
        remaining.difference_update(dependants)
    return True


def audit(directory, npm):
    result = subprocess.run(
        [npm, "audit", "--json", "--audit-level=low", "--include=prod", "--include=dev", "--include=optional", "--include=peer"],
        cwd=directory, stdout=subprocess.PIPE, text=True, encoding="utf-8", check=False,
    )
    try:
        report = json.loads(result.stdout)
        findings = report["vulnerabilities"]
        total = report["metadata"]["vulnerabilities"]["total"]
        if (report.get("error") or report.get("auditReportVersion") != 2 or not isinstance(findings, dict)
                or total != len(findings) or any(not isinstance(value, dict) for value in findings.values())):
            raise ValueError("invalid audit report")
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SystemExit("npm audit did not return a complete dependency report") from None
    if not findings and result.returncode == 0:
        print("npm audit passed without advisories")
        return []
    packages = json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    if result.returncode == 1 and known_build_advisory(findings, packages):
        print(f"npm audit accepted the known build-only advisory {PICOMATCH_ADVISORY} for picomatch 4.0.7")
        return [PICOMATCH_ADVISORY]
    raise SystemExit("npm audit rejected the dependency tree: " + ", ".join(sorted(findings)))


if __name__ == "__main__":
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if npm is None:
        raise SystemExit("Node.js/npm is required")
    audit(Path.cwd(), npm)
