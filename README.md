# homelab-git-mgmt

ArgoCD configuration repo for the homelab Kubernetes cluster.

This repo holds ArgoCD's own configuration: the `AppProject` that scopes what ArgoCD can sync, the `Application` CRs that tell ArgoCD what to deploy and where, and the repository registration secret. It is separate from `homelab-apps` (the cluster's deployable manifest repo) so that ArgoCD's wiring is versioned independently from the workloads it manages.

## Structure

```
homelab-git-mgmt/
├── argocd/
│   ├── appproject.yaml              # AppProject: homelab-apps
│   └── applications/                # Application CRs (one per app)
│       ├── jellyfin.yaml
│       ├── flaresolverr.yaml
│       ├── openwrt.yaml
│       └── monitoring.yaml
├── docs/                            # Documentation
│   ├── PLAN.md
│   ├── argocd-setup.md
│   ├── app-manifests.md
│   ├── github-actions.md
│   └── rbac.md
├── .github/
│   └── workflows/
│       └── validate.yaml            # CI: validates ArgoCD YAML client-side
├── scripts/
│   ├── check_image_tags.py
│   ├── check_namespace.py
│   └── check_servicemonitor.py
└── .yamllint
```

## Relationship to homelab-apps

- This repo (`homelab-git-mgmt`) = ArgoCD's config. ArgoCD watches this repo to know what to sync.
- `homelab-apps` = the cluster's desired state. The Application CRs here point at paths in `homelab-apps` (e.g. `apps/jellyfin/`), and ArgoCD pulls the actual manifests from there.

## Bootstrap

See `docs/argocd-setup.md` for the full walkthrough.

Briefly:
1. Install ArgoCD into the cluster.
2. Apply `argocd/appproject.yaml` and the first `Application` CR manually once:
   ```bash
   kubectl apply -f argocd/appproject.yaml -n argocd
   kubectl apply -f argocd/applications/jellyfin.yaml -n argocd
   ```
3. ArgoCD picks up the Application CRs and starts syncing from `homelab-apps`.

## CI

The `validate.yaml` workflow runs on every push and PR. It checks YAML syntax, K8s resource schema (client-side, no cluster access), image tag pinning, and namespace consistency. Run locally with:

```bash
pip install yamllint pyyaml
yamllint -c .yamllint .
kubectl apply --dry-run=client -f argocd/appproject.yaml
kubectl apply --dry-run=client -f argocd/applications/
python scripts/check_image_tags.py
python scripts/check_namespace.py
```
