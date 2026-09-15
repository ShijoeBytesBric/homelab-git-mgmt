# GitHub Actions — HomeLab Cluster

This doc describes the CI workflow that validates app and ArgoCD manifests in the `homelab-git-mgmt` repo before they reach the cluster. The workflow runs on every push and pull request, catches common misconfigurations client-side (no cluster access needed), and is intentionally lightweight.

---

## 1. What CI does

The workflow validates the manifests in the `homelab-git-mgmt` repo. In this setup, the `homelab-git-mgmt` repo holds ArgoCD's own config (AppProject, Application CRs, repo secret) — the cluster-facing app manifests live in `HomeLabCluster`. The CI for `homelab-git-mgmt` validates the ArgoCD Application CRs (which reference paths in `HomeLabCluster`). When you set up a CI workflow in `HomeLabCluster` as well (recommended), it validates the actual app manifests.

Both repos should have the same validation workflow — the content differs slightly (one validates ArgoCD CRs, the other validates app manifests), but the tooling is identical.

### Checks performed

| Check | Tool | Catches |
|---|---|---|
| YAML syntax / style | `yamllint` | Indentation errors, tab usage, missing document separators, overly long lines |
| Kubernetes resource schema | `kubectl apply --dry-run=client -f <file>` | API version errors, missing required fields, type mismatches, malformed resource specs |
| ArgoCD Application CR schema | `kubectl apply --dry-run=client -f argocd/applications/*.yaml` | Broken repo URLs, invalid project refs, invalid sync policy fields |
| Image tag pinning | `scripts/check_image_tags.py` | Any container image using `:latest` or no tag — warns and fails CI |
| Namespace consistency | `scripts/check_namespace.py` | Namespaced resources (Deployment, Service, etc.) whose `metadata.namespace` doesn't match the app dir name — catches cross-app collisions |
| ServiceMonitor-to-Service pairing | (optional, future) `scripts/check_servicemonitor.py` | ServiceMonitors whose selector doesn't match a Service in the same app dir — catches broken scrapers before they're synced |

---

## 2. Workflow file

```yaml
# .github/workflows/validate.yaml
name: validate
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Install kubectl
        uses: azure/setup-kubectl@v3
        with:
          version: latest

      - name: Install yamllint
        run: pip install yamllint

      - name: Install Python deps (for custom scripts)
        run: pip install pyyaml

      - name: Lint YAML
        run: yamllint -c .yamllint . --exclude '.github/' --exclude '.git/'

      - name: Validate Kubernetes resources (client-side)
        run: |
          set -euo pipefail
          # Validate all YAML files in the repo as K8s resources
          # Skip files that aren't K8s resources (e.g. README, docs)
          for f in $(find . -name '*.yaml' -o -name '*.yml'); do
            # Skip non-resource files
            if [[ "$f" == "./.github/"* || "$f" == "./docs/"* || "$f" == "./scripts/"* ]]; then
              continue
            fi
            echo "Validating $f"
            kubectl apply --dry-run=client -f "$f" || { echo "FAIL: $f"; exit 1; }
          done

      - name: Check image tags
        run: python scripts/check_image_tags.py

      - name: Check namespace consistency
        run: python scripts/check_namespace.py

      - name: Check ServiceMonitor pairing (optional — enable when you add ServiceMonitors)
        run: |
          if [ -f scripts/check_servicemonitor.py ]; then
            python scripts/check_servicemonitor.py
          else
            echo "check_servicemonitor.py not present — skipping"
          fi
```

**Notes:**

- The `kubectl --dry-run=client` step validates every YAML file in the repo as a Kubernetes resource. It skips `.github/`, `docs/`, and `scripts/` (which aren't K8s resources). If your repo has other non-resource YAML (e.g. Helm chart files like `Chart.yaml`, `values.yaml` that aren't standalone resources), add them to the exclusion list or validate them separately.
- For a Helm-based app (monitoring), the `values.yaml` alone won't pass `kubectl --dry-run=client` because it's not a resource — it's just values. The ArgoCD `Application` CR that references the chart is what gets validated by `kubectl`. So the workflow validates the `Application` CR (valid) and skips the `values.yaml` (excluded or validated by a separate check). If you want to validate Helm values, add a `helm template` dry-run step, but for v1 the `Application` CR validation is enough.
- The image-tag and namespace checks are custom Python scripts (below). They're intentionally simple.

---

## 3. Scripts

### 3.1 `scripts/check_image_tags.py`

Looks for container images in Deployment (and other workload) specs that use `:latest` or have no tag at all, and fails CI with a list of offenders.

```python
#!/usr/bin/env python3
"""
Check that all container images in workload resources use a pinned tag
(i.e. not :latest and not no tag). Fails CI with a list of offenders.

Usage: python scripts/check_image_tags.py
Exit code 0 = pass, 1 = fail (with offenders printed).
"""

import sys
import yaml
from pathlib import Path


def collect_images(doc):
    """Recursively collect all container image strings from a K8s doc."""
    images = []
    kind = doc.get("kind", "")

    # Workload resources with containers
    containers = []
    if kind in ("Deployment", "DaemonSet", "StatefulSet", "Pod", "Job", "CronJob"):
        spec = doc.get("spec", {})
        if kind == "CronJob":
            spec = spec.get("jobTemplate", {}).get("spec", {})
        template_spec = (
            spec.get("template", {}).get("spec", {})
            if "template" in spec
            else spec
        )
        containers = template_spec.get("containers", [])

    for container in containers:
        image = container.get("image", "")
        if image:
            images.append((kind, image))

    return images


def has_unpinned_tag(image: str) -> bool:
    """Return True if the image uses :latest or has no tag."""
    if image.endswith(":latest") or image == "latest":
        return True
    # No tag at all: image has no colon, or colon is part of a registry
    # without a tag (registry/image vs registry/image:tag).
    if ":" not in image:
        return True
    # Has a colon — check it's a tag, not just a registry port.
    # registry.io:5000/image has a colon for the port. Distinguish by
    # checking if there's a slash after the colon.
    # Simplistic: if the part after the last colon contains a slash,
    # it's a port, not a tag.
    last_colon_idx = image.rfind(":")
    after_colon = image[last_colon_idx + 1:]
    if "/" in after_colon:
        return False  # port, not tag — assume pinned (the registry/image part is pinned)
    # Has a tag, but is it :latest? Already handled above. Anything else is pinned.
    return False


def main():
    repo_root = Path(".").resolve()
    offenders = []

    for yaml_file in repo_root.rglob("*.yaml"):
        # Skip non-resource files
        rel = yaml_file.relative_to(repo_root)
        if rel.parts[0] in (".github", "docs", "scripts"):
            continue

        try:
            docs = list(yaml.safe_load_all(yaml_file.read_text()))
        except yaml.YAMLError as e:
            print(f"YAML parse error in {yaml_file}: {e}")
            sys.exit(1)

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            for kind, image in collect_images(doc):
                if has_unpinned_tag(image):
                    offenders.append((yaml_file, kind, image))

    if offenders:
        print("FAIL: Unpinned image tags found (use a pinned tag or SHA):")
        for path, kind, image in offenders:
            print(f"  {path}  ({kind})  {image}")
        sys.exit(1)

    print("PASS: All container images use pinned tags.")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

### 3.2 `scripts/check_namespace.py`

Verifies that every namespaced resource in an app directory has `metadata.namespace` matching the directory name. If a resource is in `apps/jellyfin/` but has `namespace: flaresolverr`, CI fails.

```python
#!/usr/bin/env python3
"""
Check that every namespaced Kubernetes resource in an app directory
has metadata.namespace matching the directory name.

Usage: python scripts/check_namespace.py
Exit code 0 = pass, 1 = fail (with mismatches printed).

Cluster-scoped resources (Namespace, ClusterRole, etc.) are skipped.
"""

import sys
import yaml
from pathlib import Path

# Cluster-scoped kinds — these don't have a namespace field and are fine
CLUSTER_SCOPED_KINDS = {
    "Namespace",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "PriorityClass",
    "ComponentStatus",  # deprecated but keep for completeness
}


def is_cluster_scoped(doc):
    return doc.get("kind", "") in CLUSTER_SCOPED_KINDS


def check_file(yaml_file: Path):
    """Return list of (kind, actual_namespace, expected_namespace) mismatches."""
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
            # Some resources don't have a namespace field at all even if
            # they're namespaced in some contexts — skip those (e.g. a bare
            # ServiceAccount in some API versions). We only flag clear mismatches.
            continue
        # Expected namespace = the first directory component under apps/
        # e.g. apps/jellyfin/deployment.yaml -> expected = jellyfin
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
            # Only check files under apps/ — that's where per-app namespace
            # consistency matters. ArgoCD CRs in argocd/ are cluster-scoped
            # or explicitly namespace argocd, so skip them.
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
```

### 3.3 `scripts/check_servicemonitor.py` (future — placeholder)

When you start adding `ServiceMonitor` CRs for apps with native metrics, add this script to validate that every `ServiceMonitor`'s selector matches a `Service` in the same app directory.

```python
#!/usr/bin/env python3
"""
Check that every ServiceMonitor has a matching Service in the same app dir.

Usage: python scripts/check_servicemonitor.py
Exit code 0 = pass, 1 = fail.

Not shipped in v1 — create this file when you add your first ServiceMonitor.
"""

import sys
import yaml
from pathlib import Path


def main():
    print("ServiceMonitor pairing check not yet implemented.")
    print("Create scripts/check_servicemonitor.py when you add your first ServiceMonitor.")
    sys.exit(0)


if __name__ == "__main__":
    main()
```

---

## 4. `.yamllint` config

```yaml
# .yamllint
extends: default

rules:
  line-length:
    max: 120
    level: warning
  truthy:
    allowed-values: ['true', 'false']
    level: warning
  comments:
    level: warning
  indentation:
    indent-sequences: consistent
    level: warning
```

Relaxed enough for hand-written K8s YAML. The `truthy` rule catches `yes/no` vs `true/false` which K8s doesn't accept. The `line-length` warning keeps things readable without breaking on long annotations.

---

## 5. How to extend CI

Add a new check by:

1. Writing a script in `scripts/` (Python, simple, exits 0/1 with a clear message).
2. Adding a step to the workflow that runs it.
3. Testing the step locally: `python scripts/<name>.py` in the repo.

The workflow is meant to stay fast (under a minute on a standard push). If a check takes too long, make it selective rather than running it over the whole repo.

---

## 6. What CI does NOT do

- **No cluster access.** The workflow runs on GitHub-hosted runners with no network path to your homelab. All checks are client-side (`--dry-run=client`).
- **No integration tests.** No `kind`, no `minikube`, no test cluster. For v1, catching schema errors and misconfigurations client-side is enough.
- **No policy engine.** No OPA/Gatekeeper/Kyverno in CI. The image-tag and namespace checks are the only custom guardrails — they cover the most common homelab mistakes.
- **No diff against the cluster.** CI can't see what's actually running in the cluster, so it can't catch drift. ArgoCD's selfHeal handles drift; CI handles pre-commit mistakes.

---

## 7. Setting up CI in both repos

| Repo | What CI validates | Workflow location |
|---|---|---|
| `homelab-git-mgmt` | ArgoCD CRs (AppProject, Application CRs, repo secret) — the `kubectl --dry-run=client` step validates these. The image-tag and namespace checks also run (they're harmless on ArgoCD CRs — ArgoCD CRs don't have container images, and their namespace is `argocd`). | `.github/workflows/validate.yaml` in `homelab-git-mgmt` |
| `HomeLabCluster` | App manifests (Deployments, Services, Ingresses, etc.) — all checks apply. The namespace check is most useful here (every app dir's resources should match the dir name). | `.github/workflows/validate.yaml` in `HomeLabCluster` |

The two workflows are nearly identical — the only difference is the repo they're in. Copy the same `.github/workflows/validate.yaml` and `.yamllint` to both repos. The scripts live in both repos too (they're self-contained, no external deps beyond `pyyaml`).

---

## 8. Running checks locally

Before pushing, you can run the same checks locally:

```bash
# Install deps
pip install yamllint pyyaml

# Lint
yamllint -c .yamllint . --exclude '.github/' --exclude '.git/'

# Validate K8s resources
for f in $(find . -name '*.yaml' -o -name '*.yml'); do
  kubectl apply --dry-run=client -f "$f" || echo "FAIL: $f"
done

# Custom checks
python scripts/check_image_tags.py
python scripts/check_namespace.py
```

If all pass, push. If any fail, fix before pushing — ArgoCD will sync whatever lands in the repo, so broken YAML in the repo means a broken sync attempt.
