#!/usr/bin/env bash
# Install or update the hosted assistant on a Linux server (Ubuntu, Python 3.12+), behind an existing reverse proxy.
#
#   sudo bash deploy/vps/setup.sh /path/to/unpacked/harbor
#
# - code in /opt/dastyar (replaced on every run), venv in /opt/dastyar/.venv, data in /var/lib/dastyar
# - runs as the system user "dastyar" (no shell, no sudo), systemd unit "dastyar"
# - settings in /etc/dastyar.env (created from dastyar.env.example once; never overwritten)
# Changes nothing else on the machine: the proxy route (Caddy/nginx) is added by hand, see README «میزبانی».
set -euo pipefail
SRC="${1:?usage: setup.sh <unpacked repo dir>}"
APP=/opt/dastyar
MIRROR="${PIP_INDEX_URL:-https://package-mirror.liara.ir/repository/pypi/simple}"   # PyPI is slow/blocked from Iran

id dastyar >/dev/null 2>&1 || useradd --system --home /var/lib/dastyar --shell /usr/sbin/nologin dastyar
install -d -o dastyar -g dastyar -m 700 /var/lib/dastyar
rm -rf "$APP.new" && mkdir -p "$APP.new"
cp -r "$SRC"/liara "$SRC"/core "$SRC"/main.py "$SRC"/requirements.txt "$APP.new"/
[ -f "$SRC/VERSION" ] && cp "$SRC/VERSION" "$APP.new"/
if [ -x "$APP/.venv/bin/python" ]; then mv "$APP/.venv" "$APP.new/.venv"; fi
rm -rf "$APP" && mv "$APP.new" "$APP"
[ -x "$APP/.venv/bin/pip" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet --index-url "$MIRROR" -r "$APP/requirements.txt"
[ -f /etc/dastyar.env ] || install -m 640 -o root -g dastyar "$SRC/deploy/vps/dastyar.env.example" /etc/dastyar.env
install -m 644 "$SRC/deploy/vps/dastyar.service" /etc/systemd/system/dastyar.service
systemctl daemon-reload
systemctl enable --quiet dastyar
systemctl restart dastyar
sleep 3
systemctl is-active dastyar && echo "dastyar: $(cat "$APP/VERSION" 2>/dev/null || echo unknown) running"
