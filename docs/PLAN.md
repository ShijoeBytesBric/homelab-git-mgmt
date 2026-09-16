# HomeLab Kubernetes Cluster — Plan

## Context

Deploy homelab apps (Jellyfin, FlareSolverr, OpenWrt, and similar) onto a Kubernetes cluster. App configurations live in a separate Git repo and are continuously reconciled by ArgoCD. GitHub Actions guardrails catch misconfigurations before they reach the cluster. RBAC limits what each component and user can do. Keep it minimal and maintainable — no extra abstraction layers.

---

## Architecture Overview

```
┌──────────────────────────┐
│  GitHub: app-configs repo│   ← Helm charts / K8s YAML per app
│  (separate from cluster) │   ← PRs gated by CI
└─────────────┬────────────┘
              │ (ArgoCD watches this repo)
              ▼
┌──────────────────────────┐
│  ArgoCD (in-cluster)     │   ← Auto-sync or manual sync per app
│  - AppProject per group  │   ← RBAC-scoped (see below)
│  - Source: git repo      │
└─────────────┬────────────┘
              │
              ▼
┌──────────────────────────┐
│  Kubernetes cluster      │
│  - jellyfin namespace    │
│  - flaresolverr ns       │
│  - openwrt ns            │
│  - argocd ns (bootstrap) │
│  - monitoring ns         │
└──────────────────────────┘
```

Bootstrap path: install ArgoCD first → configure AppProject + repos → create app manifests → enable auto-sync.

---

## Components

### 1. ArgoCD

- Install via the official Helm chart into its own namespace (`argocd`).
- **Repo access:** one (or more) Git repositories registered as `Repository` CRDs. The app-configs repo is the primary one. Use a deploy key (SSH) or HTTPS token — SSH deploy key is simpler for a single-read-only repo.
- **AppProject:** create one `AppProject` per logical group (e.g. `homelab-apps`). Scope it to the namespaces where apps actually run. Restrict `source.repos` to the app-configs repo only.
- **Application CRs:** one `Application` per app. Each points at a path in the app-configs repo (e.g. `apps/jellyfin/`). Sync policy: `automated` with `prune=true` and `selfHeal=true` for hands-off operation; or `manual` if you want a confirmation gate — pick per app.
- **UI / CLI:** expose the ArgoCD UI behind an HTTPRoute (basic auth or Dex later if needed). `argocd` CLI available for ad-hoc troubleshooting.

### 2. App Configuration Repo (`app-configs`)

Structure:

```
app-configs/
├── apps/
│   ├── jellyfin/
│   │   ├── Chart.yaml          # or raw manifests
│   │   ├── values.yaml         # app-specific values
│   │   └── templates/          # if using a chart
│   ├── flaresolverr/
│   │   └── ...
│   ├── openwrt/
│   │   └── ...
│   └── ...
├── argocd/
│   ├── applications/           # Application CRs (or use ApplicationSet)
│   │   ├── jellyfin.yaml
│   │   ├── flaresolverr.yaml
│   │   └── ...
│   └── appproject.yaml         # AppProject definition
├── helm-repos/                 # (if needed) remote chart references
└── README.md
```

Choose one of:

- **Raw manifests** — simplest. Each app dir is a self-contained set of K8s YAMLs (Deployment, Service, HTTPRoute, ConfigMap, Secret references). ArgoCD applies them directly.
- **Helm** — if apps already have charts (e.g. Jellyfin has a community Helm chart), use `source.helm` in the Application. Values live in `values.yaml` in the app dir, and ArgoCD renders the chart.

Recommendation: **start with raw manifests** for apps that don't have a good chart, and **Helm** where a chart saves effort. No need for a custom umbrella chart.

### 3. App Manifests — What Each App Needs

A minimal per-app set:

- `Namespace` (if not shared)
- `Deployment` + `Service`
- `HTTPRoute` (if exposed) — see `docs/gateway-api.md`
- `ConfigMap` for non-secret config
- `Secret` reference for credentials (create externally or via SealedSecrets/ExternalSecrets later — out of scope for v1)
- `PVC` if the app needs persistent storage
- Resource `requests/limits` — important for a homelab to avoid noisy neighbors

Example: Jellyfin

```
apps/jellyfin/
├── namespace.yaml
├── deployment.yaml       # image: jellyfin/jellyfin, ports, resources
├── service.yaml          # ClusterIP or NodePort
├── httproute.yaml        # optional — Gateway API routing, see docs/gateway-api.md
├── configmap.yaml        # jellyfin config overrides
├── pvc.yaml              # /config, /data mounts
└── values.yaml           # if Helm
```

OpenWrt is unusual — it runs as a VM-like workload. Use a `Deployment` with the OpenWrt image, possibly with `privileged: true` and host networking if it needs to act as a router, or run it as a `Pod` with specific CNI setup. Flag this as something to validate early.

### 4. GitHub Actions — Misconfiguration Detection

CI runs on every push to the app-configs repo. The goal is to catch obvious mistakes **before** ArgoCD tries to apply them.

#### What CI checks

| Check | Tool | Why |
|---|---|---|
| YAML/JSON syntax validity | `yamllint`, built-in `kubectl --dry-run=client` | Catch malformed YAML early |
| Kubernetes resource validation (client-side) | `kubectl apply --dry-run=client -f <file>` | Catches API schema errors without a server |
| Namespace scoping check (lightweight) | Custom script: every namespaced resource has a `namespace` field matching the app dir | Prevents cross-app collisions |
| Image tag pinning check | Custom script: warn if `latest` or no tag is used | Homelab stability — pinned tags avoid surprise restarts |
| Required fields present | Custom script per app type: Deployment has `replicas` or `strategy`, Service has `ports`, HTTPRoute has `host` | Basic completeness check |
| ArgoCD Application CR validation | `kubectl --dry-run=client` on the argocd/ dir | Catches broken ArgoCD refs |

#### CI pipeline outline

```yaml
# .github/workflows/validate.yaml
name: validate
on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-kubectl@v3
      - name: Install yamllint
        run: pip install yamllint
      - name: Lint YAML
        run: yamllint -c .yamllint apps/ argocd/
      - name: Client-side K8s validation
        run: |
          for f in $(find apps/ -name '*.yaml'); do
            kubectl apply --dry-run=client -f "$f" || exit 1
          done
      - name: Validate ArgoCD Application CRs
        run: |
          for f in argocd/applications/*.yaml; do
            kubectl apply --dry-run=client -f "$f" || exit 1
          done
      - name: Image tag pin check
        run: python scripts/check_image_tags.py  # custom, warn on :latest
      - name: Namespace consistency check
        run: python scripts/check_namespace.py  # custom, each resource has correct ns
```

Keep scripts lightweight — a few dozen lines each in `scripts/`. No external services, no cluster access needed from CI (that's the point: client-side only).

#### What CI does NOT do

- No cluster connection (you can't reach the homelab cluster from GitHub Actions without an agent — out of scope).
- No integration tests (no `kind`/`minikube` in CI — over-engineering for this use case).
- No policy engine (no OPA/Gatekeeper in CI — too heavy right now).

### 5. RBAC

RBAC in this plan has two layers: **cluster-level** (who can do what in Kubernetes) and **ArgoCD-level** (who can do what in ArgoCD).

#### Cluster-level RBAC

| Subject | Role | Scope | Purpose |
|---|---|---|---|
| `argocd-server` ServiceAccount | `argocd-server` role (built-in) | `argocd` namespace | ArgoCD needs to read/write Application CRs and apply manifests in target namespaces |
| ArgoCD's applier | ClusterRole bound per namespace | Target app namespaces | ArgoCD syncs apps — it needs `get/create/update/delete` on typical workload resources in each app namespace. Use a dedicated `Role` per namespace, not cluster-wide, to limit blast radius. |
| Human admin | `cluster-admin` or admin `ClusterRole` | Cluster | You, for setup and troubleshooting |
| Read-only viewer | `view` ClusterRole | Cluster or per-namespace | Optional: family members or CI read-only access |

The ArgoCD namespace access pattern: create a `Role` per app namespace with the resources ArgoCD needs to manage (Deployment, Service, HTTPRoute, ConfigMap, Secret, PVC, etc.), then bind it with a `RoleBinding` to the `argocd-server` ServiceAccount (or a dedicated `argocd-applier` SA if you prefer). This keeps ArgoCD from touching namespaces it shouldn't.

#### ArgoCD-level RBAC (optional, v1 scope)

ArgoCD has its own RBAC via `argocd-rbac-cm` ConfigMap. For a homelab:

- Start with a single admin user (basic auth or the default `admin` with a changed password).
- Add `argocd-rbac-cm` policies later only if you need to share access (e.g. read-only for a partner).
- Don't set up Dex/SSO in v1 — that's over-engineering for a single-user homelab. Add when you actually need it.

Example minimal `argocd-rbac-cm`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-rbac-cm
  namespace: argocd
data:
  policy.default: role:none
  policy.csv: |
    p, role:admin, applications, *, argocd/homelab-apps/*, allow
    p, role:admin, projects, get, homelab-apps, allow
    g, admin-user, role:admin
```

### 6. Bootstrapping Sequence

1. Provision the cluster (already done — this plan assumes an existing cluster).
2. Install ArgoCD via Helm into `argocd` namespace.
3. Change the ArgoCD admin password; optionally expose the UI via HTTPRoute.
4. Create the `app-configs` repo on GitHub.
5. Add the repo as an ArgoCD `Repository` (SSH deploy key or HTTPS token).
6. Create the `AppProject` CR in ArgoCD (`homelab-apps`).
7. Push the first app to the repo. **Recommendation:** deploy monitoring (kube-prometheus-stack) first so you have cluster visibility before adding more apps — or deploy it alongside the first app. Start with one non-critical app, e.g. FlareSolverr, with its `Application` CR under `argocd/applications/`.
8. ArgoCD syncs it — verify it runs.
9. Add CI workflow to the repo.
10. Add remaining apps one at a time, verifying each.

### 7. Monitoring (Prometheus + Grafana)

Add cluster-level monitoring with the `kube-prometheus-stack` Helm chart. This is the minimal path that gives you a working dashboard and cluster health visibility without building Prometheus + Grafana by hand.

**What you get:**
- Prometheus scraping node-exporter (per-node CPU, memory, disk) and kube-state-metrics (cluster object states).
- Grafana with the built-in "Kubernetes cluster monitoring" dashboard already imported.
- Alertmanager included (no alert routes configured in v1 — see below).

**What you don't do in v1:**
- No custom ServiceMonitor CRs per app — cluster metrics only. Add per-app monitoring when you care about a specific app's metrics.
- No alert routes (email, Slack, PagerDuty) — Alertmanager runs but sends nowhere. Add routes when you want actual alerts.
- No long-term retention / remote write / Thanos / Mimir — metrics live on a PVC in the cluster. If the PVC is lost (node failure, no storage), historical metrics are gone. Acceptable for v1.
- No dashboard-as-code — use the pre-installed Kubernetes dashboard; import others via the Grafana UI. No JSON provisioning in v1.

**Deployment:**
- Namespace: `monitoring`.
- Chart: `prometheus-community/kube-prometheus-stack`.
- Values to set (in `apps/monitoring/values.yaml`):
  - `grafana.adminPassword` (or leave blank and read the auto-generated one from the Secret).
  - External access is via `HTTPRoute` in `apps/monitoring/httproute.yaml` — the chart's built-in ingress is disabled (see that file).
  - `prometheus.prometheusSpec.resources` — set requests/limits (e.g. 256Mi–512Mi memory). The chart defaults can be heavy for a small homelab; size this to your cluster.
  - `alertmanager.enabled` — leave `true` (bundle default) or set `false` if you don't want it at all. No routes configured either way in v1.
  - `kubeStateMetrics.enabled=true` (default) and `nodeExporter.enabled=true` (default) — keep both.
- ArgoCD `Application` CR points at `apps/monitoring/` and uses `source.helm` with the chart reference + values.

**Resource note:** `kube-prometheus-stack` is not tiny — it runs Prometheus, Grafana, Alertmanager, the operator, kube-state-metrics, and node-exporter. On a very small cluster (e.g. under ~4Gi total RAM), weigh whether the visibility is worth the memory. If it's too heavy, the fallback is a single manual Prometheus + Grafana pod pair, but that loses the pre-built dashboards and operator management — only do that if the chart doesn't fit.

**RBAC extension:** The ArgoCD per-namespace `Role` gains the `monitoring` namespace. The `AppProject`'s `destinations` adds `monitoring` namespace. No new RBAC machinery.

**CI:** No new CI needed — the existing workflow already runs `kubectl --dry-run=client` and image-tag checks over every file under `apps/`, which covers the monitoring manifests automatically.

### 8. What's Out of Scope (for now)

- **SealedSecrets / ExternalSecrets** — handle secrets manually for v1; add a secrets manager when the number of credentials grows.
- **Ingress controller** — assume one is already running (Traefik + Gateway API, etc.). If not, that's a prerequisite step, not part of this plan. See `docs/gateway-api.md` for the full Gateway API plan.
- **Cert-manager / TLS** — use the ingress controller's default cert handling or manually managed certs in v1.
- **Alerting pipeline** — Alertmanager is included in the chart but no alert routes (email, Slack, PagerDuty) are configured in v1. Add routes when you need actual alerts.
- **App-level ServiceMonitors** — Prometheus scrapes cluster metrics by default. Per-app ServiceMonitor CRs come later when you care about app-specific metrics.
- **Long-term metrics retention / remote storage** — metrics are stored on a PVC in the cluster. No Thanos, Mimir, or remote write in v1.
- **Grafana dashboard-as-code provisioning** — use the built-in Kubernetes dashboard; import others via the Grafana UI. No JSON dashboard provisioning in v1.
- **Backup / disaster recovery** — Velero or similar when the cluster's value justifies it.
- **Multi-cluster ArgoCD** — single cluster only.
- **ApplicationSet** — use plain `Application` CRs for v1; switch to ApplicationSet if you end up with many similar apps.
- **OPA / Gatekeeper / Kyverno** — policy-as-code is useful but not necessary at this scale.

---

## File Layout (what we'll create)

```
homelab-apps/
├── docs/
│   ├── PLAN.md              # this file
│   ├── argocd-setup.md      # ArgoCD install + repo + AppProject + Application walkthrough
│   ├── app-manifests.md     # manifest patterns + per-app examples (Jellyfin, FlareSolverr, OpenWrt)
│   ├── github-actions.md    # CI workflow + scripts reference
│   └── rbac.md              # RBAC role/rolebinding manifests + ArgoCD RBAC config
├── argocd/
│   ├── appproject.yaml
│   └── applications/
│       ├── jellyfin.yaml
│       ├── flaresolverr.yaml
│       └── openwrt.yaml
├── apps/
│   ├── jellyfin/
│   │   └── ... (manifests or chart values)
│   ├── flaresolverr/
│   │   └── ...
│   ├── openwrt/
│       └── ...
│   └── monitoring/
│       └── ... (kube-prometheus-stack values + HTTPRoute)
├── .github/
│   └── workflows/
│       └── validate.yaml
├── scripts/
│   ├── check_image_tags.py
│   └── check_namespace.py
└── README.md
```

---

## Decisions (locked)

These were either answered by the user or selected as the industry-standard practice for a kubeadm homelab. They are recorded here so the manifests and docs are built against a fixed baseline.

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Cluster type / CNI | **kubeadm** (user-provided). CNI: assume a running CNI (Calico, Flannel, etc.) — manifests are CNI-agnostic. | kubeadm is self-hosted; storage class and ingress are cluster-specific. |
| 2 | OpenWrt networking | **`hostNetwork: true` + `privileged: true` Pod/Deployment**, running on a dedicated node if possible. | OpenWrt is a router — it needs to see/manipulate raw traffic and routing tables. Host networking is the common homelab pattern for router pods. If node isolation matters, pin it to a dedicate node via `nodeSelector`/`affinity`. Flag: validate early — this is the one manifest that isn't portable. |
| 3 | Secrets v1 | **SealedSecrets** (Bitnami SealedSecrets controller). Commit encrypted Secrets to the Git repo; the controller decrypts them in-cluster. | Fits the GitOps model (everything in Git, encrypted at rest in the repo). Industry standard for ArgoCD-based GitOps. No external secrets store needed in v1. |
| 4 | Ingress controller | **Traefik + Kubernetes Gateway API** (Traefik Helm chart as the data plane, Gateway API CRDs for routing). Install Traefik + GatewayClass + Gateway as a prerequisite before app HTTPRoutes are created. | Gateway API is the Kubernetes project's direction (Ingress API is frozen). Traefik implements the Gateway API and is actively maintained — unlike the retired community ingress-nginx. Covers host/path routing, TLS (with ACME built-in), and future Gateway API features (header matching, weighted traffic splitting). If one is already running, skip this — it's only a prerequisite when absent. |
| 5 | Cluster RAM / node count | **Proceed with `kube-prometheus-stack` as-is.** Assumes a typical homelab with ≥2 nodes and enough aggregate RAM for the stack (~500Mi–1Gi for Prometheus). | If the cluster is very small (e.g. single node <4Gi), flag it and we'll trim `prometheus.prometheusSpec.resources` or fall back to a lighter Prometheus + Grafana pair. For now, build against the chart defaults with sane resource caps. |

### Open Questions (remaining — nothing blocks building)

None of the above are open anymore. The only thing left to confirm is the **app-configs repo location** (GitHub repo name / URL) and whether you want the ArgoCD repo access to use an SSH deploy key or an HTTPS token — both work, SSH is simpler for a single read-only repo. I'll pick SSH deploy key unless you say otherwise.
