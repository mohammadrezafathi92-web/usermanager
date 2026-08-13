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
set -e

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    echo "این پوشه یه git clone نیست - اول باید یه‌بار پروژه رو با git clone نصب کنی (بالای همین فایل توضیح داده شده)."
    exit 1
fi

echo "==> در حال گرفتن آخرین تغییرات از GitHub ..."
git pull --no-rebase origin main

echo "==> در حال ری‌بیلد و ری‌استارت بک‌اند و فرانت‌اند ..."
docker compose up -d --build

echo "==> انجام شد."
echo "    نسخه: $(cat VERSION 2>/dev/null || echo '?')   کامیت: $(git rev-parse --short HEAD)"
echo "    (همین دو مقدار پایین سایدبار پنل هم نشان داده می‌شوند - اگر با هم"
echo "     نخواندند، یعنی مرورگر نسخه‌ی قدیمی را از کش می‌آورد.)"
echo "==> آخرین لاگ بک‌اند:"
docker compose logs --tail 20 backend
