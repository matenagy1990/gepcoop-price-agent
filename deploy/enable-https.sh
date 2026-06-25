#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/price_agent}"
PUBLIC_IP="${1:-}"
EMAIL="${2:-}"

if [[ -z "${PUBLIC_IP}" ]]; then
  PUBLIC_IP="$(curl -4 -fsS https://api.ipify.org)"
fi

if [[ ! "${PUBLIC_IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "Hibás vagy nem felismerhető publikus IPv4-cím: ${PUBLIC_IP}"
  echo "Használat: sudo bash deploy/enable-https.sh 178.104.208.200 [email@example.hu]"
  exit 1
fi

if [[ ! -f "${APP_DIR}/deploy/nginx-price-agent-https.conf.template" ]]; then
  echo "Nem található a HTTPS Nginx sablon: ${APP_DIR}"
  exit 1
fi

echo "==> Nginx, Snap és tűzfal telepítése"
apt-get update -y
apt-get install -y nginx snapd gettext-base ufw
systemctl enable --now snapd.socket

echo "==> Certbot 5.4+ telepítése Snapből"
snap install core >/dev/null 2>&1 || true
snap refresh core >/dev/null
snap install --classic certbot >/dev/null 2>&1 || snap refresh certbot >/dev/null
ln -sf /snap/bin/certbot /usr/local/bin/certbot

CERTBOT_VERSION="$(certbot --version | awk '{print $2}')"
if [[ "$(printf '%s\n' "5.4" "${CERTBOT_VERSION}" | sort -V | head -n1)" != "5.4" ]]; then
  echo "Az IP-tanúsítványhoz Certbot 5.4 vagy újabb szükséges. Telepített: ${CERTBOT_VERSION}"
  exit 1
fi

echo "==> Ideiglenes HTTP reverse proxy beállítása"
cp "${APP_DIR}/deploy/nginx-price-agent.conf" /etc/nginx/sites-available/price-agent
ln -sf /etc/nginx/sites-available/price-agent /etc/nginx/sites-enabled/price-agent
rm -f /etc/nginx/sites-enabled/default
mkdir -p /var/www/html/.well-known/acme-challenge
nginx -t
systemctl enable --now nginx
systemctl reload nginx

CERTBOT_ACCOUNT_ARGS=(--agree-tos --non-interactive)
if [[ -n "${EMAIL}" ]]; then
  CERTBOT_ACCOUNT_ARGS+=(--email "${EMAIL}")
else
  CERTBOT_ACCOUNT_ARGS+=(--register-unsafely-without-email)
fi

echo "==> Hatnapos, nyilvánosan megbízható TLS-tanúsítvány igénylése: ${PUBLIC_IP}"
certbot certonly \
  --preferred-profile shortlived \
  --webroot \
  --webroot-path /var/www/html \
  --ip-address "${PUBLIC_IP}" \
  "${CERTBOT_ACCOUNT_ARGS[@]}"

echo "==> HTTPS reverse proxy aktiválása"
export PRICE_AGENT_IP="${PUBLIC_IP}"
envsubst '${PRICE_AGENT_IP}' \
  < "${APP_DIR}/deploy/nginx-price-agent-https.conf.template" \
  > /etc/nginx/sites-available/price-agent
nginx -t
systemctl reload nginx

echo "==> Automatikus megújítás és Nginx újratöltés"
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'EOF'
#!/usr/bin/env bash
systemctl reload nginx
EOF
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
systemctl enable --now snap.certbot.renew.timer 2>/dev/null || true

echo "==> Tűzfal szigorítása"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw delete allow 8080/tcp >/dev/null 2>&1 || true
ufw delete allow 8001/tcp >/dev/null 2>&1 || true
ufw deny 8080/tcp
ufw deny 8001/tcp
ufw --force enable

echo ""
echo "HTTPS aktív: https://${PUBLIC_IP}"
echo "Batch Agent: https://${PUBLIC_IP}/batch-agent/"
echo "A tanúsítvány hatnapos, a Certbot automatikusan megújítja."
echo "A 8080 és 8001 portok csak a szerveren belül érhetők el."
