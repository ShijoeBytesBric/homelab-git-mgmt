# homelab-git-mgmt

ArgoCD configuration repository for the Homelab Kubernetes cluster.

This repository holds ArgoCD's own configuration — the `AppProject` that scopes
what ArgoCD can sync, the `Application` CRs that define what to deploy and where,
the RBAC roles that authorize ArgoCD in each namespace, and the repository
registration secret that lets ArgoCD read the manifest repository. It does **not**
contain the actual workload manifests — those live in
[homelab-apps](https://github.com/ShijoeBytesBric/homelab-apps).

The separation is deliberate: ArgoCD's wiring is versioned independently from the
workloads it manages, mirroring how larger GitOps setups separate infrastructure
configuration from workload configuration.


## Architecture

Two-repo ArgoCD GitOps model:

| Repository | Role | Contains |
|---|---|---|
| `homelab-git-mgmt` (this repository) | ArgoCD's configuration | AppProject, Application CRs, RBAC, repo secret, CI |
| `homelab-apps` | Cluster's desired state | Deployments, Services, PVCs, SealedSecrets, HTTPRoutes, Namespaces |

The `Application` CRs in this repository point at paths inside `homelab-apps`
(for example, `apps/jellyfin/`). ArgoCD reads those paths and syncs the manifests
into the cluster. When a change is pushed to `homelab-apps`, ArgoCD detects the new
commit and syncs automatically (automated sync with `selfHeal: true`).


## Repository structure

```
homelab-git-mgmt/
├── argocd/
│   ├── appproject.yaml              # AppProject — sync scope and permissions
│   ├── repositories.yaml           # SealedSecret — SSH key for homelab-apps access
│   ├── gatewayclass.yaml           # GatewayClass "traefik" (bootstrap)
│   ├── gateway.yaml                # Gateway "homelab-gateway" (bootstrap, ports 80/443)
│   └── applications/
│       ├── jellyfin.yaml           # → homelab-apps/apps/jellyfin
│       ├── flaresolverr.yaml       # → homelab-apps/apps/flaresolverr
│       └── monitoring.yaml         # → Helm chart kube-prometheus-stack
├── rbac/
│   ├── jellyfin-role.yaml & *-rolebinding.yaml
│   ├── flaresolverr-role.yaml & *-rolebinding.yaml
│   └── monitoring-role.yaml & *-rolebinding.yaml
├── .github/workflows/
│   └── validate.yaml               # CI: client-side manifest validation
├── scripts/
│   ├── validate_k8s.py
│   ├── check_image_tags.py
│   ├── check_namespace.py
│   └── check_servicemonitor.py     # placeholder
├── .yamllint
└── README.md
```

**`argocd/`** — ArgoCD resources. The `AppProject` defines which namespaces ArgoCD
can sync to and which repositories it can read from. The `Application` CRs tell
ArgoCD what to deploy. The `repositories.yaml` SealedSecret registers the
`homelab-apps` repository with ArgoCD over SSH. `gatewayclass.yaml` and
`gateway.yaml` are cluster infrastructure applied manually once during bootstrap.

**`rbac/`** — per-namespace `Role` and `RoleBinding` pairs. Each pair grants the
`argocd-server` ServiceAccount permission to create and update resources in one app
namespace. These are applied manually once during cluster setup — they are not synced
by ArgoCD.

**`scripts/`** — validation scripts run by CI and available for local use.


## Getting started

Brief bootstrap sequence:

1. Install ArgoCD into the cluster.
2. Apply the `AppProject` and the first `Application` CR manually once:
   ```bash
   kubectl apply -f argocd/appproject.yaml
   kubectl apply -f argocd/applications/jellyfin.yaml
   ```
3. ArgoCD picks up the Application CRs and starts syncing from `homelab-apps`.

The RBAC roles and the repository-registration SealedSecret are also applied
manually once — see the deploy guide for the complete sequence and the SealedSecret
sealing steps.

After the first application is syncing, all further changes are committed to Git and
ArgoCD reconciles the difference automatically.


## CI

The `validate` workflow runs on every push and pull request to `main`. It catches
broken YAML and misconfigurations before they reach the cluster.

| Check | Tool | Catches |
|---|---|---|
| YAML lint | `yamllint` | Indentation, tabs, missing `---`, `yes`/`no` vs `true`/`false` |
| K8s resource structure | `scripts/validate_k8s.py` | Missing required fields per resource kind |
| Image tags | `scripts/check_image_tags.py` | `:latest` or untagged images |
| Namespace consistency | `scripts/check_namespace.py` | Resource namespace does not match app directory name |

Run the same checks locally before pushing:

```bash
pip install yamllint pyyaml
yamllint -c .yamllint .
python scripts/validate_k8s.py
python scripts/check_image_tags.py
python scripts/check_namespace.py
```

CI does **not** connect to the cluster. All checks are client-side — either
`kubectl apply --dry-run=client` equivalents or pure Python analysis.


## License

This repository does not currently include a license. If you plan to use or
modify this project, check with the repository owner before doing so.
