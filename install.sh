#!/bin/bash
# ============================================================================
#  User Manager - one-command installer / updater
#  Style/pattern inspired by 3x-ui's install.sh (root check, OS detection,
#  colored output, non-interactive support, single-command re-run for update).
#
#  Usage (fresh install or update - safe to re-run):
#    sudo bash install.sh
#
#  Where does the source code come from? (auto-detected, in this order)
#    1) A usermanager.tar.gz / usermanager_src.tar.gz sitting next to this
#       script (offline / private-repo friendly - just scp both files up).
#    2) git clone, if USERMANAGER_REPO_URL is exported or set below.
#    3) If neither is found, the script prints exactly what to do (including
#       the one-liner to run from your own Windows/Mac/Linux machine).
#
#  Non-interactive install (e.g. for automation/CI):
#    USERMANAGER_NONINTERACTIVE=1 \
#    USERMANAGER_PANEL_PORT=80 \
#    USERMANAGER_ADMIN_USERNAME=admin \
#    USERMANAGER_ADMIN_PASSWORD='change-me' \
#    sudo -E bash install.sh
# ============================================================================

set -u

# ---- default source repo (override by exporting USERMANAGER_REPO_URL) ----
USERMANAGER_REPO_URL="${USERMANAGER_REPO_URL:-https://github.com/mohammadrezafathi92-web/usermanager.git}"
USERMANAGER_REPO_BRANCH="${USERMANAGER_REPO_BRANCH:-main}"

red='\033[0;31m'
green='\033[0;32m'
blue='\033[0;34m'
yellow='\033[0;33m'
plain='\033[0m'

log_info()  { echo -e "${green}[INFO]${plain} $*"; }
log_warn()  { echo -e "${yellow}[WARN]${plain} $*"; }
log_err()   { echo -e "${red}[ERROR]${plain} $*"; }
log_step()  { echo -e "${blue}==>${plain} $*"; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" &>/dev/null && pwd)"

# ---------------------------------------------------------------------------
# 0) root check
# ---------------------------------------------------------------------------
if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    log_err "Please run this script as root (e.g. sudo bash install.sh)."
    exit 1
fi

# ---------------------------------------------------------------------------
# 1) parse flags
# ---------------------------------------------------------------------------
CLI_ACTION="install"
CLI_SOURCE_PATH=""
CLI_SOURCE_URL=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        update) CLI_ACTION="update"; shift ;;
        uninstall) CLI_ACTION="uninstall"; shift ;;
        --source)
            CLI_SOURCE_PATH="$2"; shift 2 ;;
        --repo)
            CLI_SOURCE_URL="$2"; shift 2 ;;
        --port)
            USERMANAGER_PANEL_PORT="$2"; shift 2 ;;
        --dir)
            USERMANAGER_DIR="$2"; shift 2 ;;
        --yes|-y)
            USERMANAGER_NONINTERACTIVE=1; shift ;;
        -h|--help)
            cat <<EOF
Usage: sudo bash install.sh [install|update|uninstall] [options]

Options:
  --source <path>   Use this local tarball/directory as the project source
  --repo <url>       git clone from this URL instead of a local tarball
  --port <port>      Panel web port (default 80, or keep existing on update)
  --dir <path>        Install directory (default: /opt/usermanager, or the
                       existing /root/usermanager if already installed there)
  --yes, -y            Non-interactive (accept all defaults / env overrides)
EOF
            exit 0 ;;
        *)
            log_warn "Unknown argument: $1"; shift ;;
    esac
done

if [[ "${USERMANAGER_NONINTERACTIVE:-0}" == "1" ]] || [[ ! -t 0 ]]; then
    NONINTERACTIVE=1
else
    NONINTERACTIVE=0
fi

# ---------------------------------------------------------------------------
# 2) OS detection (only used for a friendlier log line + docker bootstrap)
# ---------------------------------------------------------------------------
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_RELEASE="${ID:-unknown}"
elif [[ -f /usr/lib/os-release ]]; then
    . /usr/lib/os-release
    OS_RELEASE="${ID:-unknown}"
else
    OS_RELEASE="unknown"
fi
log_info "Detected OS: ${OS_RELEASE}"

# ---------------------------------------------------------------------------
# 3) install dir (reuse the existing production layout if present)
# ---------------------------------------------------------------------------
if [[ -n "${USERMANAGER_DIR:-}" ]]; then
    INSTALL_DIR="$USERMANAGER_DIR"
elif [[ -f /root/usermanager/docker-compose.yml ]]; then
    INSTALL_DIR="/root/usermanager"
else
    INSTALL_DIR="/opt/usermanager"
fi
log_info "Install directory: ${INSTALL_DIR}"

STATE_DIR="/etc/usermanager"
mkdir -p "$STATE_DIR"
echo "$INSTALL_DIR" > "$STATE_DIR/install_dir"

# ---------------------------------------------------------------------------
# uninstall path (short-circuit, doesn't need docker/source logic below)
# ---------------------------------------------------------------------------
if [[ "$CLI_ACTION" == "uninstall" ]]; then
    log_warn "This will stop and remove the User Manager containers."
    if [[ "$NONINTERACTIVE" != "1" ]]; then
        read -r -p "Also delete the database and all data in ${INSTALL_DIR}/backend/data ? [y/N] " confirm_wipe
    else
        confirm_wipe="n"
    fi
    if [[ -d "$INSTALL_DIR" ]]; then
        (cd "$INSTALL_DIR" && docker compose down) || true
        if [[ "${confirm_wipe:-n}" =~ ^[Yy]$ ]]; then
            rm -rf "${INSTALL_DIR:?}/backend/data"
            log_warn "Data directory removed."
        fi
    fi
    rm -f /usr/local/bin/usermanager
    log_info "Uninstalled. Project files kept at ${INSTALL_DIR} (data preserved unless you confirmed the wipe)."
    exit 0
fi

# ---------------------------------------------------------------------------
# 4) docker + docker compose
# ---------------------------------------------------------------------------
install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log_info "Docker + Compose plugin already installed, skipping."
        return
    fi
    log_step "Installing Docker (official get.docker.com script)..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    systemctl enable --now docker >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true
    if ! docker compose version >/dev/null 2>&1; then
        log_err "Docker Compose plugin still missing after install. Please install it manually and re-run."
        exit 1
    fi
}
install_docker

# ---------------------------------------------------------------------------
# 5) get the source code
# ---------------------------------------------------------------------------
find_local_tarball() {
    for f in "$SCRIPT_DIR"/usermanager.tar.gz "$SCRIPT_DIR"/usermanager_src.tar.gz "$SCRIPT_DIR"/usermanager_update*.zip; do
        [[ -e "$f" ]] && { echo "$f"; return 0; }
    done
    return 1
}

fetch_source() {
    local mode=""
    local tarball=""

    if [[ -n "$CLI_SOURCE_PATH" ]]; then
        tarball="$CLI_SOURCE_PATH"
        mode="local"
    elif tarball="$(find_local_tarball)"; then
        mode="local"
    elif [[ -n "$CLI_SOURCE_URL" || -n "$USERMANAGER_REPO_URL" ]]; then
        mode="git"
    elif [[ -d "$INSTALL_DIR/backend" && -d "$INSTALL_DIR/frontend" ]]; then
        # Already has a project on disk (e.g. this is a re-run after a manual
        # scp/unzip) - just use what's already there.
        log_info "Existing project found at ${INSTALL_DIR}, using it as-is."
        return 0
    else
        log_err "No source found. Do one of the following, then re-run:"
        echo "  1) Put usermanager.tar.gz next to install.sh, e.g.:"
        echo "       tar czf usermanager.tar.gz --exclude node_modules --exclude .git usermanager"
        echo "       scp usermanager.tar.gz install.sh root@THIS_SERVER:/root/"
        echo "  2) Or set a git repo:  export USERMANAGER_REPO_URL=git@github.com:you/usermanager.git"
        echo "  3) Or run install_from_windows.ps1 on your own PC - it does step 1 for you"
        echo "     automatically (tar + scp + ssh + remote install in one command)."
        exit 1
    fi

    mkdir -p "$INSTALL_DIR"

    if [[ "$mode" == "local" ]]; then
        log_step "Installing from local archive: ${tarball}"
        case "$tarball" in
            *.zip)
                command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; } 2>/dev/null || true
                unzip -o "$tarball" -d "$INSTALL_DIR" >/dev/null
                ;;
            *)
                tar xzf "$tarball" -C "$INSTALL_DIR" --strip-components=1 2>/dev/null \
                    || tar xzf "$tarball" -C "$INSTALL_DIR"
                ;;
        esac
    else
        local repo_url="${CLI_SOURCE_URL:-$USERMANAGER_REPO_URL}"
        log_step "Cloning ${repo_url} (branch: ${USERMANAGER_REPO_BRANCH})..."
        command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git; } 2>/dev/null || true
        if [[ -d "$INSTALL_DIR/.git" ]]; then
            (cd "$INSTALL_DIR" && git fetch origin "$USERMANAGER_REPO_BRANCH" && git reset --hard "origin/$USERMANAGER_REPO_BRANCH")
        else
            git clone --branch "$USERMANAGER_REPO_BRANCH" --depth 1 "$repo_url" "$INSTALL_DIR"
        fi
    fi

    # tolerate an extra nested "usermanager/" folder from a tar/zip made of
    # the parent directory
    if [[ ! -f "$INSTALL_DIR/docker-compose.yml" && -f "$INSTALL_DIR/usermanager/docker-compose.yml" ]]; then
        shopt -s dotglob
        mv "$INSTALL_DIR"/usermanager/* "$INSTALL_DIR"/
        rmdir "$INSTALL_DIR/usermanager" 2>/dev/null || true
        shopt -u dotglob
    fi
}
fetch_source

cd "$INSTALL_DIR" || { log_err "Install directory ${INSTALL_DIR} not found after fetch."; exit 1; }

if [[ ! -f docker-compose.yml ]]; then
    log_err "docker-compose.yml not found in ${INSTALL_DIR}. Source fetch looks incomplete."
    exit 1
fi

# ---------------------------------------------------------------------------
# 6) configure (.env) - only on first install, or if --port changed on update
# ---------------------------------------------------------------------------
gen_secret() { openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | md5sum | cut -d' ' -f1; }
gen_password() { openssl rand -base64 16 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c14; }
detect_public_ip() {
    curl -fsS --max-time 3 https://ifconfig.me 2>/dev/null \
        || curl -fsS --max-time 3 https://icanhazip.com 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}' \
        || echo ""
}

FIRST_INSTALL=0
if [[ ! -f backend/.env ]]; then
    FIRST_INSTALL=1
fi

PANEL_PORT="${USERMANAGER_PANEL_PORT:-80}"
ADMIN_USERNAME="${USERMANAGER_ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${USERMANAGER_ADMIN_PASSWORD:-}"

if [[ "$FIRST_INSTALL" == "1" ]]; then
    if [[ "$NONINTERACTIVE" != "1" ]]; then
        echo
        read -r -p "Panel web port [80]: " _p; [[ -n "${_p:-}" ]] && PANEL_PORT="$_p"
        read -r -p "Admin username [admin]: " _u; [[ -n "${_u:-}" ]] && ADMIN_USERNAME="$_u"
        read -r -s -p "Admin password [leave empty to auto-generate]: " _pw; echo
        [[ -n "${_pw:-}" ]] && ADMIN_PASSWORD="$_pw"
    fi
    [[ -z "$ADMIN_PASSWORD" ]] && ADMIN_PASSWORD="$(gen_password)"

    PUBLIC_IP="$(detect_public_ip)"
    SECRET_KEY="$(gen_secret)"

    log_step "Writing backend/.env..."
    cat > backend/.env <<EOF
SECRET_KEY=${SECRET_KEY}
DATABASE_URL=sqlite:////app/data/usermanager.db
DEFAULT_ADMIN_USERNAME=${ADMIN_USERNAME}
DEFAULT_ADMIN_PASSWORD=${ADMIN_PASSWORD}
POLL_INTERVAL_SECONDS=30

RADIUS_ENABLED=true
RADIUS_BIND_HOST=0.0.0.0
RADIUS_AUTH_PORT=1812
RADIUS_ACCT_PORT=1813
RADIUS_HOSTS_REFRESH_SECONDS=60
PANEL_PUBLIC_HOST=${PUBLIC_IP}
EOF
else
    log_info "backend/.env already exists, leaving admin credentials untouched (this is an update)."
    PANEL_PORT="${USERMANAGER_PANEL_PORT:-}"
fi

# apply a custom panel port to docker-compose.yml (same targeted string
# replace the panel's own "change panel port" feature uses internally)
if [[ -n "$PANEL_PORT" && "$PANEL_PORT" != "80" ]]; then
    if grep -q '"80:80"' docker-compose.yml; then
        sed -i "s/\"80:80\"/\"${PANEL_PORT}:80\"/" docker-compose.yml
        log_info "Panel port set to ${PANEL_PORT}."
    fi
fi
CURRENT_PORT="$(grep -oE '"[0-9]+:80"' docker-compose.yml | head -1 | grep -oE '^"[0-9]+' | tr -d '"')"
CURRENT_PORT="${CURRENT_PORT:-80}"

# ---------------------------------------------------------------------------
# 7) build + start
# ---------------------------------------------------------------------------
log_step "Building and starting containers (this can take a few minutes on first install)..."
docker compose up -d --build

log_step "Waiting for the panel to come up..."
ready=0
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${CURRENT_PORT}" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
[[ "$ready" == "1" ]] || log_warn "Panel didn't answer yet after 60s - check: docker compose logs -f frontend backend"

# ---------------------------------------------------------------------------
# 8) install the `usermanager` management command (menu, like x-ui's own CLI)
# ---------------------------------------------------------------------------
install_cli() {
    cat > /usr/local/bin/usermanager <<'CLIEOF'
#!/bin/bash
STATE_DIR="/etc/usermanager"
INSTALL_DIR="$(cat "$STATE_DIR/install_dir" 2>/dev/null || echo /opt/usermanager)"
cd "$INSTALL_DIR" || { echo "Install dir not found: $INSTALL_DIR"; exit 1; }

action="${1:-menu}"
case "$action" in
    start)   docker compose up -d ;;
    stop)    docker compose stop ;;
    restart) docker compose restart ;;
    status)  docker compose ps ;;
    logs)    docker compose logs -f --tail=200 "${2:-}" ;;
    update)
        SCRIPT_SRC="$INSTALL_DIR/install.sh"
        [[ -f "$SCRIPT_SRC" ]] || { echo "install.sh not found in $INSTALL_DIR"; exit 1; }
        bash "$SCRIPT_SRC" update
        ;;
    uninstall)
        bash "$INSTALL_DIR/install.sh" uninstall
        ;;
    menu|*)
        echo "======================================"
        echo " User Manager control panel"
        echo "======================================"
        echo " 1) Status"
        echo " 2) Logs (backend)"
        echo " 3) Restart"
        echo " 4) Stop"
        echo " 5) Start"
        echo " 6) Update to latest source"
        echo " 7) Uninstall"
        echo " 0) Exit"
        read -r -p "Choose: " choice
        case "$choice" in
            1) docker compose ps ;;
            2) docker compose logs -f --tail=200 backend ;;
            3) docker compose restart ;;
            4) docker compose stop ;;
            5) docker compose up -d ;;
            6) bash "$INSTALL_DIR/install.sh" update ;;
            7) bash "$INSTALL_DIR/install.sh" uninstall ;;
            *) exit 0 ;;
        esac
        ;;
esac
CLIEOF
    chmod +x /usr/local/bin/usermanager
}
install_cli

# ---------------------------------------------------------------------------
# 9) summary
# ---------------------------------------------------------------------------
PUBLIC_IP_DISPLAY="$(detect_public_ip)"
[[ -z "$PUBLIC_IP_DISPLAY" ]] && PUBLIC_IP_DISPLAY="SERVER_IP"

echo
echo -e "${green}============================================================${plain}"
echo -e "${green} User Manager is up.${plain}"
echo -e "${green}============================================================${plain}"
echo -e " Panel:        http://${PUBLIC_IP_DISPLAY}:${CURRENT_PORT}"
echo -e " API/Swagger:  http://${PUBLIC_IP_DISPLAY}:8000/docs"
if [[ "$FIRST_INSTALL" == "1" ]]; then
echo -e " Admin user:   ${ADMIN_USERNAME}"
echo -e " Admin pass:   ${ADMIN_PASSWORD}"
echo -e " ${yellow}Change the admin password from Settings after your first login.${plain}"
fi
echo -e " Project dir:  ${INSTALL_DIR}"
echo -e " Manage it any time with:  ${blue}usermanager${plain}   (or: usermanager status|logs|restart|update|uninstall)"
echo -e "${green}============================================================${plain}"
