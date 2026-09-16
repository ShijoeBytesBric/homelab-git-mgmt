# ArgoCD Setup — HomeLab Cluster

This doc walks through installing ArgoCD, configuring repo access and the AppProject, and deploying apps via ArgoCD Application CRs. Everything here assumes the decisions locked in `PLAN.md`: kubeadm cluster, one GitHub org with two repos (`homelab-git-mgmt` for this code and `homelab-apps` for the deployable cluster GitOps), SealedSecrets for secrets, and SSH deploy-key access to the GitOps repo.

---

## 1. Prerequisites

- A running Kubernetes cluster (kubeadm) with a working `kubectl` context.
- `helm` v3 installed on the machine running these commands.
- The `homelab-git-mgmt` repo exists on GitHub (this repo). The `homelab-apps` repo will be created later once there's content to push.
- SSH key pair for ArgoCD to read the `homelab-apps` repo. If you don't have one:

```bash
ssh-keygen -t ed25519 -C "argocd-deploy-key" -f ~/.ssh/argocd-deploy-key -N ""
```

Add the **public** key (`~/.ssh/argocd-deploy-key.pub`) as a deploy key on the `homelab-apps` GitHub repo with **read-only** access. Keep the private key for the ArgoCD secret below.

---

## 2. Install ArgoCD

```bash
# Create namespace
kubectl create namespace argocd

# Add Helm repo
helm repo add argo https://argoproj.github.io/argo-helm
helm repo update

# Install (minimal values — no Dex, no extra features)
helm install argocd argo/argocd \
  --namespace argocd \
  --set server.service.type=ClusterIP \
  --set configs.secret.argocdServerAdminPassword="$(argocd admin initial-password -n argocd)" \
  --wait
```

If you prefer to set the admin password explicitly (replace the command above):

```bash
# Generate bcrypt hash
ARGOCD_PWD_HASH=$(htpasswd -nbBC argon2 <your-password> | tr -d ':\n' | sed 's/$2y/$2a/')

helm install argocd argo/argocd \
  --namespace argocd \
  --set server.service.type=ClusterIP \
  --set configs.secret.argocdServerAdminPassword="$ARGOCD_PWD_HASH" \
  --wait
```

Access the UI (port-forward while testing):

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443 &
# Login: admin / <password-you-set>
# https://localhost:8080
```

When you're ready to expose it permanently, add an HTTPRoute (see `docs/gateway-api.md` section 8).

---

## 3. Register the GitOps repo

ArgoCD needs to be able to read `homelab-apps`. The cleanest way for a single read-only repo is an SSH deploy key.

```bash
# Create the secret ArgoCD uses to authenticate to the repo
kubectl create secret generic argocd-repo-key \
  --namespace argocd \
  --from-file=ssh-privatekey=$HOME/.ssh/argocd-deploy-key \
  --type=kubernetes.io/ssh-auth
```

Then register the repo via the ArgoCD CR (or via `argocd` CLI — both work; the CR is what we commit to Git so it's reproducible):

```yaml
# argocd/repositories.yaml  (committed to homelab-git-mgmt argocd/ dir)
apiVersion: v1
kind: Secret
metadata:
  name: home-lab-cluster-repo
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  url: git@github.com:<YOUR_GITHUB_USERNAME>/homelab-apps.git
  sshPrivateKey: |
    <contents of ~/.ssh/argocd-deploy-key>
```

Apply it:

```bash
kubectl apply -f argocd/repositories.yaml
```

ArgoCD picks up the labeled Secret and registers the repo automatically. Verify:

```bash
argocd repo list
# should show homelab-apps with status Successful
```

If the SSH key isn't picked up, restart the repo-server pod:

```bash
kubectl rollout restart deployment/argocd-repo-server -n argocd
```

---

## 4. Create the AppProject

The `AppProject` scopes what ArgoCD is allowed to sync. One project for all homelab apps, restricted to the app namespaces and the one repo.

```yaml
# argocd/appproject.yaml  (committed to homelab-git-mgmt argocd/ dir)
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: homelab-apps
  namespace: argocd
spec:
  description: Homelab application group
  sourceRepos:
    - git@github.com:<YOUR_GITHUB_USERNAME>/homelab-apps.git
  destinations:
    - namespace: jellyfin
      server: https://kubernetes.default.svc
    - namespace: flaresolverr
      server: https://kubernetes.default.svc
    - namespace: openwrt
      server: https://kubernetes.default.svc
    - namespace: monitoring
      server: https://kubernetes.default.svc
  clusterResourceWhitelist:
    - group: ''
      kind: Namespace
  namespaceResourceBlacklist:
    - group: ''
      kind: ResourceQuota
    - group: ''
      kind: LimitRange
    - group: ''
      kind: NetworkPolicy
  selfHeal: true
  syncWave: 0
```

Apply:

```bash
kubectl apply -f argocd/appproject.yaml
```

This project:

- Only allows the `homelab-apps` repo as a source.
- Only allows sync to the four app namespaces (jellyfin, flaresolverr, openwrt, monitoring).
- Prevents ArgoCD from deleting ResourceQuotas, LimitRanges, and NetworkPolicies (so the project doesn't accidentally remove cluster guardrails).
- Has selfHeal enabled at the project level — individual apps can override this.

---

## 5. Deploy apps via Application CRs

Each app gets one `Application` CR in `argocd/applications/`. The CR points at a path in the `homelab-apps` repo and specifies the sync policy.

### Pattern (raw manifests app)

```yaml
# argocd/applications/jellyfin.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: jellyfin
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: homelab-apps
  source:
    repoURL: git@github.com:<YOUR_GITHUB_USERNAME>/homelab-apps.git
    targetRevision: main
    path: apps/jellyfin
  destination:
    server: https://kubernetes.default.svc
    namespace: jellyfin
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
      allowEmpty: false
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

### Pattern (Helm app — monitoring)

Monitoring uses the `kube-prometheus-stack` chart. The `Application` CR references the Helm repo and the values file lives in the GitOps repo. The chart's built-in ingress is disabled — external access is handled by the `HTTPRoute` in `apps/monitoring/httproute.yaml` (see `docs/gateway-api.md`).

```yaml
# argocd/applications/monitoring.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: monitoring
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: homelab-apps
  source:
    repoURL: https://prometheus-community.github.io/helm-charts
    chart: kube-prometheus-stack
    targetRevision: 60.3.0   # pin the chart version — update deliberately
    helm:
      valueFiles:
        - values.yaml
      values: |
        ingress:
          enabled: false   # Gateway API HTTPRoute handles external access (see docs/gateway-api.md)
        grafana:
          adminPassword: <set-via-sealedsecret-or-external>
          ingress:
            enabled: false   # controlled by the top-level ingress above
        prometheus:
          prometheusSpec:
            resources:
              requests:
                cpu: 200m
                memory: 512Mi
              limits:
                cpu: 1000m
                memory: 1Gi
        alertmanager:
          enabled: true
        kubeStateMetrics:
          enabled: true
        nodeExporter:
          enabled: true
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
      allowEmpty: false
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
    retry:
      limit: 5
      backoff:
        duration: 5s
        factor: 2
        maxDuration: 3m
```

### Notes on sync policy

- `CreateNamespace=true` — ArgoCD creates the destination namespace if it doesn't exist. If you manage namespaces separately (e.g. a `namespace.yaml` committed in the app dir), add `ServerSideApply=true` and remove `CreateNamespace=true` to avoid conflicts.
- `ServerSideApply=true` — uses server-side apply, which is kinder to concurrent edits and reduces merge conflicts in the live cluster state. Recommended for GitOps.
- `prune=true, selfHeal=true` — the standard hands-off policy. If you want a confirmation gate for a particular app, set `automated: { prune: true, selfHeal: false }` and sync manually.

### Deploying an Application CR

Push the Application CR to `homelab-git-mgmt` (the repo ArgoCD is watching for its own config — see the bootstrap note below), then either:

- Let the bootstrap Application (see below) sync it, or
- Apply it directly once for the first app: `kubectl apply -f argocd/applications/jellyfin.yaml -n argocd`.

Once ArgoCD has the Application CR, it takes over.

---

## 6. Bootstrap sequence

There's a chicken-and-egg problem: ArgoCD needs an Application CR to start syncing apps, but the Application CR lives in Git. The typical bootstrap is:

1. Install ArgoCD into the cluster (step 2 above).
2. Apply the `AppProject`, `Repository` secret, and the first `Application` CR **directly** with `kubectl apply` (one-time, manual).
3. ArgoCD picks up the Application and starts syncing the app from `homelab-apps`.
4. From then on, any change to the Application CR or app manifests is committed to Git, and ArgoCD reconciles the difference.

After the first app is syncing, commit the bootstrap resources (`AppProject`, `Repository` secret, and the Application CRs) into `homelab-git-mgmt` under `bootstrap/` so the setup is reproducible, but **don't create an ArgoCD Application that syncs the bootstrap dir itself** — that creates a loop. Treat bootstrap as manual-only setup documentation, or guard it with a one-time sync flag. For a homelab, the simplest approach is: document the bootstrap commands in this doc, apply them manually once, and keep the resource YAMLs in `bootstrap/` as reference only.

---

## 7. SealedSecrets for secrets

ArgoCD syncs whatever is in the app dir, including `SealedSecret` CRs. The SealedSecrets controller in the cluster decrypts them into regular `Secret` objects.

### Install the controller

```bash
kubectl create namespace sealed-secrets
helm repo add sealed-secrets https://charts.bitnami.com/bitnami
helm install sealed-secrets sealed-secrets/sealed-secrets \
  --namespace sealed-secrets \
  --wait
```

### Create a sealed secret

```bash
# Install kubeseal CLI (e.g. brew install kubeseal, or from GitHub releases)
# Seal a secret that you'd otherwise create with kubectl
kubectl create secret generic jellyfin-secrets \
  --dry-run=client -o yaml \
  --from-literal=JELLYFIN__LICENSE_KEY=abcdef123456 \
  --from-literal=JELLYFIN__ADMIN_PASSWORD=strongpassword \
  > /tmp/jellyfin-secrets.yaml

kubeseal --cert=sealed-secrets-public-key.pem --format yaml < /tmp/jellyfin-secrets.yaml > apps/jellyfin/sealedsecret.yaml
```

The `sealed-secrets-public-key.pem` is exported from the controller:

```bash
kubeseal --fetch-cert > sealed-secrets-public-key.pem
```

Commit the resulting `SealedSecret` YAML to `homelab-apps` (in the app dir). ArgoCD syncs it, the controller decrypts it, and the real `Secret` appears in the namespace. The plaintext never touches Git.

### Rotation

To rotate a secret: create a new `SealedSecret` with a new name (or overwrite the old one), commit, and let ArgoCD sync. The controller handles the update. For secrets managed outside Git (e.g. OAuth client secrets), keep the rotation process documented and use SealedSecrets as the delivery mechanism.

---

## 8. HTTPRoute for ArgoCD UI (optional)

When you're ready to expose the ArgoCD UI beyond port-forwarding, add an `HTTPRoute` in the `argocd` namespace:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: argocd-ui
  namespace: argocd
  labels:
    app.kubernetes.io/name: argocd
spec:
  parentRefs:
    - name: homelab-gateway
      namespace: traefik
  hostnames:
    - "argocd.<your-domain>"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: argocd-server
          port: 80
```

ArgoCD handles its own authentication (the admin password set at install). For a homelab this is enough; add a Traefik `Middleware` CRD for external auth or move to Dex/SSO later if needed.

---

## 9. Troubleshooting

- **App stuck in "OutOfSync":** `argocd app get <app>` shows the diff. Common causes: the Git revision doesn't match (`targetRevision` in the Application CR), the path is wrong, or the repo secret is expired.
- **Repo shows "Failed":** check the SSH key secret in `argocd` namespace; restart `argocd-repo-server` if needed.
- **Sync fails with permission error:** the ArgoCD ServiceAccount's Role in the destination namespace doesn't cover the resource being created. Check the RBAC Role for that namespace (see `docs/rbac.md`).
- **"hello-app" health check fails:** ArgoCD's default health checks work for Deployments and most resources. For custom resources, you may need a custom health check hook — rare for homelab apps.

---

## 10. What's committed where

| Resource | Committed to | Why |
|---|---|---|
| `argocd/appproject.yaml` | `homelab-git-mgmt` (argocd/) | ArgoCD config — AppProject defines the project scope |
| `argocd/repositories.yaml` | `homelab-git-mgmt` (argocd/) | ArgoCD repo registration — the SSH key Secret |
| `argocd/applications/*.yaml` | `homelab-git-mgmt` (argocd/) | ArgoCD Application CRs — the sync definitions |
| `bootstrap/*.yaml` | `homelab-git-mgmt` (bootstrap/) | One-time setup resources — reference only, applied manually |
| `apps/<name>/*` | `homelab-apps` | Deployable app manifests — what ArgoCD syncs into the cluster |
| `sealed-secrets-public-key.pem` | `homelab-git-mgmt` (bootstrap/ or root) | Public key for sealing secrets — needed to run `kubeseal` |

The `homelab-git-mgmt` repo is ArgoCD's configuration repo (ArgoCD itself, the AppProject, the Application CRs, the repo secret). The `homelab-apps` repo is the cluster's desired state (the actual app manifests). This separation is deliberate: it lets you version ArgoCD's own config independently from the apps, and it mirrors how larger GitOps setups split "infrastructure config" from "workload config."
