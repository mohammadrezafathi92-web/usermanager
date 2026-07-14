# Multi-stage build - keeps the final image (what actually sits on disk and
# runs) free of gcc/libffi-dev/binutils, which was previously baked in
# permanently just to compile pip's C-extension packages. On small-disk VPS
# boxes that gcc toolchain alone (~200MB+, incl. cc1/lto1) was enough to
# blow the disk during "docker compose up --build". None of that is needed
# at RUNTIME - only while pip is resolving/building wheels.

# ---- stage 1: build/install python deps (gcc available here only) ----
FROM python:3.11-slim AS pydeps

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- stage 2: fetch docker CLI + compose plugin static binaries (no gcc needed) ----
FROM python:3.11-slim AS dockercli

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
# Docker CLI + compose plugin (client-only, static binaries - no engine, no
# daemon) so the panel's own "تغییر پورت پنل وب" feature can talk to the
# HOST's docker daemon over the socket mounted in at /var/run/docker.sock
# (docker-compose.yml) - see services/local_deploy.py's module docstring.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) docker_arch="x86_64" ;; \
        arm64) docker_arch="aarch64" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${docker_arch}/docker-25.0.3.tgz" -o /tmp/docker.tgz; \
    mkdir -p /out/bin; \
    tar xzf /tmp/docker.tgz --strip-components=1 -C /out/bin docker/docker; \
    rm /tmp/docker.tgz; \
    mkdir -p /out/cli-plugins; \
    compose_arch="$([ "$arch" = "arm64" ] && echo aarch64 || echo x86_64)"; \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-${compose_arch}" -o /out/cli-plugins/docker-compose; \
    chmod +x /out/bin/docker /out/cli-plugins/docker-compose

# ---- stage 3: final runtime image - only what's actually needed to run ----
FROM python:3.11-slim

WORKDIR /app

# curl stays (used at runtime by the panel's own health checks / HA sync,
# not just at build time) - gcc/libffi-dev do NOT come along.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY --from=pydeps /install /usr/local
COPY --from=dockercli /out/bin/docker /usr/local/bin/docker
COPY --from=dockercli /out/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose

COPY app ./app

EXPOSE 8000
EXPOSE 1812/udp
EXPOSE 1813/udp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
