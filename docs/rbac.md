# RBAC — HomeLab Cluster

This doc describes the RBAC setup for the homelab cluster: the cluster-level roles that let ArgoCD sync apps into their namespaces without cluster-wide access, and the ArgoCD-level RBAC that controls who can do what in the ArgoCD UI/CLI.

---

## 1. Design principles

- **Least privilege for ArgoCD:** ArgoCD needs to create/update/delete workloads in the app namespaces, but it shouldn't have cluster-wide access. Give it a `Role` per namespace, not a `ClusterRole`.
- **Namespace isolation:** each app namespace is independently scoped. ArgoCD can sync into `jellyfin` and `flaresolverr` but only because it has a `Role` in each — not because it has cluster-wide permissions.
- **Human admin:** you (the cluster admin) have `cluster-admin` for setup and troubleshooting. Nothing else needs that.
- **ArgoCD RBAC is separate:** Kubernetes RBAC controls what ArgoCD can do *in the cluster*. ArgoCD's own RBAC (`argocd-rbac-cm`) controls what users can do *in ArgoCD*. Both are documented here.

---

## 2. Cluster-level RBAC

### 2.1 ArgoCD's per-namespace applier role

Create a `Role` in each app namespace that grants ArgoCD the permissions it needs to sync apps into that namespace. Bind it to the ArgoCD ServiceAccount.

```yaml
# rbac/jellyfin-role.yaml  (one file per app namespace, or a combined file)
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: argocd-applier
  namespace: jellyfin
rules:
  - apiGroups: [""]
    resources: ["configmaps", "secrets", "services", "persistentvolumeclaims"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
  - apiGroups: ["networking.k8s.io"]
    resources: ["ingresses"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
  - apiGroups: ["bitnami.com"]    # SealedSecrets
    resources: ["sealedsecrets"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
  - apiGroups: ["monitoring.coreos.com"]   # ServiceMonitors (future)
    resources: ["servicemonitors"]
    verbs: ["get", "list", "create", "update", "patch", "delete"]
```

This Role covers the resource types that the homelab apps use: ConfigMaps, Secrets, Services, PVCs, Deployments, ReplicaSets, Ingresses, SealedSecrets, and (future) ServiceMonitors. If you add a new resource type (e.g. `StatefulSet`, `DaemonSet`, `HorizontalPodAutoscaler`), add it to this list.

**Why not `ClusterRole`?** A `ClusterRole` would let ArgoCD touch any namespace, including ones it shouldn't. A per-namespace `Role` limits the blast radius: if ArgoCD has a bug or a misconfigured sync, it can only affect the namespaces it has Roles in.

**Why `patch` in addition to `update`?** ArgoCD uses both. `patch` is for strategic merge patches (common in syncs), `update` is for full replacements. Both are needed for a smooth sync.

### 2.2 RoleBinding per namespace

Bind the Role to the ArgoCD ServiceAccount.

```yaml
# rbac/jellyfin-rolebinding.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: argocd-applier
  namespace: jellyfin
subjects:
  - kind: ServiceAccount
    name: argocd-server
    namespace: argocd
roleRef:
  kind: Role
  name: argocd-applier
  apiGroup: rbac.authorization.k8s.io
```

This binds the `argocd-server` ServiceAccount (which the ArgoCD server pod uses) to the `argocd-applier` Role in the `jellyfin` namespace. Repeat for each app namespace (`flaresolverr`, `openwrt`, `monitoring`).

**Why bind to `argocd-server` and not a dedicated applier SA?** The `argocd-server` SA is what the ArgoCD server uses to talk to the Kubernetes API. Using it directly is simpler for a homelab. For a larger setup, you'd create a dedicated `argocd-applier` SA and use impersonation, but that's not necessary here.

**Alternative: let ArgoCD use your admin credentials.** If you're the only user and you're comfortable with it, ArgoCD can use your `kubectl` config (which has `cluster-admin`) to sync apps. This is the "delegated auth" mode — ArgoCD uses your kubeconfig. It's simpler (no per-namespace Roles needed), but it means ArgoCD has your admin permissions. For a homelab this is acceptable, but the per-namespace Role approach is more correct and documents what ArgoCD actually needs. Pick one — the Role approach is what we build here.

### 2.3 Human admin

No action needed — your `kubectl` context (the one you use to set up the cluster) carries `cluster-admin` via the kubeadm setup. If you have other admins, add them via `ClusterRoleBinding` to `cluster-admin` or a more restricted admin role.

### 2.4 Read-only access (optional)

For a partner or family member who should see but not change things:

```yaml
# rbac/readonly-rolebinding.yaml  (per namespace, or use the built-in `view` ClusterRole)
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: readonly
  namespace: jellyfin
subjects:
  - kind: User
    name: partner@example.com
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: view
  apiGroup: rbac.authorization.k8s.io
```

The built-in `view` ClusterRole gives read-only access to most resources in the namespace. Bind it per namespace for the namespaces you want to share. Skip this until you actually need to share access.

---

## 3. ArgoCD-level RBAC

ArgoCD has its own RBAC, controlled by the `argocd-rbac-cm` ConfigMap. This controls what users can do in the ArgoCD UI and CLI — not what they can do in Kubernetes.

### 3.1 Default: single admin

For a single-user homelab, the default ArgoCD setup (one admin user, full access) is enough. No `argocd-rbac-cm` needed — the admin user you set during install has full access.

### 3.2 Adding project-scoped access

If you want to share read-only or limited access later, configure `argocd-rbac-cm`:

```yaml
# argocd/argocd-rbac-cm.yaml  (committed to homelab-git-mgmt argocd/)
apiVersion: v1
kind: ConfigMap
metadata:
  name: argocd-rbac-cm
  namespace: argocd
data:
  policy.default: role:none
  policy.csv: |
    # Admin: full access to homelab-apps project
    p, role:homelab-admin, applications, *, homelab-apps/*, allow
    p, role:homelab-admin, projects, get, homelab-apps, allow
    p, role:homelab-admin, repositories, get, *, allow
    p, role:homelab-admin, clusters, get, *, allow
    p, role:homelab-admin, logs, get, homelab-apps/*, allow
    g, admin-user, role:homelab-admin

    # Read-only: can view apps and pods, cannot sync or change
    p, role:homelab-viewer, applications, get, homelab-apps/*, allow
    p, role:homelab-viewer, applications, overrides, homelab-apps/*, deny
    p, role:homelab-viewer, projects, get, homelab-apps, allow
    p, role:homelab-viewer, logs, get, homelab-apps/*, allow
    g, viewer-user, role:homelab-viewer
```

**How this works:**

- `policy.default: role:none` — by default, users have no access. Only explicitly granted roles work.
- `p, ...` lines define permissions: `p, <role>, <resource>, <action>, <scope>, <allow/deny>`.
- `g, <user>, <role>` lines assign roles to users.
- The `homelab-admin` role can sync, override, and view all apps in the `homelab-apps` project.
- The `homelab-viewer` role can view apps and logs but cannot sync or override.

**User identity:** ArgoCD authenticates users via the configured auth provider (basic auth by default, Dex/SSO later). The `g, <user>, <role>` lines match the username from that provider. For basic auth, the username is the one you set in the `argocd-users` ConfigMap or the admin user.

**When to add this:** when you actually need to share ArgoCD access with someone. For v1 (single admin), skip it.

---

## 4. What's built

The RBAC resources committed to `homelab-git-mgmt`:

```
rbac/
├── jellyfin-role.yaml
├── jellyfin-rolebinding.yaml
├── flaresolverr-role.yaml
├── flaresolverr-rolebinding.yaml
├── openwrt-role.yaml
├── openwrt-rolebinding.yaml
├── monitoring-role.yaml
├── monitoring-rolebinding.yaml
└── README.md          # explains the pattern and how to add a new namespace
```

Plus, optionally:

```
argocd/
└── argocd-rbac-cm.yaml   # only if you need ArgoCD-level RBAC beyond the default admin
```

The `argocd-rbac-cm` is not committed in v1 — single admin is the default. Add it when you need to share.

---

## 5. Adding a new namespace

When you add a new app with its own namespace:

1. Create `rbac/<name>-role.yaml` with the resource types the app uses (copy from an existing role and adjust if needed).
2. Create `rbac/<name>-rolebinding.yaml` binding it to `argocd-server`.
3. Update the `AppProject`'s `destinations` in `argocd/appproject.yaml` to include the new namespace.
4. Commit both. Apply the RBAC resources once manually (they're not synced by ArgoCD — ArgoCD doesn't have permission to create Roles yet, and it doesn't need to; these are cluster setup resources).
5. Push the updated `AppProject`. ArgoCD picks up the new destination.

**Optional: wildcard destination.** Instead of listing every namespace in the `AppProject`, you can use a wildcard:

```yaml
destinations:
  - namespace: "*"
    server: https://kubernetes.default.svc
```

This lets ArgoCD sync into any namespace, which is simpler but less restrictive. The per-namespace Roles still limit what ArgoCD can do (it can only touch namespaces where it has a Role), so the wildcard in the `AppProject` is mostly a convenience. For a homelab with a handful of namespaces, listing them explicitly is clearer and more secure. For a larger setup with many namespaces, the wildcard + per-namespace Roles is a reasonable middle ground. We list explicitly in v1.

---

## 6. Troubleshooting RBAC

- **Sync fails with "Permission denied" or "Forbidden":** the `Role` in the destination namespace doesn't cover the resource being created. Check the Role's `rules` — does it include the resource kind and API group? Add it if missing.
- **ArgoCD can't create a `Namespace`:** the `AppProject`'s `clusterResourceWhitelist` includes `Namespace` (so ArgoCD can create namespaces if `CreateNamespace=true` is set and the Role allows it). If you use pre-created namespaces, remove `CreateNamespace=true` from the Application CR and the `Namespace` from the whitelist — ArgoCD doesn't need to create namespaces then.
- **"Unknown resource kind" in sync:** the `Role`'s `apiGroups` or `resources` doesn't match the resource being synced. Check the API group of the resource (e.g. `networking.k8s.io/v1` Ingress → `apiGroups: ["networking.k8s.io"]`).
