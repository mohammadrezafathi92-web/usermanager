"""Gives THIS panel its own hostname and an HTTPS certificate.

Why it exists, in order of how much it matters:

  1. A Telegram Mini App is only ever opened over https with a
     publicly-trusted certificate, and the page then has to call this
     panel's own API - a browser refuses to let an https page talk to
     http://<ip>. So a central https server cannot stand in: the panel
     itself has to be https.
  2. Until now every reseller typed their panel password into a plain-HTTP
     login page, over Iranian networks. That was worth fixing regardless of
     Telegram.

How: docker-compose.yml gains a Caddy container (profile "tls") that takes
the host's ports 80 and 443 and reverse-proxies to the existing frontend
nginx. Caddy issues and renews the certificate itself over ACME http-01 -
no certbot, no cron, no renewal that silently stops after a year.

Turning it on moves the frontend off host port 80, because ACME's http-01
challenge has to be answered there and the renewal 60 days from now needs
it just as much. The frontend stays published on a second port so there is
always a way back in by IP if DNS or the certificate ever breaks - locking
the operator out of the panel while debugging the panel's own TLS would be
a spectacular way to fail.

Two pre-flight checks, both learned the hard way on 2026-09-13.

The readiness check asks the question END-TO-END: it fetches
http://<hostname>/api/tls-echo and looks for this install's own token. That
is the same question ACME is about to ask, asked the same way. The first
version compared the hostname's A record against "our public IP" as reported
by an outside service - which on this product is not a fixed value at all,
because the backend container runs its own WireGuard tunnel to reach
Telegram, so the answer is whichever route happens to be up. It refused a
correctly-pointed domain while naming an address the operator had never
seen. Getting this right matters because a failed issuance is rate-limited
to five per hostname per week, with an error that says nothing about why.

The port check refuses if another container already publishes 80 or 443.
That is not a nicety: on the vendor's own server, which also serves
license.netcip.ir on 443, enabling wrote the compose profile, Caddy could
not bind, and from then on EVERY `docker compose up` aborted on it - so
`bash update.sh` could no longer bring the panel up at all. One failed
switch took out the whole stack.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import socket

import requests

from .local_deploy import (
    DeployError,
    _docker_api,
    HOST_PROJECT_DIR,
    _read_env_var,
    _root_env_path,
    _write_env_var,
    ensure_docker_compose_cli,
    spawn_sibling_container,
    verify_host_path,
)

logger = logging.getLogger("panel_tls")

# Where the frontend goes when Caddy takes 80. Deliberately still published:
# see the module docstring on not locking the operator out.
FALLBACK_HTTP_PORT = 8080

# The compose work is logged here rather than returned, because by the time
# it finishes there is no request left to return it to - see _apply().
LOG_PATH = os.path.join(HOST_PROJECT_DIR, "backend", "data", ".panel_tls.log")
_LOG_IN_CONTAINER = os.path.join(os.environ.get("APP_DATA_DIR", "/app/data"), ".panel_tls.log")

# See instance_token(): stable for this process even when the data
# directory cannot be written.
_token_cache: str | None = None

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$"
)


def normalise_domain(raw: str) -> str:
    """Accepts what people actually paste - "https://x.ir/", "X.IR" - and
    returns the bare lowercase hostname, or raises."""
    value = (raw or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/", 1)[0].split(":", 1)[0].strip(".")
    if not value:
        raise DeployError("دامنه وارد نشده است.")
    if not _HOSTNAME_RE.match(value):
        raise DeployError(
            f"«{value}» یک نام دامنه‌ی معتبر نیست. نمونه‌ی درست: panel.example.com"
        )
    # "1.2.3.4" satisfies the pattern above - digits and dots are legal in a
    # hostname - but no public certificate authority issues for a bare IP, so
    # this would fail at ACME with a far less useful message. A real TLD is
    # never all digits, which is the same rule that rules out an IP.
    if value.rsplit(".", 1)[-1].isdigit():
        raise DeployError(
            "برای آی‌پی نمی‌شود گواهی گرفت - یک نام دامنه لازم است. نمونه: panel.example.com"
        )
    return value


def instance_token() -> str:
    """A random string this install answers with, and no other does.

    Written once to the persistent data volume. Not a secret and not a
    credential - it proves only "the thing you just reached is this panel",
    which is the entire question check_dns needs answered.
    """
    global _token_cache
    if _token_cache:
        return _token_cache

    path = os.path.join(os.environ.get("APP_DATA_DIR", "/app/data"), ".panel_tls_token")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
            if token:
                _token_cache = token
                return token
    except OSError:
        pass

    token = secrets.token_urlsafe(16)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(token)
    except OSError:
        # A data directory that cannot be written must not make this return a
        # DIFFERENT token each time - the check compares what came back over
        # the network against this, so an unstable value would report "this
        # hostname reaches a different panel" about the panel itself. Held in
        # memory instead: good for this process's lifetime, which is all the
        # check needs.
        logger.warning("panel_tls: شناسه‌ی نمونه ذخیره نشد (%s) - تا ری‌استارت بعدی در حافظه می‌ماند", path)
    _token_cache = token
    return token


def server_public_ip() -> str | None:
    """This server's address as the internet sees it - INFORMATIONAL ONLY.

    Deliberately no longer what decides anything. Reported 2026-09-13: the
    check announced this server was 31.171.101.237, an address the operator
    had never seen, and a minute later agreed it was 88.218.18.156. Both
    answers came from here, and both were "true": the backend container
    brings up its own WireGuard tunnel to reach Telegram (see
    docker-compose.yml's NET_ADMIN/tun mounts and services/wg_tunnel.py), so
    a request asking "what is my IP" leaves by whichever route happens to be
    up and gets told the tunnel's exit address or the host's.

    An answer that changes minute to minute cannot be the basis of "does
    this hostname point at us" - it produced a flat refusal for a correctly
    configured domain. check_dns asks the question end-to-end instead.
    """
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            resp = requests.get(url, timeout=6)
            if resp.ok:
                ip = resp.text.strip()
                if ip.count(".") == 3:
                    return ip
        except requests.RequestException:
            continue
    return None


def reaches_this_panel(domain: str) -> tuple[bool | None, str]:
    """Does http://<domain>/ actually land on THIS panel?

    The same question ACME is about to ask, asked the same way - from
    outside, over the name, on the port the challenge will use. Immune to
    every reason the IP comparison was not: NAT, a tunnel inside the
    container, a CDN in front, several A records, IPv6.

    Returns (True | False | None, reason). None means "could not tell" -
    nothing answered - which is treated as a warning rather than a refusal,
    since a panel whose own outbound traffic is restricted should still be
    able to proceed.
    """
    url = f"http://{domain}/api/tls-echo"
    try:
        resp = requests.get(url, timeout=8, allow_redirects=False)
    except requests.RequestException:
        return None, "پاسخی از این دامنه روی پورت ۸۰ نیامد - یا هنوز پخش نشده، یا پورت ۸۰ سرور از بیرون بسته است."
    if resp.status_code in (301, 302, 307, 308):
        return None, "این دامنه به جای دیگری ریدایرکت می‌شود - احتمالاً یک CDN یا پروکسی جلوی آن است."
    try:
        token = (resp.json() or {}).get("token")
    except ValueError:
        token = None
    if not token:
        return False, "این دامنه به یک وب‌سرور دیگر می‌رسد، نه به این پنل."
    if token != instance_token():
        return False, "این دامنه به یک پنل دیگر می‌رسد، نه به این یکی."
    return True, "این دامنه از بیرون به همین پنل می‌رسد - آماده‌ی گرفتن گواهی است."


def resolve(domain: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except OSError:
        return []


def check_dns(domain: str) -> dict:
    """Is this hostname ready for a certificate?

    Decided by reaching the panel over the name (reaches_this_panel), not by
    comparing IP addresses - see server_public_ip for why that comparison
    was wrong here. The resolved addresses are still reported, because they
    are what the operator edits, but they no longer decide.
    """
    resolved = resolve(domain)
    reachable, reason = reaches_this_panel(domain)

    if not resolved:
        reachable = False
        reason = "این دامنه به هیچ آدرسی اشاره نمی‌کند. یک رکورد A بسازید که به آی‌پی این سرور اشاره کند و چند دقیقه صبر کنید."

    return {
        # None ("could not tell") is not a refusal - see reaches_this_panel.
        "ok": reachable is True,
        "unknown": reachable is None,
        "domain": domain,
        "resolved": resolved,
        # Informational only now, and labelled as such in the panel.
        "public_ip": None,
        "reason": reason,
        "ports": ports_in_use(),
    }


def ports_in_use() -> list[str]:
    """Which of the ports Caddy needs are already published by some OTHER
    container.

    Reported 2026-09-13 on the vendor's own server, which also hosts
    license.netcip.ir on 443: enabling TLS wrote the compose profile, the
    Caddy container then failed to bind 443 - and from that moment every
    `docker compose up` aborted on it, so an ordinary `bash update.sh`
    could not bring the panel up at all. The conflict is knowable in
    advance, so it is now checked in advance.

    Only docker-published ports are visible from here; something listening
    on the host outside docker is not. That is why the failure path below
    still has to be survivable rather than merely unlikely.
    """
    busy: list[str] = []
    containers = _docker_api("/containers/json") or []
    if isinstance(containers, dict):
        return busy
    for container in containers:
        names = [n.lstrip("/") for n in (container.get("Names") or [])]
        if "usermanager-caddy" in names:
            continue  # our own, from a previous attempt
        for port in container.get("Ports") or []:
            public = port.get("PublicPort")
            if public in (80, 443):
                busy.append(f"{public} ({names[0] if names else '?'})")
    return sorted(set(busy))


def current_state() -> dict:
    env_path = _root_env_path()
    domain = _read_env_var(env_path, "PANEL_DOMAIN") or ""
    profiles = _read_env_var(env_path, "COMPOSE_PROFILES") or ""
    return {
        "domain": domain,
        "enabled": bool(domain) and "tls" in _split_profiles(profiles),
        "fallback_http_port": int(_read_env_var(env_path, "PANEL_WEB_PORT") or 80),
        "email": _read_env_var(env_path, "PANEL_TLS_EMAIL") or "",
    }


def _split_profiles(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").replace(" ", ",").split(",") if p.strip()]


def _set_profile(env_path: str, name: str, on: bool) -> None:
    """COMPOSE_PROFILES is a comma-separated list that may already carry
    someone else's profile (license-server on the vendor's own box, for
    one). Rewriting it wholesale would silently switch that off, so this
    only ever adds or removes the one name."""
    profiles = _split_profiles(_read_env_var(env_path, "COMPOSE_PROFILES") or "")
    if on and name not in profiles:
        profiles.append(name)
    if not on and name in profiles:
        profiles.remove(name)
    _write_env_var(env_path, "COMPOSE_PROFILES", ",".join(profiles))


def _apply(compose_path: str, *, stop_caddy: bool = False) -> None:
    """Runs the compose work in a THROWAWAY SIBLING CONTAINER, not here.

    Reported while testing, as a bare "خطا در ذخیره": this recreates the
    frontend container, and the frontend container is the nginx currently
    proxying the very request that asked for it. Run as an ordinary
    subprocess, it kills its own connection half-way through - the browser
    gets no response at all, so there is not even an error message to show,
    and whether the change actually completed is anyone's guess.

    A sibling container is not part of the compose project being recreated,
    so it survives and finishes the job. Exactly the same reasoning (and the
    same helper) as services/self_update.py's final restart step.

    The command is a single /bin/sh -c so the two compose calls run in
    order; the sibling has no panel to report back to by the time it
    finishes, which is why everything goes to LOG_PATH instead.
    """
    # Downloads/caches the binary and returns the path AS SEEN FROM THIS
    # container (/app/data/...). The sibling only mounts the project
    # directory, so the same file has to be named by its path under that
    # mount - which is where docker-compose.yml's ./backend/data:/app/data
    # bind puts it on the host.
    ensure_docker_compose_cli()
    compose_bin = os.path.join(HOST_PROJECT_DIR, "backend", "data", ".docker-cli", "docker-compose")

    steps = [f'"{compose_bin}" -f "{compose_path}" up -d frontend']
    if stop_caddy:
        # Stopped BEFORE the frontend moves back, so port 80 is free by the
        # time the frontend wants it again.
        steps.insert(0, f'"{compose_bin}" -f "{compose_path}" --profile tls rm -sf caddy')
    else:
        steps.append(f'"{compose_bin}" -f "{compose_path}" --profile tls up -d caddy')

    spawn_sibling_container(
        ["/bin/sh", "-c", "set -x; " + "; ".join(steps)],
        log_path=LOG_PATH,
    )
    logger.info("panel_tls: کانتینر کمکی برای اعمال تغییرات شروع شد")


def read_log() -> str:
    """What the sibling container has printed so far - the only way to find
    out how it went, since it outlives the request that started it."""
    for path in (_LOG_IN_CONTAINER, LOG_PATH):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[-8000:]
        except OSError:
            continue
    return ""


def enable(domain: str, email: str = "", *, skip_dns_check: bool = False) -> str:
    """Points this panel at `domain` over https. Returns a log to show."""
    verify_host_path()
    domain = normalise_domain(domain)
    compose_path = os.path.join(HOST_PROJECT_DIR, "docker-compose.yml")
    env_path = _root_env_path()
    log_lines: list[str] = []

    def log(line: str) -> None:
        log_lines.append(line)
        logger.info(line)

    if not os.path.isfile(compose_path):
        raise DeployError(f"فایل docker-compose.yml در مسیر {compose_path} پیدا نشد.", "\n".join(log_lines))

    # Checked before anything is written, and NOT skippable by `force` -
    # forcing past a DNS doubt is a judgement call, but forcing Caddy onto a
    # port another container already holds simply cannot work. Reported
    # 2026-09-13 on the vendor's own server, which serves license.netcip.ir
    # on 443: the profile got written, Caddy could not bind, and from then on
    # every `docker compose up` aborted on it - so `bash update.sh` could no
    # longer bring the panel up at all. One failed switch took out the whole
    # stack, which is a far worse outcome than refusing.
    busy = ports_in_use()
    if busy:
        raise DeployError(
            "پورت " + " و ".join(busy) + " روی این سرور در اختیار سرویس دیگری است. "
            "Caddy برای گرفتن گواهی به پورت‌های ۸۰ و ۴۴۳ نیاز دارد. "
            "یا آن سرویس را جابه‌جا کنید، یا پنل را روی سروری بالا بیاورید که این پورت‌ها آزادند.",
            "\n".join(log_lines),
        )

    if not skip_dns_check:
        dns = check_dns(domain)
        log(dns["reason"])
        # Each on its own line, so the bidi algorithm has no surrounding
        # Persian text to reorder them against - see check_dns.
        if dns["resolved"]:
            log("دامنه به: " + " ".join(dns["resolved"]))
        # "Could not tell" is not "no". A panel whose own outbound traffic
        # is restricted cannot reach itself over its own hostname, and
        # refusing on that would block a perfectly good setup.
        if not dns["ok"] and not dns["unknown"]:
            # Refused rather than attempted: five failed issuances in a week
            # locks this hostname out of Let's Encrypt entirely, and the
            # operator would be none the wiser until the sixth.
            raise DeployError(dns["reason"], "\n".join(log_lines))

    previous = current_state()
    _write_env_var(env_path, "PANEL_DOMAIN", domain)
    if email:
        _write_env_var(env_path, "PANEL_TLS_EMAIL", email.strip())
    # Caddy needs port 80 for the ACME challenge, today and at every
    # renewal. The frontend keeps a published port so the panel stays
    # reachable by IP if anything about DNS or the certificate goes wrong.
    if int(previous["fallback_http_port"] or 80) == 80:
        _write_env_var(env_path, "PANEL_WEB_PORT", str(FALLBACK_HTTP_PORT))
        log(f"پنل روی پورت {FALLBACK_HTTP_PORT} هم در دسترس می‌ماند (برای مواقع اضطراری با آی‌پی).")
    _set_profile(env_path, "tls", True)
    log(f"دامنه {domain} در .env ثبت شد.")

    def rollback() -> None:
        try:
            _write_env_var(env_path, "PANEL_DOMAIN", previous["domain"])
            _write_env_var(env_path, "PANEL_WEB_PORT", str(previous["fallback_http_port"] or 80))
            _set_profile(env_path, "tls", previous["enabled"])
            log("(تنظیمات به حالت قبل بازگردانده شد.)")
        except OSError:
            pass

    try:
        _apply(compose_path)
    except DeployError:
        rollback()
        raise

    log("در حال جابه‌جا کردن پنل از پورت ۸۰ و بالا آوردن Caddy ...")
    log(f"یک دقیقه صبر کنید و بعد https://{domain} را باز کنید.")
    log(f"اگر مشکلی پیش آمد، پنل روی http://<آی‌پی سرور>:{FALLBACK_HTTP_PORT} باقی می‌ماند.")
    return "\n".join(log_lines)


def disable() -> str:
    """Back to plain HTTP on port 80. The certificate stays in the volume,
    so turning it on again does not re-issue and does not spend one of the
    week's five allowed issuances."""
    verify_host_path()
    compose_path = os.path.join(HOST_PROJECT_DIR, "docker-compose.yml")
    env_path = _root_env_path()
    log_lines: list[str] = []

    def log(line: str) -> None:
        log_lines.append(line)
        logger.info(line)

    _set_profile(env_path, "tls", False)
    _write_env_var(env_path, "PANEL_WEB_PORT", "80")
    _apply(compose_path, stop_caddy=True)
    log("در حال متوقف کردن Caddy و بازگرداندن پنل به پورت ۸۰ ...")
    log("یک دقیقه صبر کنید و بعد پنل را با آی‌پی سرور باز کنید.")
    log("گواهی پاک نشد - اگر دوباره فعالش کنید، صدور مجدد لازم نیست.")
    return "\n".join(log_lines)
