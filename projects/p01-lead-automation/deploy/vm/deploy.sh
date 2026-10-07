#!/usr/bin/env bash
# Deploys the stack into the Lima VM (M6, UD-7). Run from the project directory: `make vm-deploy`.
#
# 1. Builds the app image on the Mac (the build `make up` uses) and copies it and the three
#    service images into the VM, unless the VM already holds the same image ID. Nothing is built
#    or pulled in the VM: the Mac and the VM run the same images (AC-6.2).
# 2. Copies the deployment files to /opt/sales-ops (the VM shares no folder with the Mac).
# 3. On the first deploy, writes a random database password to /etc/sales-ops/secrets.env in the
#    VM: root only, outside git, never printed (AC-6.3).
# 4. docker compose up: the migrate service applies the migrations before the api and the worker
#    start (AC-6.5), and --wait fails the deploy unless every service reports healthy (AC-6.4).
set -euo pipefail

LIMACTL="${LIMACTL:-$HOME/.local/lima/bin/limactl}"
VM="${VM:-p01-vm}"
REMOTE=/opt/sales-ops
SECRETS=/etc/sales-ops/secrets.env
IMAGES=(sales-ops-app:local postgres:17-alpine axllent/mailpit:v1.31.2 n8nio/n8n:2.40.7)

in_vm() { "$LIMACTL" shell --workdir / "$VM" -- "$@"; }

# The image's content is its linux/arm64 manifest (config and layers). The top-level ID is an
# index that every build writes anew, even when every layer comes from the cache, so the same
# content can carry different IDs (evidence M6/diagnosis-image-id.txt, D-94).
content() { "$@" --platform linux/arm64 --format '{{json .Descriptor}}' | sed -E 's/.*"digest":"([^"]+)".*/\1/'; }

echo "== build the app image on the Mac"
docker compose build --quiet api

echo "== images: the same content (linux/arm64 manifest) on the Mac and in the VM"
for image in "${IMAGES[@]}"; do
  mac=$(content docker image inspect "$image")
  vm=$(content in_vm sudo docker image inspect "$image" 2>/dev/null || true)
  if [ "$mac" != "$vm" ]; then
    docker save "$image" | in_vm sudo docker load >/dev/null
    vm=$(content in_vm sudo docker image inspect "$image")
  fi
  echo "   $image  mac $mac  vm $vm"
  if [ "$mac" != "$vm" ]; then
    echo "image content differs for $image" >&2
    exit 1
  fi
done

echo "== deployment files -> $VM:$REMOTE"
staging=$(in_vm mktemp -d)
"$LIMACTL" copy --recursive deploy/vm/compose.yaml n8n scripts/e2e.py deploy/vm/e2e.sh "$VM:$staging/"
in_vm sudo bash -c "
  set -euo pipefail
  mkdir -p $REMOTE/scripts
  install -m 0644 $staging/compose.yaml $REMOTE/compose.yaml
  install -m 0644 $staging/e2e.py $REMOTE/scripts/e2e.py
  install -m 0755 $staging/e2e.sh $REMOTE/e2e.sh
  rm -rf $REMOTE/n8n && cp -r $staging/n8n $REMOTE/n8n && chmod -R a+rX $REMOTE/n8n
  rm -rf $staging
"

echo "== secrets: $SECRETS"
in_vm sudo bash -c "
  set -euo pipefail
  umask 077
  mkdir -p \$(dirname $SECRETS)
  if [ ! -s $SECRETS ]; then
    printf 'POSTGRES_PASSWORD=%s\n' \"\$(openssl rand -hex 32)\" > $SECRETS
    echo '   created (a random database password; the value is not shown)'
  else
    echo '   kept (already present; the value is not shown)'
  fi
  stat -c '   %U:%G %a %n' $SECRETS
"

echo "== docker compose up (migrations first, then every service must be healthy)"
in_vm sudo docker compose -p sales-ops -f "$REMOTE/compose.yaml" --env-file "$SECRETS" \
  up -d --wait --wait-timeout 300
in_vm sudo docker compose -p sales-ops -f "$REMOTE/compose.yaml" --env-file "$SECRETS" \
  ps --all --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
echo "== migrate log"
in_vm sudo docker compose -p sales-ops -f "$REMOTE/compose.yaml" --env-file "$SECRETS" \
  logs --no-log-prefix migrate
