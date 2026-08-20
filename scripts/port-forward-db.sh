#!/usr/bin/env bash
# Keep a reconnecting tunnel to Crunchy Postgres for local `status` CLI.
#
# Run in a dedicated terminal (or tmux pane) and leave it open while you run
# `status collect`, `status draft`, etc. Defaults match the `example` cluster
# in openshift-operators; override with env vars if yours differ.
#
#   export KUBECONFIG=/path/to/kubeconfig
#   ./scripts/port-forward-db.sh
#
set -euo pipefail

NAMESPACE="${PG_NAMESPACE:-openshift-operators}"
CLUSTER="${PG_CLUSTER:-example}"
LOCAL_PORT="${PG_LOCAL_PORT:-5432}"
REMOTE_PORT="${PG_REMOTE_PORT:-5432}"

while true; do
  POD="$(
    oc get pod -n "$NAMESPACE" \
      -l "postgres-operator.crunchydata.com/cluster=${CLUSTER},postgres-operator.crunchydata.com/role=master" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true
  )"
  if [[ -z "$POD" ]]; then
    echo "No primary pod for cluster=${CLUSTER} in ${NAMESPACE}; retrying in 5s..." >&2
    sleep 5
    continue
  fi
  echo "Port-forward ${LOCAL_PORT}:${REMOTE_PORT} → ${POD} (${NAMESPACE})" >&2
  oc port-forward -n "$NAMESPACE" "$POD" "${LOCAL_PORT}:${REMOTE_PORT}" || true
  echo "Port-forward exited; reconnecting in 2s..." >&2
  sleep 2
done
