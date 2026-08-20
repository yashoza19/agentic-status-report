# OpenShift deployment (M3.5)

Deploy the long-lived Slack Socket Mode bot to OpenShift. The bot uses **outbound**
connections only — no Route, Ingress, or public URL is required.

Full design: [docs/DESIGN.md](../docs/DESIGN.md) §5.6

Closes GitHub issue #6 (M3.5 OpenShift deploy).

## Prerequisites

1. **Postgres** reachable from the cluster; schema applied. For a quick in-cluster DB,
   see [postgres/README.md](postgres/README.md).

   ```bash
   alembic upgrade head
   ```

2. **Person rows** with `slack_user_id` for each pilot user.

3. **Slack app** ([api.slack.com/apps](https://api.slack.com/apps)):
   - Socket Mode enabled
   - Bot scopes: `chat:write`, `im:write`, `im:history`, `users:read`, `commands`
   - Interactivity enabled (no request URL with Socket Mode)
   - Slash command: `/weekly-status` (Slack reserves `/status`)

4. **OpenShift** namespace and pull secret if using a private registry.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile` | Application image (bot + future CronJobs) |
| `deployment.yaml` | Slack bot Deployment (1 replica) |
| `secrets.example.yaml` | App Secret template — copy, fill, never commit real values |
| `job-collect-draft-send.yaml` | Optional one-off smoke test Job |
| `postgres/` | Crunchy PGO `PostgresCluster` + migrate Job — see [postgres/README.md](postgres/README.md) |

## Build and push

From the repository root:

```bash
export REGISTRY=<your-registry>
export TAG=m3.5-$(git rev-parse --short HEAD)

docker build -t "${REGISTRY}/weekly-status:${TAG}" -f deploy/Dockerfile .
docker push "${REGISTRY}/weekly-status:${TAG}"
```

On OpenShift with an internal registry:

```bash
oc project weekly-status   # or your namespace
oc new-project weekly-status

# Build inside the cluster (alternative to docker push)
oc new-build --binary --name=weekly-status -l app.kubernetes.io/name=weekly-status
oc start-build weekly-status --from-dir=. --follow
```

If using `oc new-build`, adjust `deployment.yaml` to reference the ImageStream tag
(e.g. `weekly-status:latest`).

## Configure secrets

```bash
cp deploy/secrets.example.yaml deploy/secrets.yaml
# Edit deploy/secrets.yaml with real values
oc apply -f deploy/secrets.yaml
```

**Minimum for the bot Deployment:**

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Postgres connection string |
| `SLACK_BOT_TOKEN` | `xoxb-…` |
| `SLACK_APP_TOKEN` | `xapp-…` (Socket Mode) |

The smoke-test Job also needs Jira, GitHub, Anthropic, and `DRAFTER_SKILL_ID`.

## Deploy the Slack bot

1. Set the image in `deployment.yaml` (or patch at apply time):

   ```bash
   sed "s|weekly-status:latest|${REGISTRY}/weekly-status:${TAG}|" deploy/deployment.yaml \
     | oc apply -f -
   ```

2. Watch logs:

   ```bash
   oc logs -f deployment/weekly-status-slack-bot
   ```

   Expect: `starting Slack socket mode handler` and Bolt connection messages.

3. **Do not scale above 1 replica** — multiple pods duplicate Socket Mode connections.

## Smoke test (collect → draft → send)

While the bot Deployment is running, seed a draft with a one-off Job:

1. Edit `job-collect-draft-send.yaml`: set `PERSON_ID`, `WEEK_ENDING`, and image.
2. Apply:

   ```bash
   oc apply -f deploy/job-collect-draft-send.yaml
   oc logs -f job/weekly-status-collect-draft-send
   ```

3. In Slack: open the DM, click **Looks right**, or run `/weekly-status`.
4. Verify in Postgres: `status_entry.confirmed_at` is set.

Alternatively, run the same commands locally against the cluster database while the
bot runs in OpenShift.

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Pod crash loop on start | `oc logs`; missing `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` |
| Bot up but buttons dead | Only one replica; Socket Mode token valid; interactivity enabled |
| `status send` fails in Job | Person has `slack_user_id`; bot token has `im:write` |
| DB connection errors | `DATABASE_URL` reachable from pod network |

## Out of scope (M6)

- `cronjobs.yaml` for weekly automation
- `PILOT_PERSON_IDS` enforcement in send
