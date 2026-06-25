#!/usr/bin/env bash
# =============================================================================
# setup-server.sh — Hetzner Ubuntu 22.04 first-time setup
# Run as root: bash setup-server.sh
# =============================================================================
set -euo pipefail

APP_DIR="/opt/price_agent"
REPO_URL="https://github.com/matenagy1990/gepcoop-price-agent.git"
SERVICE_NAME="price-agent"

echo "==> Updating system packages…"
apt-get update -y
apt-get upgrade -y

echo "==> Installing Docker…"
apt-get install -y ca-certificates curl gnupg nginx ufw
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker
systemctl start docker

echo "==> Cloning the repository to ${APP_DIR}…"
if [ -d "${APP_DIR}/.git" ]; then
  echo "   (already cloned — pulling latest)"
  git -C "${APP_DIR}" pull
else
  git clone "${REPO_URL}" "${APP_DIR}"
fi

echo ""
echo "==> ACTION REQUIRED: Create the .env file"
echo "   Copy your .env content into: ${APP_DIR}/.env"
echo "   Template is at:              ${APP_DIR}/.env.example"
echo ""
echo "   When ready, press ENTER to continue…"
read -r

if [ ! -f "${APP_DIR}/.env" ]; then
  echo "ERROR: ${APP_DIR}/.env not found. Create it and re-run this script."
  exit 1
fi

echo "==> Installing systemd service…"
cp "${APP_DIR}/deploy/price-agent.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

echo "==> Configuring Nginx…"
cp "${APP_DIR}/deploy/nginx-price-agent.conf" /etc/nginx/sites-available/price-agent
ln -sf /etc/nginx/sites-available/price-agent /etc/nginx/sites-enabled/price-agent
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "   Nginx configured (port 80 → Price Agent, /batch-agent/ → Batch Agent)"

echo "==> Configuring firewall…"
ufw allow OpenSSH
ufw allow 80/tcp
ufw delete allow 8080/tcp >/dev/null 2>&1 || true
ufw delete allow 8001/tcp >/dev/null 2>&1 || true
ufw deny 8080/tcp
ufw deny 8001/tcp
ufw --force enable

echo "==> Starting the app for the first time (Docker image download ~1.5 GB)…"
systemctl start "${SERVICE_NAME}"

echo ""
echo "==> Done! Status:"
systemctl status "${SERVICE_NAME}" --no-pager

SERVER_IP=$(curl -s ifconfig.me || echo "<server-ip>")
echo ""
echo "App is accessible at: http://${SERVER_IP}"
echo "Batch agent at:       http://${SERVER_IP}/batch-agent/"
echo ""
echo "HTTPS activation directly for the server IP (no domain required):"
echo "  bash ${APP_DIR}/deploy/enable-https.sh ${SERVER_IP} admin@example.hu"
echo ""
echo "Useful commands:"
echo "  systemctl status ${SERVICE_NAME}     # check if running"
echo "  systemctl restart ${SERVICE_NAME}    # restart"
echo "  journalctl -u ${SERVICE_NAME} -f     # live logs"
echo "  docker compose -f ${APP_DIR}/docker-compose.yml logs -f  # app logs"
