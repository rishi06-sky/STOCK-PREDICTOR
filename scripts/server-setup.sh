#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu or Debian server for this platform.
#
# Run as root on the server, with the domain whose DNS A record points at it:
#
#   curl -fsSL https://raw.githubusercontent.com/rishi06-sky/STOCK-PREDICTOR/main/scripts/server-setup.sh -o server-setup.sh
#   less server-setup.sh          # read it first
#   bash server-setup.sh stocks.example.com
#
# It installs Docker, opens only SSH/HTTP/HTTPS in the firewall, creates a
# `deploy` user, clones the repository to /opt/stock-intelligence, writes a
# production .env with generated secrets, starts the stack behind Caddy
# (HTTPS), loads reference data, and creates the SSH key GitHub Actions deploys
# with. At the end it prints the values to add as GitHub secrets.
#
# Safe to re-run: an existing .env, user, clone or key is kept, never replaced,
# so generated passwords are not rotated out from under the database.
set -euo pipefail

DOMAIN="${1:-}"
REPO_URL="${REPO_URL:-https://github.com/rishi06-sky/STOCK-PREDICTOR.git}"
APP_DIR="${APP_DIR:-/opt/stock-intelligence}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
KEY_FILE="/root/github-actions-deploy-key"

log() { printf '\n[setup] %s\n' "$*"; }
die() { printf '[setup] error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 <domain>)"
[ -n "$DOMAIN" ] || die "usage: bash $0 <domain>   e.g. bash $0 stocks.example.com"
case "$DOMAIN" in
  http*|*/*|*" "*) die "pass the bare domain, e.g. stocks.example.com" ;;
esac
# shellcheck source=/dev/null
. /etc/os-release
case "${ID:-}" in
  ubuntu|debian) ;;
  *) die "this script supports Ubuntu and Debian; found ${PRETTY_NAME:-unknown}" ;;
esac

# --------------------------------------------------------------- DNS check
public_ip=$(curl -fsS4 --max-time 10 https://api.ipify.org || true)
dns_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}' || true)
if [ -z "$dns_ip" ]; then
  log "WARNING: $DOMAIN does not resolve yet. HTTPS will start working once its A record points at ${public_ip:-this server}."
elif [ -n "$public_ip" ] && [ "$dns_ip" != "$public_ip" ]; then
  log "WARNING: $DOMAIN resolves to $dns_ip but this server is $public_ip. Fix the A record, or HTTPS certificates cannot be issued."
fi

# ------------------------------------------------------------------ packages
log "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl git openssl ufw

if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker from Docker's apt repository"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker >/dev/null

# ------------------------------------------------------------------ firewall
# Docker publishes ports around ufw, which is why only Caddy publishes public
# ports; the API and frontend are bound to 127.0.0.1 in docker-compose.yml.
log "configuring the firewall (SSH, HTTP, HTTPS only)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

# --------------------------------------------------------------- deploy user
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  log "creating user $DEPLOY_USER"
  adduser --disabled-password --gecos "" "$DEPLOY_USER" >/dev/null
fi
usermod -aG docker "$DEPLOY_USER"

# -------------------------------------------------------------- repository
if [ ! -d "$APP_DIR/.git" ]; then
  log "cloning $REPO_URL to $APP_DIR"
  install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"
  sudo -u "$DEPLOY_USER" git clone -q "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------------- .env
env_file="$APP_DIR/.env"
set_env() {
  # Replace KEY=... (commented out or not), or append it.
  if grep -qE "^#? ?$1=" "$env_file"; then
    sed -i -E "s|^#? ?$1=.*|$1=$2|" "$env_file"
  else
    printf '%s=%s\n' "$1" "$2" >> "$env_file"
  fi
}
if [ ! -f "$env_file" ]; then
  log "writing a production .env with generated secrets"
  sudo -u "$DEPLOY_USER" cp "$APP_DIR/.env.example" "$env_file"
  chmod 600 "$env_file"
  set_env ENVIRONMENT production
  set_env DEBUG false
  set_env SECRET_KEY "$(openssl rand -hex 32)"
  set_env POSTGRES_PASSWORD "$(openssl rand -hex 24)"
  set_env CORS_ORIGINS "https://$DOMAIN"
  set_env NEXT_PUBLIC_API_BASE "https://$DOMAIN/api/v1"
  set_env COMPOSE_PROFILES https
  set_env DOMAIN "$DOMAIN"
  set_env BIND_ADDRESS 127.0.0.1
  chown "$DEPLOY_USER:$DEPLOY_USER" "$env_file"
else
  log "keeping the existing $env_file"
fi

# ------------------------------------------------------- first start + data
log "building and starting the stack (the first build takes several minutes)"
sudo -u "$DEPLOY_USER" -H bash -c "cd '$APP_DIR' && ./scripts/deploy.sh"

log "loading reference data (markets, exchanges, securities)"
sudo -u "$DEPLOY_USER" -H bash -c "cd '$APP_DIR' && docker compose exec -T api python scripts/seed.py"

# --------------------------------------------------------- GitHub Actions key
if [ ! -f "$KEY_FILE" ]; then
  log "creating the SSH key GitHub Actions deploys with"
  ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f "$KEY_FILE"
fi
auth_keys="/home/$DEPLOY_USER/.ssh/authorized_keys"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
touch "$auth_keys"
# `restrict` turns off forwarding and PTY allocation; the deploy only needs to
# run commands.
if ! grep -qF "$(cut -d' ' -f2 "$KEY_FILE.pub")" "$auth_keys"; then
  echo "restrict $(cat "$KEY_FILE.pub")" >> "$auth_keys"
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$auth_keys"
chmod 600 "$auth_keys"

known_hosts=$(awk -v host="$DOMAIN" '{print host, $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub)

# ------------------------------------------------------------------- summary
cat <<EOF

========================================================================
 Server ready. https://$DOMAIN (once DNS points here)

 Add these in GitHub: rishi06-sky/STOCK-PREDICTOR
   Settings > Secrets and variables > Actions

 Secrets (New repository secret):
   DEPLOY_HOST          $DOMAIN
   DEPLOY_USER          $DEPLOY_USER
   DEPLOY_KNOWN_HOSTS   $known_hosts
   DEPLOY_SSH_KEY       the whole private key below, BEGIN and END lines included

 Variables (Variables tab > New repository variable):
   DEPLOY_PATH          $APP_DIR

 Private key for DEPLOY_SSH_KEY:
EOF
cat "$KEY_FILE"
cat <<EOF

 After saving it in GitHub, delete it from the server:
   shred -u $KEY_FILE

 Next:
   1. Open https://$DOMAIN and register. The first account becomes the admin.
   2. Load price history and train the first model (takes a while):
        sudo -u $DEPLOY_USER -H bash -c 'cd $APP_DIR && docker compose exec -T api python scripts/bootstrap.py'
   3. In GitHub, Actions > Deploy > Run workflow, to check that deploys work.
========================================================================
EOF
