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

The DNS check before any of that is the important part. Let's Encrypt's
failure for a hostname that does not point here is a rate-limited, opaque
"challenge failed", and five of those in a week locks the hostname out
entirely. Asking DNS ourselves first turns the overwhelmingly common
mistake - record not made, or still propagating - into a sentence the
operator can act on, and costs one lookup.
"""
from __future__ import annotations

import logging
import os
import re
import socket

import requests

from .local_deploy import (
    DeployError,
    HOST_PROJECT_DIR,
    _read_env_var,
    _root_env_path,
    _write_env_var,
    ensure_docker_compose_cli,
    _run,
    verify_host_path,
)

logger = logging.getLogger("panel_tls")

# Where the frontend goes when Caddy takes 80. Deliberately still published:
# see the module docstring on not locking the operator out.
FALLBACK_HTTP_PORT = 8080

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


def server_public_ip() -> str | None:
    """This server's address as the rest of the internet sees it.

    Read from outside rather than from a local interface on purpose: the
    panel runs in a container behind Docker's NAT, so every address it can
    see locally is a private one, and comparing a DNS record against
    172.18.0.3 would fail for every correctly-configured install.
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


def resolve(domain: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(domain, None, socket.AF_INET)})
    except OSError:
        return []


def check_dns(domain: str) -> dict:
    """What DNS says about this hostname, versus where we actually are.

    Returns a dict rather than raising so the settings page can show the
    situation while the operator is still editing - "points at 1.2.3.4, this
    server is 5.6.7.8" is a far more useful screen than a refusal.
    """
    resolved = resolve(domain)
    public_ip = server_public_ip()
    ok = bool(resolved) and bool(public_ip) and public_ip in resolved

    # The addresses are deliberately NOT interpolated into this sentence.
    # An IPv4 address inside right-to-left prose is reordered by the bidi
    # algorithm, so "points at A but this server is B" can render with A and
    # B in the opposite visual order - and the whole point of this message is
    # to say which is which. The panel shows them on their own left-to-right
    # lines instead (components/PanelTlsCard.jsx); the text only has to say
    # what to DO about it.
    if not resolved:
        reason = "این دامنه به هیچ آدرسی اشاره نمی‌کند. یک رکورد A بسازید که به آی‌پی این سرور اشاره کند و چند دقیقه صبر کنید."
    elif not public_ip:
        reason = "آی‌پی عمومی این سرور خوانده نشد - دسترسی خروجی سرور را بررسی کنید."
    elif not ok:
        reason = "این دامنه به سرور دیگری اشاره می‌کند. یا رکورد A را به این سرور تغییر دهید، یا برای این پنل یک زیردامنه‌ی دیگر بسازید."
    else:
        reason = "درست به این سرور اشاره می‌کند - آماده‌ی گرفتن گواهی است."
    return {"ok": ok, "domain": domain, "resolved": resolved, "public_ip": public_ip, "reason": reason}


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

    if not skip_dns_check:
        dns = check_dns(domain)
        log(dns["reason"])
        # Each on its own line, so the bidi algorithm has no surrounding
        # Persian text to reorder them against - see check_dns.
        if dns["resolved"]:
            log("دامنه به: " + " ".join(dns["resolved"]))
        if dns["public_ip"]:
            log("این سرور: " + dns["public_ip"])
        if not dns["ok"]:
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
        compose_bin = ensure_docker_compose_cli()
    except DeployError:
        rollback()
        raise

    log("در حال جابه‌جا کردن پنل از پورت ۸۰ ...")
    code, out, err = _run([compose_bin, "-f", compose_path, "up", "-d", "frontend"], timeout=180)
    if code != 0:
        rollback()
        raise DeployError(f"بازسازی کانتینر frontend ناموفق بود:\n{err or out}", "\n".join(log_lines))

    log("در حال بالا آوردن Caddy و گرفتن گواهی ...")
    code, out, err = _run(
        [compose_bin, "-f", compose_path, "--profile", "tls", "up", "-d", "caddy"], timeout=300
    )
    if code != 0:
        rollback()
        _run([compose_bin, "-f", compose_path, "up", "-d", "frontend"], timeout=180)
        raise DeployError(f"بالا آوردن Caddy ناموفق بود:\n{err or out}", "\n".join(log_lines))

    log(f"انجام شد. پنل از این پس روی https://{domain} در دسترس است.")
    log("صدور گواهی چند ثانیه طول می‌کشد؛ اگر بار اول خطا دید، یک دقیقه بعد دوباره باز کنید.")
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

    compose_bin = ensure_docker_compose_cli()
    _run([compose_bin, "-f", compose_path, "--profile", "tls", "stop", "caddy"], timeout=120)
    _run([compose_bin, "-f", compose_path, "--profile", "tls", "rm", "-f", "caddy"], timeout=120)
    log("Caddy متوقف شد.")

    _set_profile(env_path, "tls", False)
    _write_env_var(env_path, "PANEL_WEB_PORT", "80")
    code, out, err = _run([compose_bin, "-f", compose_path, "up", "-d", "frontend"], timeout=180)
    if code != 0:
        raise DeployError(f"بازگرداندن پنل به پورت ۸۰ ناموفق بود:\n{err or out}", "\n".join(log_lines))
    log("پنل دوباره روی پورت ۸۰ (بدون https) در دسترس است.")
    log("گواهی پاک نشد - اگر دوباره فعالش کنید، صدور مجدد لازم نیست.")
    return "\n".join(log_lines)
