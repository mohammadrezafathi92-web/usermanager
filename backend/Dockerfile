# Two things used to be baked into EVERY backend image build and were the
# main reason builds were slow/heavy (fatal on small-disk VPS boxes):
#   1) gcc/libffi-dev, installed unconditionally "just in case" a pip package
#      needed to compile a C extension. In practice cryptography/bcrypt/
#      paramiko's transitive deps (PyNaCl) all ship prebuilt manylinux
#      wheels for this base image's platform (glibc x86_64/arm64), so no
#      compiler is normally needed at all. Below we try a wheels-only
#      install first; gcc is only installed (and then purged again, in the
#      SAME layer so it never actually lands on disk) as a fallback if some
#      future dependency genuinely lacks a wheel.
#   2) The docker CLI + compose plugin static binaries, downloaded on every
#      single build even though they're only used by one rarely-used admin
#      feature ("تنظیمات > پورت پنل وب" - see services/local_deploy.py).
#      That's now downloaded lazily at runtime, once, into the persistent
#      ./backend/data bind mount - not part of the image at all anymore.

# ---- stage 1: install python deps (wheels-only, gcc fallback purged in-layer) ----
FROM python:3.11-slim AS pydeps

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: --prefix=/install -r requirements.txt \
    || (apt-get update \
        && apt-get install -y --no-install-recommends gcc libffi-dev \
        && pip install --no-cache-dir --prefix=/install -r requirements.txt \
        && apt-get purge -y gcc libffi-dev \
        && apt-get autoremove -y \
        && rm -rf /var/lib/apt/lists/*)

# ---- stage 2: final runtime image - only what's actually needed to run ----
FROM python:3.11-slim

WORKDIR /app

# mysqldump/mysql CLI - only used when DATABASE_URL is a mysql+pymysql://
# URL (opt-in at install time, see install.sh's database-engine prompt and
# services/backup.py's module docstring). SQLite installs never call these
# binaries, but they still need to EXIST in every image (an admin can only
# pick MySQL/MariaDB at install time, and we don't build a separate image
# per DB choice) - without this, create_backup()/restore_from_upload()/HA
# sync would fail with FileNotFoundError the moment a MySQL/MariaDB install
# tried to take its first backup. default-mysql-client is Debian's
# MariaDB-based client package (works against both MySQL and MariaDB
# servers) - small (a few MB), so always installing it is cheaper than
# maintaining two image variants.
# git - needed by services/self_update.py ("بروزرسانی پنل" in Settings),
# which pulls the newest commit into the bind-mounted repository from
# inside this container. Note that services/version.py deliberately does
# NOT use it: reading .git by hand keeps the version badge working even on
# installs that predate this line. Roughly 20MB with --no-install-recommends
# (no perl tooling, no docs) - the price of not needing SSH to update.
RUN apt-get update \
    && apt-get install -y --no-install-recommends default-mysql-client git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /install /usr/local

# Small text file, negligible size - kept in the final image (not just the
# pydeps build stage) because services/remote_deploy.py re-reads THIS exact
# file at runtime to upload it to a second server when deploying the remote
# bot ("نصب ربات روی سرور دیگر" in Settings). Without it here, that feature
# fails with FileNotFoundError: /app/requirements.txt the moment it tries to
# read it, since app.tar.gz containing the source is not the same as this.
COPY requirements.txt .

COPY app ./app

EXPOSE 8000
EXPOSE 1812/udp
EXPOSE 1813/udp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
