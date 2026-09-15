#!/usr/bin/env python3
"""
Check that every namespaced Kubernetes resource in an app directory
has metadata.namespace matching the directory name.

Usage: python scripts/check_namespace.py
Exit code 0 = pass, 1 = fail (with mismatches printed).
"""

import sys
import yaml
from pathlib import Path

CLUSTER_SCOPED_KINDS = {
    "Namespace",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "PriorityClass",
}


def is_cluster_scoped(doc):
    return doc.get("kind", "") in CLUSTER_SCOPED_KINDS


def check_file(yaml_file: Path):
    mismatches = []
    try:
        docs = list(yaml.safe_load_all(yaml_file.read_text()))
    except yaml.YAMLError as e:
        print(f"YAML parse error in {yaml_file}: {e}")
        sys.exit(1)

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if is_cluster_scoped(doc):
            continue
        actual_ns = doc.get("metadata", {}).get("namespace", "")
        if not actual_ns:
            continue
        parts = yaml_file.relative_to(yaml_file.parent.parent).parts
        if len(parts) >= 2 and parts[0] == "apps":
            expected_ns = parts[1]
            if actual_ns and actual_ns != expected_ns:
                mismatches.append((doc.get("kind", "Unknown"), actual_ns, expected_ns))
    return mismatches


def main():
    repo_root = Path(".").resolve()
    mismatches = []

    for yaml_file in repo_root.rglob("*.yaml"):
        rel = yaml_file.relative_to(repo_root)
        if rel.parts[0] in (".github", "docs", "scripts"):
            continue
        if rel.parts[0] != "apps":
            continue
        mismatches.extend(check_file(yaml_file))

    if mismatches:
        print("FAIL: Namespace mismatches (resource namespace != app directory):")
        for kind, actual, expected in mismatches:
            print(f"  {kind}: namespace={actual}, expected={expected}")
        sys.exit(1)

    print("PASS: Namespace consistency check passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
