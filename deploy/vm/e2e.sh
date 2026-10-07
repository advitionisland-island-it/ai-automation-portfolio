#!/usr/bin/env bash
# Runs scripts/e2e.py inside the VM, against the deployed stack (AC-6.1). Run from the Mac with
# `make vm-e2e`; deploy.sh copies this file to /opt/sales-ops.
#
# The checks run in a container from the same app image, on the VM's host network, where the
# services listen on 127.0.0.1. The last check stops the api with docker compose, as on the Mac,
# so the container gets the VM's docker CLI and socket, and the compose file and secrets file it
# needs to address the project. Nothing is installed in the VM for this.
set -euo pipefail

REMOTE=/opt/sales-ops
SECRETS=/etc/sales-ops/secrets.env

exec docker run --rm --network host --user root \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /usr/bin/docker:/usr/bin/docker:ro \
  -v /usr/libexec/docker/cli-plugins:/usr/libexec/docker/cli-plugins:ro \
  -v "$REMOTE:$REMOTE:ro" \
  -v "$SECRETS:$SECRETS:ro" \
  -e E2E_COMPOSE="docker compose -p sales-ops -f $REMOTE/compose.yaml --env-file $SECRETS" \
  sales-ops-app:local python "$REMOTE/scripts/e2e.py"
