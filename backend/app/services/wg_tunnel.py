"""The WireGuard tunnel this panel uses to reach api.telegram.org.

Driven with `ip` and `wg` directly rather than wg-quick. wg-quick is a bash
script that also wants resolvconf and writes its own state files; here the
interface lives inside a container that is recreated on every deploy, so
"what is configured" must come from the database on each start, not from a
file the container no longer has. Driving the two binaries directly also
means a failure names the exact step that failed instead of a shell exit
code.

Two properties matter more than anything else in this module:

  1. ONLY Telegram's ranges are routed here. The panel's default route is
     never touched. A tunnel that goes down must cost the bot its
     connection and nothing else - not the customers' subscriptions, not
     node management, not the operator's SSH.

  2. Bringing the tunnel up is idempotent and self-healing. The container
     is recreated on every `docker compose up`, taking the interface with
     it, so `ensure_up` is called at startup and simply rebuilds whatever
     is missing.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import subprocess
import datetime as dt

import requests
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)

logger = logging.getLogger("wg_tunnel")

# Telegram publishes the list itself. Fetched rather than hardcoded because
# the ranges do change, and the failure mode of a stale list is a bot that
# stops working for no visible reason months after anyone touched it.
TELEGRAM_CIDR_URL = "https://core.telegram.org/resources/cidr.txt"

# The full IPv4 list as published at TELEGRAM_CIDR_URL, embedded here so a
# tunnel can be built without ever reaching Telegram first.
#
# That "first" is the whole point: fetching the list requires reaching
# core.telegram.org, which is behind exactly the block this tunnel exists to
# get around. Shipping only the two ranges the bot documentation names -
# which is what this was originally - left the other seven unrouted, so the
# tunnel would carry some Telegram endpoints and silently miss others.
# Refreshing from the live URL still works, but only AFTER the tunnel is up,
# and is a correction rather than a prerequisite.
DEFAULT_CIDRS = (
    "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22", "91.108.16.0/22",
    "91.108.20.0/22", "91.108.56.0/22", "149.154.160.0/20",
    "91.105.192.0/23", "185.76.151.0/24",
)

PROBE_URL = "https://api.telegram.org/"


class TunnelError(Exception):
    """Carries a message already written for the admin, plus the raw output
    of whichever command failed - the two things needed to act on it."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def _run(*args: str, check: bool = True, timeout: int = 15) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise TunnelError(
            f"ابزار «{args[0]}» در این نسخه‌ی پنل نصب نیست. برای تونل وایرگارد باید پنل "
            "بروزرسانی و دوباره ساخته شود.",
        )
    except subprocess.TimeoutExpired:
        raise TunnelError(f"اجرای «{' '.join(args)}» زمان‌بر شد و متوقف گردید")
    out = (proc.stdout + proc.stderr).strip()
    if check and proc.returncode != 0:
        raise TunnelError(f"اجرای «{' '.join(args[:3])}» ناموفق بود", out)
    return out


# ------------------------------------------------------------------ keys
def generate_keypair() -> tuple[str, str]:
    """(private, public), base64, in WireGuard's own format.

    Generated in-process with the cryptography library rather than by
    shelling out to `wg genkey`, so key material never passes through a
    command line - where it would be visible to anything that can read the
    process table for as long as the command runs.
    """
    key = X25519PrivateKey.generate()
    priv = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(priv).decode(), base64.b64encode(pub).decode()


def public_key_of(private_key_b64: str) -> str:
    raw = base64.b64decode(private_key_b64)
    key = X25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()


# ------------------------------------------------------------------ CIDRs
def fetch_telegram_cidrs(timeout: int = 15) -> list[str]:
    """IPv4 ranges only.

    IPv6 is dropped on purpose: routing a v6 range into the tunnel on a host
    with no working v6 makes every Telegram request try v6 first and wait
    for it to fail, turning a working bot into a slow one.
    """
    resp = requests.get(TELEGRAM_CIDR_URL, timeout=timeout)
    resp.raise_for_status()
    out: list[str] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            out.append(str(net))
    if not out:
        raise TunnelError("فهرست دریافتی از تلگرام هیچ رنج IPv4 معتبری نداشت")
    return out


def parse_cidrs(raw: str | None) -> list[str]:
    values = [c.strip() for c in (raw or "").replace("\n", ",").split(",") if c.strip()]
    valid: list[str] = []
    for c in values:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            valid.append(str(net))
    return valid or list(DEFAULT_CIDRS)


# ------------------------------------------------------------- interface
def interface_exists(name: str) -> bool:
    try:
        _run("ip", "link", "show", name, check=True, timeout=5)
        return True
    except TunnelError:
        return False


def down(name: str) -> None:
    """Removes the interface. Its routes go with it - the kernel drops every
    route through an interface that no longer exists, so there is no
    separate cleanup step to forget."""
    if interface_exists(name):
        _run("ip", "link", "del", name, check=False)


def up(tunnel) -> list[str]:
    """(Re)builds the interface from the stored configuration.

    Torn down first rather than patched: a half-configured interface left
    by a previous failure is indistinguishable from a working one until
    traffic silently goes the wrong way, and rebuilding costs milliseconds.
    """
    name = tunnel.interface_name or "wg-tg"
    for field, label in (
        (tunnel.private_key, "کلید خصوصی"),
        (tunnel.address, "آدرس داخل تونل"),
        (tunnel.peer_public_key, "کلید عمومی نود"),
        (tunnel.peer_endpoint, "آدرس نود"),
    ):
        if not (field or "").strip():
            raise TunnelError(f"تونل ناقص است: {label} تنظیم نشده")

    cidrs = parse_cidrs(tunnel.allowed_ips)
    log: list[str] = []
    down(name)

    _run("ip", "link", "add", name, "type", "wireguard")
    log.append(f"اینترفیس {name} ساخته شد")

    # The private key is handed over on stdin, not as an argument: `wg set
    # ... private-key /proc/self/fd/0` keeps it out of the process table.
    proc = subprocess.run(
        ["wg", "set", name, "private-key", "/dev/stdin",
         "peer", tunnel.peer_public_key,
         "endpoint", tunnel.peer_endpoint,
         "persistent-keepalive", str(tunnel.persistent_keepalive or 25),
         "allowed-ips", ",".join(cidrs)],
        input=tunnel.private_key + "\n", capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0:
        down(name)
        raise TunnelError("پیکربندی وایرگارد ناموفق بود", (proc.stdout + proc.stderr).strip())
    log.append(f"پیر تنظیم شد: {tunnel.peer_endpoint}")

    _run("ip", "address", "add", tunnel.address, "dev", name)
    _run("ip", "link", "set", name, "up")
    log.append(f"آدرس {tunnel.address} روی اینترفیس نشست")

    routed = 0
    for cidr in cidrs:
        try:
            _run("ip", "route", "replace", cidr, "dev", name)
            routed += 1
        except TunnelError:
            # One unroutable range must not abort the rest: a partially
            # routed tunnel still carries most of Telegram, while raising
            # here would leave the interface up with no routes at all.
            logger.warning("مسیر %s روی %s اضافه نشد", cidr, name)
    log.append(f"{routed} رنج تلگرام از این تونل مسیریابی می‌شود (مسیر پیش‌فرض سرور دست‌نخورده)")
    return log


def status(name: str) -> dict:
    """What the tunnel is actually doing right now, from `wg show ... dump`.

    The handshake age is the number that matters: an interface can exist,
    have routes, and still never have reached its peer. "Up" is not the
    same as "working", and only the handshake tells them apart.
    """
    if not interface_exists(name):
        return {"exists": False, "handshake_age_s": None, "rx": 0, "tx": 0, "peer": None}

    dump = _run("wg", "show", name, "dump", check=False)
    lines = [l for l in dump.splitlines() if l.strip()]
    # First line is the interface itself; peers follow.
    if len(lines) < 2:
        return {"exists": True, "handshake_age_s": None, "rx": 0, "tx": 0, "peer": None}

    parts = lines[1].split("\t")
    peer_key = parts[0] if parts else None
    try:
        last_handshake = int(parts[4])
        rx, tx = int(parts[5]), int(parts[6])
    except (IndexError, ValueError):
        last_handshake, rx, tx = 0, 0, 0

    age = None
    if last_handshake:
        age = max(0, int(dt.datetime.now(dt.timezone.utc).timestamp()) - last_handshake)
    return {"exists": True, "handshake_age_s": age, "rx": rx, "tx": tx, "peer": peer_key}


def test_telegram(timeout: int = 12) -> tuple[bool, str]:
    """Does a real request to Telegram succeed from inside this container?

    The only check that answers the question the admin actually has. An
    interface that is up with a fresh handshake can still fail to carry
    traffic if the peer does not NAT it, and no amount of local state
    reveals that.
    """
    try:
        resp = requests.get(PROBE_URL, timeout=timeout)
        return True, f"پاسخ گرفت (کد {resp.status_code})"
    except Exception as exc:  # noqa: BLE001 - any failure is the same answer
        return False, f"دسترسی برقرار نشد: {exc}"


def ensure_up(tunnel) -> None:
    """Called at startup. The container is recreated on every deploy and
    takes the interface with it, so this rebuilds it from the database
    instead of expecting it to have survived."""
    if not tunnel or not tunnel.enabled:
        return
    name = tunnel.interface_name or "wg-tg"
    if interface_exists(name):
        return
    try:
        up(tunnel)
        logger.info("تونل تلگرام %s بعد از راه‌اندازی مجدد دوباره بالا آمد", name)
    except TunnelError as exc:
        # Never fatal: a failed tunnel must not stop the panel from booting.
        logger.error("بالا آوردن تونل تلگرام ناموفق بود: %s | %s", exc.message, exc.detail)
