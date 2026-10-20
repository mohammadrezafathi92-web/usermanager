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
cd "$(dirname "$0")"

die() { echo "STOP: $*" >&2; exit 1; }
target_arg=""
if [[ $# -gt 0 ]]; then
    [[ $# == 2 && $1 == --target && $2 =~ ^[0-9a-f]{40}$ ]] || die "usage: bash update.sh [--target <full SHA>]"
    target_arg=$2
fi
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
tag="update_$(date -u +%Y%m%dT%H%M%SZ)_$$"
backup_root=${UM_UPDATE_BACKUP_DIR:-/var/backups/usermanager}
[[ $backup_root == /* ]] || die "backup directory must be absolute"
mkdir -p "$backup_root"
python3 -c 'import shutil,sys; sys.exit(0 if shutil.disk_usage(sys.argv[1]).free >= 2*1024**3 else 1)' "$backup_root" || die "less than 2 GiB free at backup destination"
backup_dir="$backup_root/$tag"
mkdir -m 700 "$backup_dir"
cp backend/scripts/update_preflight.py "$backup_dir/helper.py"
printf '%s\n' "$previous" > "$backup_dir/previous_commit"
printf '%s\n' "$target" > "$backup_dir/target_commit"
echo "BACKUP_DIRECTORY=$backup_dir"
docker compose exec -T backend python - backup "$tag" < "$backup_dir/helper.py" > "$backup_dir/manifest.json"
container_dir=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["directory"])' "$backup_dir/manifest.json")
docker compose cp "backend:$container_dir/." "$backup_dir/"
python3 "$backup_dir/helper.py" verify-backup "$backup_dir"
for service in backend frontend; do
    container=$(docker compose ps -q "$service")
    [[ -n $container ]] || die "$service container missing"
    image=$(docker inspect --format '{{.Image}}' "$container")
    docker image tag "$image" "usermanager-$service:pre-$tag"
done
echo "PREVIOUS_COMMIT=$previous"
echo "ROLLBACK_IMAGES=usermanager-backend:pre-$tag usermanager-frontend:pre-$tag"
git merge --ff-only "$target"
[[ $(git rev-parse HEAD) == "$target" ]] || die "unexpected checkout head"
bash backend/scripts/ensure_host_identity.sh
docker compose build backend frontend
docker compose exec -T backend python - snapshot < "$backup_dir/helper.py" > "$backup_dir/before_restart.json"
python3 "$backup_dir/helper.py" compare "$backup_dir/runtime.json" "$backup_dir/before_restart.json"
docker compose up -d --no-build --no-deps backend
healthy=false
for attempt in $(seq 1 60); do
    if docker compose exec -T backend python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=3).close()' >/dev/null 2>&1; then
        healthy=true; break
    fi
    sleep 2
done
$healthy || die "backend health failed; no automatic rollback or DB restore"
docker compose exec -T backend python - snapshot < "$backup_dir/helper.py" > "$backup_dir/after_restart.json"
python3 "$backup_dir/helper.py" compare "$backup_dir/runtime.json" "$backup_dir/after_restart.json"
docker compose up -d --no-build --no-deps frontend
healthy=false
for attempt in $(seq 1 30); do
    if docker compose exec -T frontend wget -q -T 3 -O /dev/null http://127.0.0.1/ >/dev/null 2>&1; then
        healthy=true; break
    fi
    sleep 2
done
$healthy || die "frontend health failed"
echo "UPDATE_OK commit=$target; modes preserved; license-server untouched"
echo "Backups and rollback references: $backup_dir"
}
# Parse the whole function before git fast-forwards this script on disk.
main "$@"
