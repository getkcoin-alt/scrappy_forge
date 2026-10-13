#!/usr/bin/env bash
# One-time bootstrap on the user's VPS. Run as root from a trusted SSH session:
# bash bootstrap.sh /root/forge-deploy.pub [public-domain]
set -Eeuo pipefail

[[ "$(id -u)" == 0 ]] || { echo "Run as root on the VPS" >&2; exit 1; }
KEY_FILE="${1:-}"
DOMAIN="${2:-31-97-227-171.sslip.io}"
[[ -f "$KEY_FILE" ]] || { echo "Provide a trusted SSH public key file as argument 1" >&2; exit 2; }
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || {
  echo "Domain must be a DNS name" >&2; exit 2;
}
[[ -f /etc/debian_version ]] || { echo "This bootstrap supports Debian/Ubuntu apt systems only" >&2; exit 2; }

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates iproute2
# Existing VPS apps may already rely on Docker CE. Never replace that installation.
if ! command -v docker >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io
  systemctl enable --now docker
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable; inspect the existing service without replacing it" >&2
  exit 1
fi

if ! id forge-deploy >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash forge-deploy
fi
# Docker group grants root-equivalent authority: restrict this deploy key to the trusted repository.
usermod -aG docker forge-deploy
install -d -m 0750 -o forge-deploy -g forge-deploy /opt/scrappy-forge
install -d -m 0750 -o forge-deploy -g forge-deploy /opt/scrappy-forge/incoming
install -d -m 0700 -o forge-deploy -g forge-deploy /home/forge-deploy/.ssh

PUB="$(cat "$KEY_FILE")"
if [[ ! "$PUB" =~ ^ssh-ed25519[[:space:]]+([A-Za-z0-9+/]+={0,2})([[:space:]].*)?$ ]]; then
  echo "The deploy key must be a single ed25519 SSH public key" >&2
  exit 2
fi
touch /home/forge-deploy/.ssh/authorized_keys
if ! grep -qxF "$PUB" /home/forge-deploy/.ssh/authorized_keys; then
  printf '%s\n' "$PUB" >> /home/forge-deploy/.ssh/authorized_keys
fi
chown forge-deploy:forge-deploy /home/forge-deploy/.ssh/authorized_keys
chmod 0600 /home/forge-deploy/.ssh/authorized_keys

cat > /opt/scrappy-forge/Caddyfile <<EOF
${DOMAIN} {
  encode gzip
  reverse_proxy 127.0.0.1:18080
}
EOF
chmod 0644 /opt/scrappy-forge/Caddyfile

# Never steal ports from other services. Install a virtual host in the existing
# proxy instead when 80/443 are already occupied.
if docker container inspect scrappy-forge-caddy >/dev/null 2>&1; then
  echo "Existing scrappy-forge-caddy retained; review domain and reload manually if changed."
elif ss -H -ltn '( sport = :80 or sport = :443 )' | grep -q .; then
  echo "Ports 80/443 are occupied; Caddy NOT started. Configure existing proxy for ${DOMAIN} -> 127.0.0.1:18080." >&2
  echo "See deploy/vps/nginx-site.conf.example for an Nginx example." >&2
else
  docker volume create scrappy-forge-caddy-data >/dev/null
  docker volume create scrappy-forge-caddy-config >/dev/null
  docker run -d \
    --name scrappy-forge-caddy \
    --restart unless-stopped \
    --network host \
    --read-only \
    -v /opt/scrappy-forge/Caddyfile:/etc/caddy/Caddyfile:ro \
    -v scrappy-forge-caddy-data:/data \
    -v scrappy-forge-caddy-config:/config \
    caddy:2 >/dev/null
  echo "Caddy started. Allow inbound TCP 80/443 and confirm public DNS resolves to this VPS."
fi

echo "Bootstrap complete: SSH as forge-deploy, then configure GitHub Actions secrets."
echo "Expected HTTPS hostname: ${DOMAIN}"
echo "Public signup remains disabled. This host runs only Forge Hub, not remote user tasks."
