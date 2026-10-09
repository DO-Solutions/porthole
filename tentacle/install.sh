#!/bin/bash
# Tentacle installer. Idempotent; safe to run again, and safe as DigitalOcean user_data.
#
# From a checkout:   sudo TENTACLE_KEY=... PEER_URL=http://10.x.x.x:8800 ./install.sh
# As user_data:      paste this file and set the variables below (or export them above this line);
#                    the code then comes from TENTACLE_TARBALL_URL (a tarball containing tentacle/).
#
# /etc/tentacle/env is written once and then left alone so secrets are never clobbered;
# set TENTACLE_ENV_OVERWRITE=1 to rewrite it.
set -euo pipefail

: "${TENTACLE_NAME:=$(hostname)}"
: "${TENTACLE_KEY:=}"
: "${TENTACLE_PORT:=8800}"
: "${PEER_URL:=}"
: "${PG_DSN:=}"
: "${FN_URL:=}"
: "${OTEL_EXPORTER_OTLP_ENDPOINT:=http://127.0.0.1:4318}"
: "${OTEL_SERVICE_NAME:=$TENTACLE_NAME}"
: "${TENTACLE_TARBALL_URL:=}"
: "${TENTACLE_ENV_OVERWRITE:=0}"

APP=/opt/tentacle
ETC=/etc/tentacle
UNIT=/etc/systemd/system/tentacle.service
export DEBIAN_FRONTEND=noninteractive

log() { echo "[tentacle-install] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

# 1. packages (cloud-init may still hold the apt lock on first boot)
# `python3 -m venv --help` succeeds on Ubuntu even when ensurepip is missing (the venv then fails to create), so
# test ensurepip itself.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1 || ! command -v curl >/dev/null; then
  log "installing python3-venv"
  apt-get -o DPkg::Lock::Timeout=600 update -q
  apt-get -o DPkg::Lock::Timeout=600 install -y -q python3-venv curl ca-certificates
fi

# 2. user and directories
id tentacle >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin tentacle
install -d -o root -g root -m 0755 "$APP"
install -d -o root -g tentacle -m 0750 "$ETC"
install -d -o tentacle -g tentacle -m 0755 /var/log/tentacle /var/tmp/tentacle

# 3. code: from the directory this script sits in, else from the tarball
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
if [ -z "$SRC" ] || [ ! -f "$SRC/tentacle.py" ]; then
  if [ -z "$TENTACLE_TARBALL_URL" ]; then
    echo "no tentacle.py next to this script and TENTACLE_TARBALL_URL is not set" >&2
    exit 1
  fi
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  log "fetching $TENTACLE_TARBALL_URL"
  curl -fsSL --retry 5 --retry-delay 3 "$TENTACLE_TARBALL_URL" | tar -xz -C "$TMP"
  SRC="$(dirname "$(find "$TMP" -path '*tentacle/tentacle.py' -print -quit)")"
  [ -f "$SRC/tentacle.py" ] || { echo "tarball has no tentacle/tentacle.py" >&2; exit 1; }
fi
install -o root -g root -m 0644 "$SRC/tentacle.py" "$SRC/requirements.txt" "$APP/"

# 4. virtualenv and pinned dependencies
# A venv created without ensurepip has a python but no pip; treat that as absent and rebuild it.
[ -x "$APP/venv/bin/pip" ] || { rm -rf "$APP/venv"; python3 -m venv "$APP/venv"; }
"$APP/venv/bin/pip" install -q --disable-pip-version-check -r "$APP/requirements.txt"

# 5. environment file (written once)
if [ ! -f "$ETC/env" ] || [ "$TENTACLE_ENV_OVERWRITE" = "1" ]; then
  if [ -z "$TENTACLE_KEY" ]; then
    log "TENTACLE_KEY not set: mutating endpoints stay disabled until $ETC/env has one"
  fi
  umask 027
  cat > "$ETC/env" <<EOF
TENTACLE_NAME=$TENTACLE_NAME
TENTACLE_KEY=$TENTACLE_KEY
TENTACLE_PORT=$TENTACLE_PORT
PEER_URL=$PEER_URL
PG_DSN=$PG_DSN
FN_URL=$FN_URL
OTEL_EXPORTER_OTLP_ENDPOINT=$OTEL_EXPORTER_OTLP_ENDPOINT
OTEL_SERVICE_NAME=$OTEL_SERVICE_NAME
EOF
  chown root:tentacle "$ETC/env"
  chmod 0640 "$ETC/env"
  log "wrote $ETC/env"
fi

# 6. systemd unit (kept identical to tentacle.service; a test checks that)
if [ -f "$SRC/tentacle.service" ]; then
  install -o root -g root -m 0644 "$SRC/tentacle.service" "$UNIT"
else
  cat > "$UNIT" <<'UNIT_EOF'
[Unit]
Description=Tentacle scenario service (Insights demo)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tentacle
Group=tentacle
EnvironmentFile=/etc/tentacle/env
WorkingDirectory=/opt/tentacle
ExecStart=/opt/tentacle/venv/bin/python /opt/tentacle/tentacle.py
Restart=always
RestartSec=3
TimeoutStopSec=20
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT_EOF
fi

systemctl daemon-reload
systemctl enable tentacle.service >/dev/null
systemctl restart tentacle.service
log "tentacle enabled and (re)started on port $(grep -E '^TENTACLE_PORT=' "$ETC/env" | cut -d= -f2)"
