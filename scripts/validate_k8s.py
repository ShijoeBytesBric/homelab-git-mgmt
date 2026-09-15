#!/usr/bin/env python3
"""
Structural validation of Kubernetes YAML manifests — no cluster needed.

Checks YAML syntax, required fields, and basic schema for resource types
used in this repo. Replaces `kubectl apply --dry-run=client` which needs
a reachable cluster even with --validate=false.

Usage: python scripts/validate_k8s.py
Exit code 0 = pass, 1 = fail.
"""

import sys
import yaml
from pathlib import Path

# Known K8s resource kinds and the fields we validate.
# Each entry: kind -> list of (label, predicate(doc_dict)).
# The predicate receives the FULL doc dict (not just spec).

REQUIRED_META = ["name"]
OPTIONAL_META = ["namespace", "labels", "annotations", "uid", "resourceVersion",
                 "creationTimestamp", "selfLink", "generation", "managedFields"]

SPEC_CHECKS = {
    "Namespace": [
        ("metadata.name", lambda d: bool(d.get("metadata", {}).get("name"))),
    ],
    "Deployment": [
        ("spec.replicas", lambda d: "replicas" in d.get("spec", {})),
        ("spec.selector.matchLabels", lambda d: bool(d.get("spec", {}).get("selector", {}).get("matchLabels"))),
        ("spec.strategy.type", lambda d: d.get("spec", {}).get("strategy", {}).get("type") in ("Recreate", "RollingUpdate")),
        ("spec.template.spec.containers", lambda d: bool(d.get("spec", {}).get("template", {}).get("spec", {}).get("containers"))),
        ("container[*].image", lambda d: all("image" in c for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))),
        ("container[*].name", lambda d: all("name" in c for c in d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []))),
    ],
    "Service": [
        ("spec.ports", lambda d: bool(d.get("spec", {}).get("ports"))),
        ("spec.selector", lambda d: bool(d.get("spec", {}).get("selector"))),
    ],
    "PersistentVolumeClaim": [
        ("spec.accessModes", lambda d: bool(d.get("spec", {}).get("accessModes"))),
        ("spec.resources.requests", lambda d: bool(d.get("spec", {}).get("resources", {}).get("requests"))),
    ],
    "ConfigMap": [
        ("data", lambda d: isinstance(d.get("data"), dict)),
    ],
    "Secret": [
        ("type", lambda d: "type" in d.get("spec", {}) or "argocd.argoproj.io/secret-type" in d.get("metadata", {}).get("labels", {})),
    ],
    "Role": [
        ("rules", lambda d: bool(d.get("rules"))),
        ("rules[*].apiGroups", lambda d: all("apiGroups" in r for r in d.get("rules", []))),
        ("rules[*].resources", lambda d: all("resources" in r for r in d.get("rules", []))),
        ("rules[*].verbs", lambda d: all("verbs" in r for r in d.get("rules", []))),
    ],
    "RoleBinding": [
        ("subjects", lambda d: bool(d.get("subjects"))),
        ("roleRef.kind", lambda d: "kind" in d.get("roleRef", {})),
        ("roleRef.name", lambda d: "name" in d.get("roleRef", {})),
    ],
    "AppProject": [
        ("spec.sourceRepos", lambda d: bool(d.get("spec", {}).get("sourceRepos"))),
        ("spec.destinations", lambda d: bool(d.get("spec", {}).get("destinations"))),
    ],
    "Application": [
        ("spec.project", lambda d: "project" in d.get("spec", {})),
        ("spec.source", lambda d: "source" in d.get("spec", {})),
        ("spec.destination", lambda d: "destination" in d.get("spec", {})),
    ],
    "SealedSecret": [
        ("spec.encryptedData", lambda d: bool(d.get("spec", {}).get("encryptedData"))),
        ("spec.template.name", lambda d: bool(d.get("spec", {}).get("template", {}).get("metadata", {}).get("name"))),
        ("spec.template.namespace", lambda d: bool(d.get("spec", {}).get("template", {}).get("metadata", {}).get("namespace"))),
    ],
    "Ingress": [
        ("spec.rules", lambda d: bool(d.get("spec", {}).get("rules"))),
    ],
}


def check_doc(doc: dict, rel_path: str) -> list:
    """Return list of (label, message) failures for one YAML doc."""
    failures = []
    kind = doc.get("kind", "")
    meta = doc.get("metadata", {})
    meta_name = meta.get("name", "<no-name>") if isinstance(meta, dict) else "<invalid-meta>"

    # --- mandatory top-level fields ---
    if not kind:
        failures.append(("kind", f"{rel_path}: missing kind field"))
        return failures
    if not isinstance(meta, dict):
        failures.append(("metadata", f"{rel_path} ({kind}): metadata is not a dict"))
        return failures
    for field in REQUIRED_META:
        if field not in meta:
            failures.append(("metadata." + field, f"{rel_path} ({kind}): missing metadata.{field}"))
            return failures

    # --- kind-specific checks ---
    for label, predicate in SPEC_CHECKS.get(kind, []):
        try:
            if not predicate(doc):
                failures.append((label, f"{rel_path} ({kind} {meta_name}): missing/invalid {label}"))
        except Exception as e:
            failures.append((label, f"{rel_path} ({kind}): error checking {label}: {e}"))

    # --- Deployment: container ports (optional, but flag if missing) ---
    if kind == "Deployment":
        containers = doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        for idx, c in enumerate(containers):
            if not c.get("ports"):
                failures.append(("container.ports",
                                 f"{rel_path} ({kind} {meta_name}) container[{idx}]: no ports defined"))

    # --- Service: each port needs port + targetPort ---
    if kind == "Service":
        for idx, p in enumerate(doc.get("spec", {}).get("ports", [])):
            for field in ("port", "targetPort"):
                if field not in p:
                    failures.append((f"port.{field}",
                                     f"{rel_path} ({kind} {meta_name}) port[{idx}]: missing {field}"))

    # --- Ingress: rules must have host or http backend ---
    if kind == "Ingress":
        rules = doc.get("spec", {}).get("rules", [])
        if not rules:
            failures.append(("spec.rules", f"{rel_path} ({kind} {meta_name}): no rules"))
        else:
            for idx, rule in enumerate(rules):
                if not rule.get("host") and not rule.get("http"):
                    failures.append(("rule.host", f"{rel_path} rule[{idx}]: missing host or http"))

    # --- AppProject: each destination needs namespace + server ---
    if kind == "AppProject":
        for idx, dest in enumerate(doc.get("spec", {}).get("destinations", [])):
            if "namespace" not in dest:
                failures.append(("destination.namespace",
                                 f"{rel_path} destination[{idx}]: missing namespace"))
            if "server" not in dest:
                failures.append(("destination.server",
                                 f"{rel_path} destination[{idx}]: missing server"))

    # --- Application: source must have path (git) or chart (helm) ---
    if kind == "Application":
        src = doc.get("spec", {}).get("source", {})
        if "path" not in src and "chart" not in src:
            failures.append(("spec.source", f"{rel_path} ({kind} {meta_name}): missing path or chart in source"))

    return failures


def is_helm_values(doc: dict) -> bool:
    """Return True if doc looks like a Helm values.yaml (no apiVersion/kind)."""
    return "apiVersion" not in doc and "kind" not in doc


def main():
    root = Path(".").resolve()
    failures = []
    count = 0

    for fpath in sorted(root.rglob("*.yaml")):
        rel = fpath.relative_to(root)
        # skip non-resource dirs
        if rel.parts[0] in (".github", "docs", "scripts", ".git"):
            continue

        try:
            docs = list(yaml.safe_load_all(fpath.read_text()))
        except yaml.YAMLError as e:
            failures.append((str(rel), f"YAML parse error: {e}"))
            continue

        for i, doc in enumerate(docs):
            if not isinstance(doc, dict):
                continue
            if is_helm_values(doc):
                continue  # skip Helm values.yaml
            count += 1
            failures.extend(check_doc(doc, f"{rel}#{i}"))

    if failures:
        print(f"FAIL: {len(failures)} issue(s) in {count} resources:\n")
        for label, msg in failures:
            print(f"  [{label}] {msg}")
        sys.exit(1)

    print(f"PASS: {count} resources validated.")
    sys.exit(0)


if __name__ == "__main__":
    main()
