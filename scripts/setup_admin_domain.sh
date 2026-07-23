#!/bin/bash
set -euo pipefail

# Attaches a second, admin-only hostname (e.g. a dedicated superadmin
# subdomain) to an existing Dograh install alongside its canonical PUBLIC_HOST.
# Both hostnames are served out of the *same* nginx server block and share one
# SAN certificate — nginx/back-end routing is identical for either host; only
# the app's own post-login/impersonation redirect logic (NEXT_PUBLIC_ADMIN_URL)
# treats them differently. Unlike setup_custom_domain.sh (which repoints the
# single canonical domain), this script is purely additive: PUBLIC_HOST /
# PUBLIC_BASE_URL are left untouched.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_PATH="$SCRIPT_DIR/lib/setup_common.sh"
BOOTSTRAP_LIB=""

if [[ ! -f "$LIB_PATH" ]]; then
    BOOTSTRAP_LIB="$(mktemp)"
    curl -fsSL -o "$BOOTSTRAP_LIB" "https://raw.githubusercontent.com/dograh-hq/dograh/main/scripts/lib/setup_common.sh"
    LIB_PATH="$BOOTSTRAP_LIB"
fi

cleanup() {
    if [[ -n "$BOOTSTRAP_LIB" ]]; then
        rm -f "$BOOTSTRAP_LIB"
    fi
    if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" && -n "${DOGRAH_DEPLOY_PROJECT_DIR:-}" && -d "$DOGRAH_DEPLOY_PROJECT_DIR" ]]; then
        echo -e "${BLUE}Restoring ownership of $DOGRAH_DEPLOY_PROJECT_DIR to ${SUDO_USER:-uid $SUDO_UID}...${NC}"
        chown -R "$SUDO_UID:$SUDO_GID" "$DOGRAH_DEPLOY_PROJECT_DIR" || true
    fi
}
trap cleanup EXIT

# shellcheck disable=SC1090
. "$LIB_PATH"

echo -e "${BLUE}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║              Dograh Admin Domain Setup                       ║"
echo "║   Attach a second (admin-only) hostname to this install      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

if [[ $EUID -ne 0 ]]; then
    dograh_fail "This script must be run as root or with sudo"
fi

# Unlike setup_custom_domain.sh (a standalone download that lives *next to*
# the dograh/ checkout), this script ships inside the repo at
# scripts/setup_admin_domain.sh — so the project root is one level up from
# this script's own location, not a "dograh" subdirectory of the caller's cwd.
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ ! -f "$PROJECT_DIR/docker-compose.yaml" ]]; then
    dograh_fail "Could not find docker-compose.yaml in $PROJECT_DIR — run this from inside your Dograh checkout (scripts/setup_admin_domain.sh)."
fi

echo -e "${YELLOW}Enter the admin hostname (e.g., admin-voice.yourcompany.com):${NC}"
read -p "> " ADMIN_DOMAIN_NAME
[[ -n "$ADMIN_DOMAIN_NAME" ]] || dograh_fail "Domain name cannot be empty"

if ! [[ "$ADMIN_DOMAIN_NAME" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$ ]]; then
    dograh_fail "Invalid domain name format"
fi

echo -e "${YELLOW}Enter your email address for SSL certificate notifications:${NC}"
read -p "> " EMAIL_ADDRESS
[[ -n "$EMAIL_ADDRESS" ]] || dograh_fail "Email address cannot be empty (required by Let's Encrypt)"

echo ""
echo -e "${GREEN}Configuration:${NC}"
echo -e "  Admin domain: ${BLUE}$ADMIN_DOMAIN_NAME${NC}"
echo -e "  Email:        ${BLUE}$EMAIL_ADDRESS${NC}"
echo ""

echo -e "${BLUE}[1/6] Verifying DNS configuration...${NC}"
SERVER_IP="$(curl -s ifconfig.me || curl -s icanhazip.com || echo "")"
RESOLVED_IP="$(dig +short "$ADMIN_DOMAIN_NAME" | tail -1)"

if [[ -z "$SERVER_IP" ]]; then
    dograh_warn "Warning: Could not detect server's public IP"
elif [[ "$RESOLVED_IP" != "$SERVER_IP" ]]; then
    echo -e "${YELLOW}Warning: Domain '$ADMIN_DOMAIN_NAME' resolves to '$RESOLVED_IP' but this server's IP is '$SERVER_IP'${NC}"
    echo -e "${YELLOW}Make sure your DNS A record points to this server before proceeding.${NC}"
    echo ""
    read -p "Continue anyway? (y/N) > " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy]$ ]]; then
        echo -e "${RED}Setup cancelled. Please configure DNS and try again.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✓ DNS is correctly configured (${RESOLVED_IP})${NC}"
fi

echo -e "${BLUE}[2/6] Installing Certbot...${NC}"
dograh_install_certbot || dograh_fail "Could not install certbot. Please install it manually and re-run."
echo -e "${GREEN}✓ Certbot installed${NC}"

echo -e "${BLUE}[3/6] Adding $ADMIN_DOMAIN_NAME to .env and starting services...${NC}"
cd "$PROJECT_DIR"
DOGRAH_DEPLOY_PROJECT_DIR="$(pwd)"

dograh_require_init_compose_layout "$(pwd)"

dograh_load_env_file .env
[[ -n "${PUBLIC_HOST:-}" ]] || dograh_fail "PUBLIC_HOST is not set — run setup_remote.sh / setup_custom_domain.sh first so this install has a canonical domain."

dograh_set_env_key .env ADMIN_HOST "$ADMIN_DOMAIN_NAME"
dograh_prepare_remote_install "$(pwd)"

# Bring the stack up so dograh-init re-renders nginx with BOTH hostnames in
# server_name, still on the existing certificate (which doesn't cover
# ADMIN_HOST yet — that's expected and fixed by the SAN reissue below).
./remote_up.sh

echo -e "${BLUE}Waiting for nginx to answer on port 80...${NC}"
nginx_ready=0
for ((i=1; i<=60; i++)); do
    if curl -s -o /dev/null --max-time 3 "http://127.0.0.1/"; then
        nginx_ready=1
        break
    fi
    sleep 2
done
[[ "$nginx_ready" == "1" ]] || dograh_fail "nginx did not come up on port 80; cannot run the ACME challenge."
echo -e "${GREEN}✓ Services running and serving the ACME challenge${NC}"

echo -e "${BLUE}[4/6] Obtaining a SAN certificate for $PUBLIC_HOST + $ADMIN_DOMAIN_NAME...${NC}"
if ! dograh_issue_letsencrypt_webroot "$(pwd)" "$PUBLIC_HOST" "$EMAIL_ADDRESS" "$ADMIN_DOMAIN_NAME"; then
    echo -e "${RED}✗ Certificate issuance failed${NC}"
    echo ""
    echo -e "${YELLOW}Common causes:${NC}"
    echo "  - Port 80 not reachable from the internet (open it in your firewall)"
    echo "  - DNS A record for $ADMIN_DOMAIN_NAME does not point to this server yet"
    echo "  - Let's Encrypt rate limit reached (wait, then retry)"
    echo ""
    echo -e "The stack is still running with the previous certificate (nginx now"
    echo -e "listens for $ADMIN_DOMAIN_NAME too, but HTTPS on it will show a"
    echo -e "certificate mismatch until this step succeeds)."
    echo -e "After fixing the issue, re-run: ${BLUE}sudo ./scripts/setup_admin_domain.sh${NC}"
    echo ""
    exit 1
fi
echo -e "${GREEN}✓ Certificate issued and copied to certs/${NC}"

echo -e "${BLUE}[5/6] Loading the new certificate (restarting nginx)...${NC}"
docker compose --profile remote restart nginx >/dev/null 2>&1 || true
echo -e "${GREEN}✓ nginx restarted${NC}"

echo -e "${BLUE}[6/6] Refreshing automatic certificate renewal...${NC}"
dograh_install_cert_renewal_hook "$(pwd)" "$PUBLIC_HOST"
if certbot renew --dry-run --quiet; then
    echo -e "${GREEN}✓ Auto-renewal configured and tested${NC}"
else
    echo -e "${YELLOW}⚠ Auto-renewal dry-run had issues, but the certificate is installed${NC}"
fi

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              Admin Domain Setup Complete!                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${YELLOW}This install now answers on both:${NC}"
echo ""
echo -e "  ${BLUE}https://$PUBLIC_HOST${NC}          (canonical app domain)"
echo -e "  ${BLUE}https://$ADMIN_DOMAIN_NAME${NC}  (admin domain)"
echo ""
echo -e "${YELLOW}Next step:${NC} set NEXT_PUBLIC_APP_URL=https://$PUBLIC_HOST and"
echo -e "NEXT_PUBLIC_ADMIN_URL=https://$ADMIN_DOMAIN_NAME in .env, then"
echo -e "./remote_up.sh --build so post-login redirects route by role."
echo ""
