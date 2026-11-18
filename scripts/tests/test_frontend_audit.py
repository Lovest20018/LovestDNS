import json
from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[2]
AUDIT = runpy.run_path(str(ROOT / "packaging/integrated/audit_frontend.py"))


@pytest.fixture
def dependency_tree():
    packages = json.loads((ROOT / "integrated/frontend/package-lock.json").read_text(encoding="utf-8"))["packages"]
    findings = {
        "picomatch": {
            "nodes": ["node_modules/picomatch"],
            "via": [{"name": "picomatch", "url": AUDIT["PICOMATCH_URL"]}],
        },
        "tinyglobby": {"nodes": ["node_modules/tinyglobby"], "via": ["picomatch"]},
        "vite": {"nodes": ["node_modules/vite"], "via": ["tinyglobby", "picomatch"]},
    }
    return packages, findings


def test_audit_accepts_only_the_pinned_build_advisory_and_its_dependants(dependency_tree):
    packages, findings = dependency_tree
    assert AUDIT["known_build_advisory"](findings, packages)


@pytest.mark.parametrize("package,path", [
    ("picomatch", "node_modules/picomatch"),
    ("vite", "node_modules/yaml"),
])
def test_audit_rejects_matching_advisory_reaching_runtime(dependency_tree, package, path):
    packages, findings = dependency_tree
    findings[package]["nodes"] = [path]
    packages[path]["dev"] = False
    assert not AUDIT["known_build_advisory"](findings, packages)


def test_audit_rejects_unreviewed_findings_and_dependency_versions(dependency_tree):
    packages, findings = dependency_tree
    advisory = {"name": "different advisory", "url": "https://example.com/other-advisory"}
    findings["picomatch"]["via"].append(advisory)
    assert not AUDIT["known_build_advisory"](findings, packages)
    findings["picomatch"]["via"].pop()
    findings["eslint"] = {"nodes": ["node_modules/eslint"], "via": [advisory]}
    assert not AUDIT["known_build_advisory"](findings, packages)
    del findings["eslint"]
    packages["node_modules/picomatch"]["version"] = "4.0.6"
    assert not AUDIT["known_build_advisory"](findings, packages)
