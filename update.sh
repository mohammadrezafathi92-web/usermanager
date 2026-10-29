#!/usr/bin/env bash
# One-command updater: pulls the latest code from GitHub and rebuilds +
# restarts BOTH the backend and frontend containers.
#
# One-time setup (per server), if you haven't already installed via git:
#   cd /opt   (or wherever you want the project to live)
#   git clone https://github.com/mohammadrezafathi92-web/usermanager.git
#   cd usermanager
#   cp backend/.env.example backend/.env   # then edit SECRET_KEY / admin password
#   docker compose up -d --build
#
# From then on, whenever there's a new version on GitHub, from inside the
# project folder (same folder as docker-compose.yml):
#   bash update.sh
set -euo pipefail
umask 077

main() {
cd "${UM_UPDATE_PROJECT_DIR:-$(dirname "$0")}"

die() { echo "STOP: $*" >&2; exit 1; }
stage() { echo "UPDATE_STAGE=$1"; }
manifest_digest() {
    python3 - "$1" <<'PY'
import hashlib, json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
print(hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
PY
}
stage preflight
target_arg=""
allow_stalled_xray_delete=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            [[ $# -ge 2 && $2 =~ ^[0-9a-f]{40}$ && -z $target_arg ]] || die "invalid target"
            target_arg=$2; shift 2 ;;
        --allow-stalled-xray-delete)
            [[ $# -ge 2 && $2 =~ ^[1-9][0-9]*$ && -z $allow_stalled_xray_delete ]] || die "invalid stalled-operation id"
            allow_stalled_xray_delete=$2; shift 2 ;;
        *) die "usage: bash update.sh [--target <full SHA>] [--allow-stalled-xray-delete <operation id>]" ;;
    esac
done
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout"
[[ -z $(git status --porcelain --untracked-files=no) ]] || die "tracked changes present"
[[ $(git symbolic-ref --short -q HEAD) == main ]] || die "checkout must be on main"
command -v flock >/dev/null || die "flock is required"
command -v timeout >/dev/null || die "coreutils timeout is required"
exec 9>"$(git rev-parse --git-path usermanager-update.lock)"
flock -n 9 || die "another update is running"
previous=$(git rev-parse HEAD)
# Remote errors can contain credential-bearing URLs; never echo them.
timeout 90 git fetch --no-tags origin main >/dev/null 2>&1 || die "fetch failed or timed out (remote details suppressed)"
target=$(git rev-parse refs/remotes/origin/main)
[[ -z $target_arg || $target == "$target_arg" ]] || die "main differs from --target"
git merge-base --is-ancestor "$previous" "$target" || die "main cannot fast-forward"
docker compose exec -T backend python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=5).close()' >/dev/null 2>&1 || die "backend unhealthy"
# Base images BEFORE anything changes: a registry that cannot be reached
# must stop the update here - not after the checkout, half way through a
# build. Exactly the references the target's Dockerfiles name (their pins
# are kept); an image already on this host needs no network at all.
stage images
base_images=$(for file in backend/Dockerfile frontend/Dockerfile; do
        git show "$target:$file" 2>/dev/null || { echo "UNREADABLE $file"; }
    done | awk 'toupper($1) == "UNREADABLE" { print "!" $2; next }
        toupper($1) == "FROM" {
            ref = ""; alias = ""
            for (i = 2; i <= NF; i++) {
                if ($i ~ /^--/) continue
                if (ref == "") { ref = $i; continue }
                if (toupper($i) == "AS" && i < NF) alias = $(i + 1)
            }
            if (alias != "") stages[alias] = 1
            refs[++n] = ref
        }
        END { for (i = 1; i <= n; i++) if (!(refs[i] in stages)) print refs[i] }' | sort -u)
[[ -n $base_images ]] || die "no base images found in the target's Dockerfiles"
for image in $base_images; do
    [[ $image != !* ]] || die "cannot read ${image#!} at the target commit"
    [[ $image =~ ^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$ ]] || die "unexpected base image reference"
    if docker image inspect "$image" >/dev/null 2>&1; then
        echo "BASE_IMAGE=$image present"
        continue
    fi
    pulled=false
    for attempt in 1 2 3; do
        if timeout 600 docker pull --quiet "$image" >/dev/null 2>&1; then pulled=true; break; fi
        echo "base image $image: pull attempt $attempt of 3 failed" >&2
        [[ $attempt == 3 ]] || sleep $((attempt * 30))
    done
    $pulled || die "base image $image could not be pulled (registry unreachable?); nothing was changed - run again later"
    echo "BASE_IMAGE=$image pulled"
done
tag="update_$(date -u +%Y%m%dT%H%M%SZ)_$$"
stage backup
backup_root=${UM_UPDATE_BACKUP_DIR:-/var/backups/usermanager}
[[ $backup_root == /* ]] || die "backup directory must be absolute"
mkdir -p "$backup_root"
python3 -c 'import shutil,sys; sys.exit(0 if shutil.disk_usage(sys.argv[1]).free >= 2*1024**3 else 1)' "$backup_root" || die "less than 2 GiB free at backup destination"
backup_dir="$backup_root/$tag"
mkdir -m 700 "$backup_dir"
cp "${UM_UPDATE_HELPER_PATH:-backend/scripts/update_preflight.py}" "$backup_dir/helper.py"
printf '%s\n' "$previous" > "$backup_dir/previous_commit"
printf '%s\n' "$target" > "$backup_dir/target_commit"
echo "BACKUP_DIRECTORY=$backup_dir"
backup_env=()
if [[ -n $allow_stalled_xray_delete ]]; then
    backup_env=(-e "UM_UPDATE_STALLED_XRAY_DELETE_ID=$allow_stalled_xray_delete")
fi
docker compose exec -T "${backup_env[@]}" backend python - backup "$tag" < "$backup_dir/helper.py" > "$backup_dir/manifest.json"
operation_hold_id=$(python3 -c 'import json,sys; h=json.load(open(sys.argv[1])).get("operation_hold") or {}; print(h.get("operation_id", ""))' "$backup_dir/manifest.json")
operation_hold_owner=$(python3 -c 'import json,sys; h=json.load(open(sys.argv[1])).get("operation_hold") or {}; print(h.get("lease_owner", ""))' "$backup_dir/manifest.json")
operation_hold_epoch=$(python3 -c 'import json,sys; h=json.load(open(sys.argv[1])).get("operation_hold") or {}; print(h.get("fencing_epoch", ""))' "$backup_dir/manifest.json")
hold_active=false
if [[ -n $operation_hold_id ]]; then
    hold_active=true
fi
hold_env=()
if [[ -n $operation_hold_id ]]; then
    hold_env=(-e "UM_UPDATE_STALLED_XRAY_DELETE_ID=$operation_hold_id"
        -e "UM_UPDATE_STALLED_XRAY_DELETE_OWNER=$operation_hold_owner"
        -e "UM_UPDATE_STALLED_XRAY_DELETE_EPOCH=$operation_hold_epoch")
fi
release_operation_hold() {
    [[ $hold_active == true ]] || return 0
    if docker compose exec -T \
        -e "UM_UPDATE_STALLED_XRAY_DELETE_ID=$operation_hold_id" \
        -e "UM_UPDATE_STALLED_XRAY_DELETE_OWNER=$operation_hold_owner" \
        -e "UM_UPDATE_STALLED_XRAY_DELETE_EPOCH=$operation_hold_epoch" \
        backend python - release-operation-hold < "$backup_dir/helper.py" >/dev/null 2>&1; then
        hold_active=false
        return 0
    fi
    echo "WARNING: operation hold could not be released; it expires by DB time within one hour" >&2
    return 1
}
trap 'release_operation_hold || true' EXIT
if [[ -n $allow_stalled_xray_delete ]]; then
    [[ $operation_hold_id == "$allow_stalled_xray_delete" && $operation_hold_owner =~ ^op:${operation_hold_id}:[1-9][0-9]*$ && $operation_hold_epoch =~ ^[1-9][0-9]*$ ]] || die "invalid deployment hold"
elif [[ -n $operation_hold_id ]]; then
    die "unexpected deployment hold"
fi
container_dir=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["directory"])' "$backup_dir/manifest.json")
manifest_sha=$(manifest_digest "$backup_dir/manifest.json")
docker compose cp "backend:$container_dir/." "$backup_dir/"
[[ $(manifest_digest "$backup_dir/manifest.json") == "$manifest_sha" ]] || die "backup manifest changed during copy"
python3 "$backup_dir/helper.py" verify-backup "$backup_dir"
for service in backend frontend; do
    container=$(docker compose ps -q "$service")
    [[ -n $container ]] || die "$service container missing"
    image=$(docker inspect --format '{{.Image}}' "$container")
    docker image tag "$image" "usermanager-$service:pre-$tag"
done
stage checkout
echo "PREVIOUS_COMMIT=$previous"
echo "ROLLBACK_IMAGES=usermanager-backend:pre-$tag usermanager-frontend:pre-$tag"
git merge --ff-only "$target"
[[ $(git rev-parse HEAD) == "$target" ]] || die "unexpected checkout head"
bash backend/scripts/ensure_host_identity.sh
stage build
docker compose build backend frontend
docker compose exec -T "${hold_env[@]}" backend python - snapshot < "$backup_dir/helper.py" > "$backup_dir/before_restart.json"
python3 "$backup_dir/helper.py" compare "$backup_dir/runtime.json" "$backup_dir/before_restart.json"
stage backend
docker compose up -d --no-build --no-deps backend
healthy=false
for attempt in $(seq 1 60); do
    if docker compose exec -T backend python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=3).close()' >/dev/null 2>&1; then
        healthy=true; break
    fi
    sleep 2
done
$healthy || die "backend health failed; no automatic rollback or DB restore"
docker compose exec -T "${hold_env[@]}" backend python - snapshot < "$backup_dir/helper.py" > "$backup_dir/after_restart.json"
python3 "$backup_dir/helper.py" compare "$backup_dir/runtime.json" "$backup_dir/after_restart.json"
stage frontend
docker compose up -d --no-build --no-deps frontend
healthy=false
for attempt in $(seq 1 30); do
    if docker compose exec -T frontend wget -q -T 3 -O /dev/null http://127.0.0.1/ >/dev/null 2>&1; then
        healthy=true; break
    fi
    sleep 2
done
$healthy || die "frontend health failed"
if [[ -n $operation_hold_id ]]; then
    docker compose exec -T "${hold_env[@]}" backend python - validate-operation-hold < "$backup_dir/helper.py" >/dev/null || die "stalled-operation hold is no longer valid"
    # Keep the lease until the superadmin recovery action takes it over. This
    # prevents the worker from retrying the unknown remote delete first.
    hold_active=false
    echo "OPERATION_HOLD_PENDING_ADMIN=$operation_hold_id (one-hour DB-clock lease)"
fi
echo "UPDATE_OK commit=$target; modes preserved; license-server untouched"
echo "Backups and rollback references: $backup_dir"
}
# Parse the whole function before git fast-forwards this script on disk.
main "$@"
