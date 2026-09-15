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
    if ":" not in image:
        return True
    last_colon_idx = image.rfind(":")
    after_colon = image[last_colon_idx + 1:]
    if "/" in after_colon:
        return False
    return False


def main():
    repo_root = Path(".").resolve()
    offenders = []

    for yaml_file in repo_root.rglob("*.yaml"):
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
