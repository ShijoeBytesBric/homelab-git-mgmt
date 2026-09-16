# Gateway API Migration — HomeLab Cluster

## Context

The community `ingress-nginx` controller was retired in March 2026. The Kubernetes project has frozen the Ingress API and points users toward the [Gateway API](https://gateway-api.sigs.k8s.io/) as the successor. This doc captures the plan to migrate the homelab from plain `Ingress` + Traefik to Gateway API + Traefik.

**Decision:** Migrate to Gateway API. Traefik serves as the Gateway API data plane (it implements the `gateway.networking.k8s.io` CRDs). Plain Ingress resources are retired from the project.

**Scope:** Jellyfin and monitoring have Ingress/ingress resources today. FlareSolverr has no ingress (internal-only). OpenWrt has no ingress (hostNetwork). ArgoCD UI ingress is documented but not yet deployed — it will be migrated at the same time.

**Out of scope:** Cert-manager / automatic TLS is noted as a follow-up. This migration uses plain HTTP listeners first; TLS is added once a real domain and ACME challenge are in place.

---

## Architecture

```
Internet/cluster traffic
  │
  ▼
┌─────────────────────────────────────────────┐
│  Traefik (Helm chart, hostPort 80/443)     │
│  - Implements Gateway API control plane     │
│  - Listens on Gateway's HTTP/HTTPS ports    │
└──────────┬──────────────────────────────────┘
           │ Gateway API (gateway.networking.k8s.io)
           │
           ├── GatewayClass "traefik"  (cluster-scoped, 1 per cluster)
           ├── Gateway "homelab-gateway" (cluster-scoped, 1 per cluster)
           │     - listener :80  → HTTP
           │     - listener :443 → HTTPS (TLS termination, added later)
           │
           └── HTTPRoute (per app, namespaced)
                 - jellyfin/httproute.yaml    → jellyfin Service :80
                 - monitoring/httproute.yaml  → monitoring-grafana Service :80
```

The Gateway is the cluster's single entry point. Each app gets an `HTTPRoute` that attaches to the Gateway and routes matching requests to the app's Service. This replaces the one-`Ingress`-per-app model.

---

## Files

### New (cluster infrastructure — homelab-git-mgmt)

| File | Purpose |
|---|---|
| `argocd/gatewayclass.yaml` | `GatewayClass` named `traefik`. Cluster-scoped. Applied manually as bootstrap. |
| `argocd/gateway.yaml` | `Gateway` named `homelab-gateway`. Listens on ports 80/443. Allows routes from all namespaces. Applied manually as bootstrap. |

### New (app routing — homelab-apps)

| File | Purpose |
|---|---|
| `apps/jellyfin/httproute.yaml` | Routes `jellyfin.<domain>` → `jellyfin` Service :80 |
| `apps/monitoring/httproute.yaml` | Routes `monitoring.<domain>` → `monitoring-grafana` Service :80 |

### Removed

| File | Reason |
|---|---|
| `apps/jellyfin/ingress.yaml` | Replaced by `httproute.yaml` |

### Modified

| File | Change |
|---|---|
| `docs/deploy-guide.md` | Step 3: add GatewayClass + Gateway install after Traefik Helm install |
| `homelab-git-mgmt/docs/PLAN.md` | Decision #4: Gateway API + Traefik (replaces Ingress + Traefik) |
| `homelab-git-mgmt/docs/app-manifests.md` | Section 3.4: replace Ingress example with HTTPRoute example |
| `homelab-git-mgmt/docs/argocd-setup.md` | Section 8: ArgoCD UI ingress → HTTPRoute example |
| `apps/monitoring/values.yaml` | `ingress.enabled: false` (chart no longer creates an Ingress) |
| `argocd/applications/monitoring.yaml` | Helm values: `ingress.enabled: false` |

---

## Migration steps

### 1. Install Traefik (same as before, Gateway API CRDs included)

The Traefik Helm chart includes Gateway API CRDs by default (`crds.enabled: true`). No separate CRD install needed for a standard cluster.

```bash
helm repo add traefik https://traefik.github.io/charts
helm repo update

helm install traefik traefik/traefik \
  --namespace traefik \
  --create-namespace \
  --set ports.web.hostPort=80 \
  --set ports.websecure.hostPort=443 \
  --wait
```

Verify the Gateway API CRDs are present:

```bash
kubectl get crd gatewayclasses.gateway.networking.k8s.io
kubectl get crd gateways.gateway.networking.k8s.io
kubectl get crd httproutes.gateway.networking.k8s.io
```

### 2. Apply GatewayClass + Gateway (bootstrap, manual)

```bash
kubectl apply -f homelab-git-mgmt/argocd/gatewayclass.yaml
kubectl apply -f homelab-git-mgmt/argocd/gateway.yaml
```

Wait for the Gateway to be ready:

```bash
kubectl wait --namespace traefik \
  --for=condition=Ready gateway/homelab-gateway \
  --timeout=120s
```

### 3. Convert Jellyfin Ingress → HTTPRoute

Delete `apps/jellyfin/ingress.yaml`, create `apps/jellyfin/httproute.yaml` (see app-manifests.md for the pattern). Commit both repos.

### 4. Disable monitoring chart ingress, add HTTPRoute

Set `ingress.enabled: false` in `apps/monitoring/values.yaml` and in the monitoring Application CR's Helm values. Create `apps/monitoring/httproute.yaml` targeting the `monitoring-grafana` Service.

The chart still creates the Grafana Service regardless of ingress settings — the HTTPRoute references it by name.

### 5. Update docs

- `app-manifests.md`: new HTTPRoute pattern (section 3.4), note that Ingress is retired from the project
- `argocd-setup.md`: ArgoCD UI example as HTTPRoute
- `PLAN.md`: Decision #4 updated
- `deploy-guide.md`: Step 3 updated with GatewayClass + Gateway

### 6. Verify

```bash
# Gateway is ready
kubectl get gateway homelab-gateway -n traefik

# HTTPRoutes are accepted
kubectl get httproute -A

# Traffic flows
curl -H "Host: jellyfin.<your-domain>" http://localhost
curl -H "Host: monitoring.<your-domain>" http://localhost
```

---

## HTTPRoute pattern (per app)

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

Replace `<name>` and `<your-domain>`. The `parentRefs` points at the cluster's Gateway (cross-namespace reference, allowed by the Gateway's `allowedRoutes.namespaces.from: All`).

For apps that need a body-size limit or other request modifications, use a Traefik `Middleware` CRD attached via `HTTPRoute` rules' `filters` — same capability as the old nginx annotations, different wiring.

---

## TLS (follow-up, not blocking)

The Gateway starts with an HTTP listener on port 80. To add HTTPS:

1. Get a domain and point it at the cluster (or use `localtest.me` / `nip.io` for testing).
2. Enable Traefik's ACME (Let's Encrypt) via Helm values:

```bash
helm upgrade traefik traefik/traefik \
  --namespace traefik \
  --set ports.websecure.hostPort=443 \
  --set tls.enabled=true \
  --set tls.acme.enabled=true \
  --set tls.acme.email=<your-email> \
  --set tls.acme.httpChallenge.entryPoint=web \
  --wait
```

3. Add a TLS listener to the Gateway with an `certificate` ref, or let Traefik handle ACME automatically via its `IngressRoute` / `TLSOption` CRDs.

For a homelab without a real domain, skip TLS and use HTTP until a domain is available. The routing works either way.

---

## Relationship to plain Ingress

Traefik supports both Ingress and Gateway API at the same time. During migration, you can run both — old Ingress resources keep working while new HTTPRoutes are added. Once all apps are on HTTPRoute, the Ingress resources can be removed. This homelab migrates in one shot (Jellyfin + monitoring), so there's no mixed period.

The Gateway API is the direction Kubernetes is investing in. The Ingress API is frozen. New homelab work should use Gateway API. This migration puts the project on that path.

---

## Risks / notes

- **Service name for Grafana:** the kube-prometheus-stack chart creates the Grafana Service as `<release>-grafana`. With ArgoCD's default release name = Application name, that's `monitoring-grafana`. Verify with `kubectl get svc -n monitoring` after the chart syncs, and adjust the HTTPRoute's `backendRefs.name` if it differs.
- **Gateway namespace:** the Gateway is deployed in the `traefik` namespace. HTTPRoutes reference it cross-namespace via `parentRefs.namespace`. The Gateway's `allowedRoutes.namespaces.from: All` permits this. Tighten to specific namespaces later if desired.
- **Body-size limit:** Traefik's Gateway API support doesn't have a direct `proxy-body-size` annotation equivalent on `HTTPRoute`. Use a Traefik `Middleware` CRD (`Buffering` or request headers middleware) if needed, or set it at the Gateway/listener level. For Jellyfin's 100m limit, this is a follow-up if uploads fail — not blocking the initial migration.
- **FlareSolverr:** no ingress today, no change. If exposed later, use an `HTTPRoute` following the same pattern.
- **OpenWrt:** hostNetwork pod, no ingress. No change.
