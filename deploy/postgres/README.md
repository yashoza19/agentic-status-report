# Postgres via Crunchy Postgres Operator (PGO)

Use this instead of a hand-rolled Postgres Deployment. PGO handles OpenShift
restricted SCC, storage, and credentials.

## 1. Install the operator

Install **Crunchy Postgres for Kubernetes** from OperatorHub (OLM) in your cluster.
Use the default channel/version your cluster supports (PGO v5.x).

Confirm the operator is running:

```bash
export KUBECONFIG=/path/to/cluster-bot-dev-kubeconfig.yaml
oc get pods -n openshift-operators | grep postgres
# or the namespace where you installed PGO
```

## 2. Create the database cluster

```bash
oc project weekly-status
oc apply -f deploy/postgres/postgrescluster.yaml
```

Wait until the cluster is ready:

```bash
oc get postgrescluster weekly-status-db -n weekly-status
oc get pods -n weekly-status -l postgres-operator.crunchydata.com/cluster=weekly-status-db
# All pods should be Running (primary + optional sidecars)
```

PGO creates:

| Resource | Name |
|----------|------|
| Cluster | `weekly-status-db` |
| Primary Service | `weekly-status-db-primary` (in-cluster hostname) |
| App user secret | `weekly-status-db-pguser-weeklystatus` |
| Database | `weekly_status` |

## 3. Build `DATABASE_URL` for `deploy/secrets.yaml`

From inside the cluster (bot pod, migrate Job), use the **host** value from the
PGO user secret — usually `weekly-status-db-primary`.

```bash
NS=weekly-status
SECRET=weekly-status-db-pguser-weeklystatus

python3 <<'PY'
import base64, json, subprocess, urllib.parse

ns = "weekly-status"
name = "weekly-status-db-pguser-weeklystatus"
raw = subprocess.check_output(
    ["oc", "get", "secret", name, "-n", ns, "-o", "json"], text=True
)
data = json.loads(raw)["data"]

def dec(key: str) -> str:
    return base64.b64decode(data[key]).decode()

user = dec("user")
password = dec("password")
host = dec("host")
port = dec("port")
dbname = dec("dbname")
password_enc = urllib.parse.quote(password, safe="")
url = f"postgresql+psycopg://{user}:{password_enc}@{host}:{port}/{dbname}"
print(url)
PY
```

Copy the printed line into `deploy/secrets.yaml`:

```yaml
DATABASE_URL: postgresql+psycopg://weeklystatus:<url-encoded-password>@weekly-status-db-primary:5432/weekly_status
```

Apply and restart the bot:

```bash
oc apply -f deploy/secrets.yaml
oc rollout restart deployment/weekly-status-slack-bot -n weekly-status
```

### URL format (manual)

If you prefer to assemble it yourself from the secret:

```
postgresql+psycopg://<user>:<password>@<host>:<port>/<dbname>
```

| Field | Where to get it |
|-------|-----------------|
| user | secret key `user` → `weeklystatus` |
| password | secret key `password` |
| host | secret key `host` → typically `weekly-status-db-primary` |
| port | secret key `port` → `5432` |
| dbname | secret key `dbname` → `weekly_status` |

URL-encode special characters in the password (`@`, `:`, `/`, etc.).

Quick peek at host/dbname only:

```bash
oc get secret weekly-status-db-pguser-weeklystatus -n weekly-status \
  -o jsonpath='{.data.host}' | base64 -d; echo
oc get secret weekly-status-db-pguser-weeklystatus -n weekly-status \
  -o jsonpath='{.data.dbname}' | base64 -d; echo
```

## 4. Run schema migrations

After `DATABASE_URL` is in `weekly-status-secrets`:

```bash
oc delete job weekly-status-db-migrate -n weekly-status --ignore-not-found
oc apply -f deploy/postgres/migrate-job.yaml
oc logs -f job/weekly-status-db-migrate -n weekly-status
```

## 5. Seed pilot person

Port-forward the primary service:

```bash
oc port-forward svc/weekly-status-db-primary -n weekly-status 5432:5432
```

Use the same user/password from the PGO secret:

```bash
psql "postgresql://weeklystatus:<password>@localhost:5432/weekly_status" <<'SQL'
INSERT INTO person (person_id, display_name, slack_user_id, jira_account_id, github_login)
VALUES ('yoza', 'Yash Oza', 'UXXXXXXXX', '712020:62c4c79e-1f59-45ed-a822-1c995df63024', 'yashoza19')
ON CONFLICT (person_id) DO UPDATE SET
  slack_user_id = EXCLUDED.slack_user_id,
  jira_account_id = EXCLUDED.jira_account_id,
  github_login = EXCLUDED.github_login;
SQL
```

Replace `UXXXXXXXX` with your Slack member ID.

## 6. Test the app

### Option A — run on cluster (recommended for `draft`)

`status draft` calls Anthropic for 1–2 minutes before writing Postgres. A flaky
`oc port-forward` will fail at persist time. Run the pipeline in-cluster instead:

```bash
oc delete job weekly-status-draft -n weekly-status --ignore-not-found
oc apply -f deploy/job-draft.yaml
oc logs -f job/weekly-status-draft -n weekly-status
```

Set `RUN_SEND: "true"` in the Job manifest to also DM the draft in Slack.

### Option B — local CLI with stable port-forward

In one terminal, keep the tunnel alive (auto-reconnects):

```bash
chmod +x scripts/port-forward-db.sh
export KUBECONFIG=/path/to/kubeconfig
# If your cluster name/namespace differ from example/openshift-operators:
# export PG_NAMESPACE=weekly-status PG_CLUSTER=weekly-status-db
./scripts/port-forward-db.sh
```

In another terminal:

```bash
status collect -p yoza --week 2026-08-14
status draft   -p yoza --week 2026-08-14
status send    -p yoza --week 2026-08-14
```

`status draft` only opens Postgres at the end and retries for ~24s if the tunnel
drops — restart or use `scripts/port-forward-db.sh` during the skill call.

In Slack: `/weekly-status` or click **Looks right** on the draft DM.

## Teardown

```bash
oc delete -f deploy/postgres/postgrescluster.yaml
# PVCs may remain depending on PGO reclaim policy — delete manually if needed
```

## Troubleshooting

| Issue | Check |
|-------|--------|
| PostgresCluster not ready | `oc describe postgrescluster weekly-status-db` |
| Image pull errors on Crunchy image | Cluster needs access to `registry.developers.crunchydata.com` |
| Bot DB errors | `DATABASE_URL` host must be `weekly-status-db-primary` from inside namespace |
| Migrate job fails | `oc logs job/weekly-status-db-migrate`; confirm secret applied first |
