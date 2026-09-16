# App Manifests — HomeLab Cluster

This doc describes the manifest pattern for each app in the `homelab-apps` repo and gives concrete examples for Jellyfin, FlareSolverr, OpenWrt, and monitoring. The goal: a consistent, minimal structure per app that ArgoCD can sync, that CI can validate, and that a future admin can understand in under five minutes.

---

## 1. Workspace layout

Each app lives under `apps/<name>/` in the `homelab-apps` repo. The contents are one or both of:

- **Raw manifests** — self-contained K8s YAML files (Deployment, Service, HTTPRoute, ConfigMap, SealedSecret reference, PVC).
- **Helm values** — `values.yaml` + optional chart files, when using a chart (monitoring does this).

```
apps/
├── jellyfin/
│   ├── namespace.yaml
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── httproute.yaml          # optional — remove if the app isn't exposed
│   ├── configmap.yaml        # optional — remove if no non-secret config
│   ├── sealedsecret.yaml     # optional — remove if no secrets
│   └── pvc.yaml              # optional — remove if no persistent storage
├── flaresolverr/
│   └── ...
├── openwrt/
│   └── ...
└── monitoring/
    ├── namespace.yaml
    └── values.yaml           # Helm values for kube-prometheus-stack
```

ArgoCD's `Application` CR points at the app dir. If the app uses raw manifests, the path is `apps/<name>/`. If it uses Helm, the `Application` CR points at a Helm repo and the values file (see `docs/argocd-setup.md` — the monitoring Application CR).

---

## 2. Manifest checklist (per app)

A complete app dir contains as many of these as the app actually needs — skip what's unused rather than leaving empty placeholders.

| File | Required? | Purpose |
|---|---|---|
| `namespace.yaml` | Yes (if the app gets its own namespace) | Declares the namespace. ArgoCD can auto-create it via `CreateNamespace=true`, but committing the namespace manifest makes the desired state explicit and lets you attach labels/annotations. |
| `deployment.yaml` | Yes (for daemon/stateful apps) | The workload. Includes image, replicas, ports, resource requests/limits, volume mounts, and (for OpenWrt) host networking/privileged flags. |
|| `service.yaml` | Yes (if the app is accessed within the cluster or via HTTPRoute) | Exposes the pod ports. ClusterIP for internal, NodePort only if you need direct node access, LoadBalancer only if your cluster has an LB. |
- `HTTPRoute` resources for external access. Use `httproute.yaml` in the app dir — see `docs/gateway-api.md` for the pattern. Remove if the app is internal-only.
| `configmap.yaml` | Optional | Non-secret configuration. Remove if the app takes all config from env vars or a Helm values file. |
| `sealedsecret.yaml` | Optional | Secret delivery via SealedSecrets (the plan's v1 choice). Remove if the app has no secrets. |
| `pvc.yaml` | Optional | Persistent storage. Remove if the app is stateless. |

---

## 3. Raw manifest patterns

### 3.1 Namespace

```yaml
# apps/<name>/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: <name>
  labels:
    app.kubernetes.io/instance: <name>
    # argocd.argoproj.io/sync-wave: "-10"   # optional: create namespace before app resources
```

The `sync-wave` annotation is optional. If you want the namespace to exist before ArgoCD tries to create resources in it, set `sync-wave: "-10"` and add `ServerSideApply=true` (not `CreateNamespace=true`) in the Application CR. If you rely on `CreateNamespace=true`, you don't need the namespace manifest at all.

### 3.2 Deployment

```yaml
# apps/<name>/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: <name>
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
    app.kubernetes.io/instance: <name>
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: <name>
  strategy:
    type: Recreate      # Use RollingUpdate for apps that support it
  template:
    metadata:
      labels:
        app.kubernetes.io/name: <name>
        app.kubernetes.io/instance: <name>
    spec:
      containers:
        - name: <name>
          image: <registry>/<image>:<tag>       # pinned tag — no :latest
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8080
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          env:
            - name: SOME_CONFIG
              valueFrom:
                configMapKeyRef:
                  name: <name>-config
                  key: some-key
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: <name>-data
```

**Image tag rule:** always pin a specific tag or SHA. `:latest` is caught by CI (see `docs/github-actions.md`), and it defeats the stability goal of a homelab — you don't want an upstream push to restart your apps overnight.

**Resource requests/limits:** set both. For a homelab, `requests` should be the realistic minimum the app needs to start, and `limits` should be a hard cap that prevents one app from starving the others. When in doubt, start conservative and observe actual usage via Grafana (after monitoring is deployed).

**Replicas:** 1 for most homelab apps (Jellyfin, FlareSolverr). Increase only if the app supports it and you have a reason (e.g. high availability for a critical service). Don't set replicas > 1 for apps that aren't designed for it — you'll get conflicts over shared state.

**Strategy:** `Recreate` for apps that can't run two copies at once and don't support rolling updates cleanly. `RollingUpdate` (the default) for apps that can. When in doubt, leave it as the default (`RollingUpdate`) and only change if you hit an issue.

### 3.3 Service

```yaml
# apps/<name>/service.yaml
apiVersion: v1
kind: Service
metadata:
  name: <name>
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
    app.kubernetes.io/instance: <name>
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: <name>
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
```

Use `ClusterIP` unless you have a specific need for `NodePort` or `LoadBalancer`. The HTTPRoute (if any) fronts the `ClusterIP` service.

### 3.4 HTTPRoute

```yaml
# apps/<name>/httproute.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: <name>
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
    app.kubernetes.io/instance: <name>
spec:
  parentRefs:
    - name: homelab-gateway
      namespace: traefik
  hostnames:
    - "<name>.<your-domain>"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: <name>
          port: 80
```

Replace `<your-domain>` with your actual domain. The `parentRefs` points at the cluster's `homelab-gateway` (see `docs/gateway-api.md`). If you don't have a domain yet, use a placeholder and fill it in before first sync.

For apps that need request modifications (body-size limit, headers, rate limiting), use a Traefik `Middleware` CRD attached via the `HTTPRoute`'s `filters` field — equivalent to the old nginx annotations, different wiring.

### 3.5 ConfigMap

```yaml
# apps/<name>/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: <name>-config
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
data:
  some-key: "some-value"
  another-key: |
    multi-line
    value
```

Keep non-secret config here. If everything is passed via env vars in the Deployment, you may not need a ConfigMap at all — skip it.

### 3.6 SealedSecret

```yaml
# apps/<name>/sealedsecret.yaml
apiVersion: bitnami.com/v1alpha1
kind: SealedSecret
metadata:
  name: <name>-secrets
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
spec:
  encryptedData:
    KEY_ONE: AgBy8i...   # sealed ciphertext — produced by kubeseal
    KEY_TWO: AgBy8i...
  template:
    metadata:
      name: <name>-secrets
      namespace: <name>
    type: Opaque
```

The `encryptedData` keys are the result of running `kubeseal` (see `docs/argocd-setup.md` section 7). The `template` section tells the controller what the resulting `Secret` should look like (name, namespace, type). The namespace in the `template` must match the app namespace — otherwise the secret lands in the wrong place.

**To create a new sealed secret:**

1. Create the plaintext secret locally (dry-run, never commit it): `kubectl create secret generic <name>-secrets --dry-run=client -o yaml --from-literal=KEY_ONE=value > /tmp/plain.yaml`
2. Seal it: `kubeseal --cert=sealed-secrets-public-key.pem --format yaml < /tmp/plain.yaml > apps/<name>/sealedsecret.yaml`
3. Commit the sealed file. The plaintext is gone from your disk if you delete `/tmp/plain.yaml`.

The public key (`sealed-secrets-public-key.pem`) is stored in `homelab-git-mgmt` (see `docs/argocd-setup.md`) so anyone setting up the cluster can run `kubeseal`.

### 3.7 PVC

```yaml
# apps/<name>/pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: <name>-data
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  # storageClassName: standard   # fill in your cluster's default/storage class
```

Set `storageClassName` to your cluster's storage class if the default isn't what you want. `ReadWriteOnce` is correct for single-node access (most homelab apps). Use `ReadWriteMany` only if you have storage that supports it and the app needs concurrent access from multiple pods.

---

## 4. HTTPRoute pattern (external access)

HTTPRoute resources replace Ingress for external access. Each exposed app gets an `HTTPRoute` in its namespace that attaches to the cluster's `homelab-gateway` (see `docs/gateway-api.md`).

**Helm apps (monitoring):** the chart's built-in ingress is disabled; a standalone `HTTPRoute` in the app dir handles external access. The chart still creates the Grafana Service — the `HTTPRoute` references it by name.

**Raw-manifest apps (jellyfin):** the `HTTPRoute` is committed alongside the Deployment and Service.

Pattern (see `apps/jellyfin/httproute.yaml` and `apps/monitoring/httproute.yaml` for committed examples):

```yaml
# apps/<name>/httproute.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: <name>
  namespace: <name>
  labels:
    app.kubernetes.io/name: <name>
    app.kubernetes.io/instance: <name>
spec:
  parentRefs:
    - name: homelab-gateway
      namespace: traefik
  hostnames:
    - "<name>.<your-domain>"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: <name>
          port: 80
```

The `port` field on `backendRefs` can reference a port by **number** (as above) or by **name** if the Service uses named ports. The `parentRefs.namespace` is `traefik` because the Gateway is deployed there; the Gateway's `allowedRoutes.namespaces.from: All` permits cross-namespace attachment.

---

## 5. Concrete per-app notes

### Jellyfin

- Image: `jellyfin/jellyfin` with a pinned tag (e.g. `10.9.0`).
- Needs persistent storage for config and media — at least two PVCs if you want config and media separate, or one combined. Plan for the media path to be large.
- Media is typically mounted from the host or a NAS — decide whether to use a hostPath (not portable, but simple for a single-node homelab) or a shared PVC.
- HTTPRoute: yes, with a reasonable body-size limit via Traefik Middleware if needed (see `docs/gateway-api.md`).
- Secrets: license key and admin password via SealedSecret.
- No native Prometheus metrics — cluster-level metrics only for now (see `docs/PLAN.md`). A `ServiceMonitor` can be added later if you run an exporter.

Skeleton committed in `apps/jellyfin/`.

### FlareSolverr

- Image: `flaresolverr/flaresolverr` with a pinned tag.
- Lightweight — small resource requests.
- No persistent storage needed (cache can be ephemeral).
- HTTPRoute: yes if you expose it to other apps; internal ClusterIP service is enough if only used cluster-internally.
- Secrets: maybe a config token, via SealedSecret if needed.
- No native Prometheus metrics.

Skeleton committed in `apps/flaresolverr/`.

### OpenWrt

- This is the unusual one. OpenWrt is a router OS — it needs to see and manipulate raw packets and routing tables.
- Pattern for v1: a `Deployment` (or `Pod`) with `hostNetwork: true` and `privileged: true`.
- Host networking means the pod shares the node's network namespace — it sees the node's interfaces and can bind to host ports. This is necessary for a router.
- Privileged is necessary for OpenWrt to manage interfaces, iptables/nftables, etc.
- **Risks:** this pod has broad access to the node's network. Run it on a dedicated node if possible (via `nodeSelector` or `affinity`), or accept the risk in a homelab context where you own the nodes.
- Storage: OpenWrt's `/etc` and `/overlay` are typically persisted — use a PVC for the overlay and a ConfigMap or init container for the base config.
- No native Prometheus metrics.

Skeleton committed in `apps/openwrt/`.

### Monitoring (kube-prometheus-stack)

- Helm chart, values in `apps/monitoring/values.yaml`.
- ArgoCD `Application` CR references the chart repo, not a path in `homelab-apps`.
- No app-level manifests — the chart renders everything.
- PVC for Prometheus data — the chart can create one, or you pre-create it. The values file references a claim name `monitoring-prometheus-data`; either create that PVC in the app dir or let the chart create it (check the chart's PVC settings).
- No per-app ServiceMonitors in v1 (see `docs/PLAN.md`).

Skeleton committed in `apps/monitoring/`.

---

## 6. Adding a new app (checklist)

When you add a future app, follow this sequence:

1. Create `apps/<name>/` directory.
2. Add `namespace.yaml` if the app gets its own namespace.
3. Add `deployment.yaml` with a pinned image tag and resource requests/limits.
4. Add `service.yaml` if the app needs a service.
5. Add `httproute.yaml` if the app is exposed externally (see section 4 for the pattern).
6. Add `configmap.yaml` / `sealedsecret.yaml` / `pvc.yaml` as needed.
7. Add the `Application` CR in `homelab-git-mgmt/argocd/applications/<name>.yaml`, pointing at `apps/<name>/`.
8. Update the `AppProject`'s `destinations` if you added a new namespace (or use a wildcard — see `docs/rbac.md` for the tradeoff).
9. Push both repos. ArgoCD syncs the new app.
10. Verify in Grafana / ArgoCD UI.

If the app has native Prometheus metrics, add a `ServiceMonitor` in the app dir (pattern below) and add it to the CI check that validates ServiceMonitor-to-Service pairing.

---

## 7. ServiceMonitor pattern (for apps with native metrics)

This is a v2 capability — use it when an app exposes Prometheus metrics natively. It's documented here so you know how to add it later without opening a new doc.

```yaml
# apps/<name>/servicemonitor.yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: <name>
  namespace: <name>
  labels:
    release: kube-prometheus-stack       # matches the Prometheus helm release label
spec:
  selector:
    matchLabels:
      app.kubernetes.io/name: <name>    # matches the Service label
  endpoints:
    - port: http-metrics                 # named port on the Service
      path: /metrics
      interval: 30s
```

Requirements:

- The app's `Service` must have a named port for metrics (e.g. `http-metrics`) and the `ServiceMonitor` references that port name.
- The app must expose Prometheus-format metrics at the given path.
- The `release: kube-prometheus-stack` label must match the label the Prometheus instance selects on. If you change the Helm release name, update this label.

**Shared ServiceMonitor (optional):** if you have multiple apps in the same namespace all exposing metrics on a standard port/path, you can write one `ServiceMonitor` with a broader selector instead of one per app. This reduces boilerplate but makes the selector semantics more complex — use per-app ServiceMonitors unless you have a clear reason to share.

**CI validation:** the CI workflow (see `docs/github-actions.md`) can be extended to check that every `ServiceMonitor` has a matching `Service` in the same app dir. This catches typos in selectors before ArgoCD syncs.
