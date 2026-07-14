FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev curl && rm -rf /var/lib/apt/lists/*

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
    tar xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker; \
    rm /tmp/docker.tgz; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    compose_arch="$([ "$arch" = "arm64" ] && echo aarch64 || echo x86_64)"; \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.24.6/docker-compose-linux-${compose_arch}" -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
EXPOSE 1812/udp
EXPOSE 1813/udp

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
