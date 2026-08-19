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

# Public resolvers, routed through the tunnel as /32s alongside Telegram's
# own ranges.
#
# Discovered the hard way on a live install: the tunnel was up, the route to
# 149.154.167.220 was correct, and the bot still could not connect - because
# the ISP's DNS answered api.telegram.org with a forged IPv6 address in an
# Iranian prefix. Traffic never reached the routed ranges at all; the name
# was poisoned before routing could matter.
#
# Routing the resolvers themselves through the tunnel means the lookup is
# answered by a server the local network cannot tamper with, so the address
# that comes back is genuine and then falls inside the ranges below.
TUNNEL_DNS = ("1.1.1.1", "8.8.8.8")

PROBE_URL = "https://api.telegram.org/"

# The names that must resolve truthfully for the bot to work, pinned into
# /etc/hosts from an answer fetched through the tunnel. See sync_hosts.
TUNNEL_HOSTS = ("api.telegram.org", "core.telegram.org")

HOSTS_PATH = "/etc/hosts"
HOSTS_BEGIN = "# >>> netcip telegram tunnel >>>"
HOSTS_END = "# <<< netcip telegram tunnel <<<"


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


# ------------------------------------------------------------------- DNS
#
# The panel asks a public resolver itself, through the tunnel, and pins the
# answer into /etc/hosts.
#
# Why not simply point the container at 1.1.1.1 with `dns:` in
# docker-compose - which is one line instead of all of this? Because that was
# tried on this very install and took the whole panel down: with the tunnel
# down, 1.1.1.1 is unreachable, so NOTHING resolves - not the nodes, not the
# payment gateway, not the update check. It made the panel's entire DNS
# depend on the one link most likely to break, to fix two names.
#
# Pinning into /etc/hosts inverts that. The system resolver stays exactly as
# it is and keeps serving everything else; only these two names bypass it,
# and if the lookup fails the previous pinned answer simply stays in place.
# The failure mode is "the pin goes stale", not "the panel goes blind".
#
# It also has to be done at all: routing 1.1.1.1 into the tunnel achieved
# nothing, because the container resolves through Docker's embedded resolver
# at 127.0.0.11, which forwards to the host - so the query never went near
# the routed address. The local ISP answered api.telegram.org with
# 10.10.34.36, a filtering sinkhole, and the request then never reached the
# routed ranges at all. The tunnel was fine; the name was poisoned first.


def _dns_query(name: str, server: str, timeout: int = 5) -> list[str]:
    """A-records for `name` from `server`, over UDP.

    Hand-built rather than pulling in a DNS library: this is one query type
    with no recursion, no caching and no zone handling, and adding a
    dependency to a live panel costs more than forty lines that can be read
    in full.
    """
    import os
    import socket

    tid = os.urandom(2)
    # Standard query, recursion desired, one question.
    packet = tid + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for label in name.split("."):
        packet += bytes([len(label)]) + label.encode()
    packet += b"\x00\x00\x01\x00\x01"          # QTYPE=A, QCLASS=IN

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(2048)
    finally:
        sock.close()

    if data[:2] != tid:
        raise TunnelError("پاسخ DNS با درخواست هم‌خوان نبود")

    def skip_name(buf: bytes, i: int) -> int:
        while True:
            length = buf[i]
            if length == 0:
                return i + 1
            if length & 0xC0 == 0xC0:          # compression pointer, 2 bytes
                return i + 2
            i += 1 + length

    answers = int.from_bytes(data[6:8], "big")
    i = skip_name(data, 12) + 4                # past the echoed question
    found: list[str] = []
    for _ in range(answers):
        i = skip_name(data, i)
        rtype = int.from_bytes(data[i:i + 2], "big")
        rdlen = int.from_bytes(data[i + 8:i + 10], "big")
        rdata = data[i + 10:i + 10 + rdlen]
        if rtype == 1 and rdlen == 4:          # A record
            found.append(".".join(str(b) for b in rdata))
        i += 10 + rdlen
    return found


def _is_sinkhole(addr: str) -> bool:
    """Filtering answers point at private space - 10.10.34.36 here.

    Checked because a poisoned answer is not an error: the query succeeds and
    returns an address, and everything downstream behaves as if the name
    resolved. Pinning it would make things worse than not pinning at all.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_unspecified


def resolve_via_tunnel(name: str) -> str:
    errors: list[str] = []
    for server in TUNNEL_DNS:
        try:
            for addr in _dns_query(name, server):
                if not _is_sinkhole(addr):
                    return addr
            errors.append(f"{server}: فقط آدرس جعلی برگرداند")
        except Exception as exc:  # noqa: BLE001 - try the next resolver
            errors.append(f"{server}: {exc}")
    raise TunnelError(f"ترجمه‌ی «{name}» از داخل تونل ناموفق بود", " | ".join(errors))


def sync_hosts() -> list[str]:
    """Refreshes the pinned block in /etc/hosts. Never raises.

    A name that cannot be resolved keeps whatever was pinned for it before -
    a stale address still works far more often than no address, and Telegram's
    do not move often.
    """
    resolved: dict[str, str] = {}
    log: list[str] = []
    for name in TUNNEL_HOSTS:
        try:
            resolved[name] = resolve_via_tunnel(name)
        except TunnelError as exc:
            logger.warning("ترجمه‌ی %s ناموفق بود: %s", name, exc.detail)

    if not resolved:
        return ["هیچ نامی از داخل تونل ترجمه نشد - مقدار قبلی /etc/hosts دست‌نخورده ماند"]

    try:
        with open(HOSTS_PATH, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return [f"خواندن {HOSTS_PATH} ناموفق بود: {exc}"]

    kept: list[str] = []
    inside = False
    previous: dict[str, str] = {}
    for line in lines:
        if line.strip() == HOSTS_BEGIN:
            inside = True
            continue
        if line.strip() == HOSTS_END:
            inside = False
            continue
        if inside:
            parts = line.split()
            if len(parts) >= 2:
                previous[parts[1]] = parts[0]
            continue
        kept.append(line)

    # Anything that failed this round keeps its previous pin.
    for name, addr in previous.items():
        resolved.setdefault(name, addr)

    block = [HOSTS_BEGIN] + [f"{addr}\t{name}" for name, addr in sorted(resolved.items())] + [HOSTS_END]
    try:
        with open(HOSTS_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept + block) + "\n")
    except OSError as exc:
        return [f"نوشتن {HOSTS_PATH} ناموفق بود: {exc}"]

    log.append("نام‌های تلگرام از داخل تونل ترجمه و ثابت شدند: "
               + "، ".join(f"{n} → {a}" for n, a in sorted(resolved.items())))
    return log


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
    # The resolvers go through the tunnel too - see TUNNEL_DNS. Added here
    # rather than stored in allowed_ips so they cannot be edited away by
    # accident: without them the tunnel routes perfectly and still resolves
    # to a forged address, which looks exactly like a broken tunnel.
    cidrs = cidrs + [f"{ip}/32" for ip in TUNNEL_DNS if f"{ip}/32" not in cidrs]
    log: list[str] = []
    down(name)

    _run("ip", "link", "add", name, "type", "wireguard")
    log.append(f"اینترفیس {name} ساخته شد")

    # The private key is handed over on stdin, not as an argument: `wg set
    # ... private-key /proc/self/fd/0` keeps it out of the process table.
    # AllowedIPs is 0.0.0.0/0 - deliberately NOT the Telegram list.
    #
    # This one line cost a full day. AllowedIPs does two different jobs at
    # once: it decides what goes INTO the tunnel, and it is also the inbound
    # filter - WireGuard drops any packet from the peer whose SOURCE address
    # is outside it. Listing only Telegram's ranges therefore also meant
    # "accept replies only from Telegram's ranges", and the router does not
    # return traffic that way: replies come back from addresses outside that
    # list, so the kernel discarded every one of them. The symptom was a
    # tunnel with a perfect fresh handshake carrying a few hundred bytes and
    # nothing else - which looks like a routing problem and is not one.
    #
    # A real customer's config has 0.0.0.0/0 here, which is why customers on
    # the very same interface always worked.
    #
    # Restricting what the panel sends is still done - but by the ROUTING
    # TABLE below, which is the thing that actually decides destinations.
    # The panel's default route stays untouched; only the ranges in `cidrs`
    # are pointed at this interface. Filter and routing are separate
    # concerns and mixing them is what broke this.
    proc = subprocess.run(
        ["wg", "set", name, "private-key", "/dev/stdin",
         "peer", tunnel.peer_public_key,
         "endpoint", tunnel.peer_endpoint,
         "persistent-keepalive", str(tunnel.persistent_keepalive or 25),
         "allowed-ips", "0.0.0.0/0"],
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

    # Only meaningful once the routes exist - the resolvers are reached
    # through the tunnel, so this has to be the last step, not the first.
    log += sync_hosts()
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


def diagnose(tunnel) -> tuple[bool, list[str]]:
    """Every check that matters, in one pass, in the order that isolates the
    fault.

    Written after diagnosing this tunnel by hand one command at a time. Each
    command answered a fraction of the question, and the answers only meant
    something together - so the whole sequence belongs behind one button
    rather than in a person's terminal history.

    The order is the point: each step only runs if the one before it passed,
    so the FIRST failing line is the fault. Later lines would fail too and
    say nothing extra.
    """
    import socket

    name = tunnel.interface_name if tunnel else "wg-tg"
    name = name or "wg-tg"
    log: list[str] = []

    st = status(name)
    if not st["exists"]:
        return False, ["اینترفیس تونل بالا نیست. دکمه‌ی «روشن کردن» را بزنید."]
    log.append(f"اینترفیس {name} بالاست")

    if st["handshake_age_s"] is None:
        return False, log + [
            "هیچ دست‌دادنی با نود انجام نشده - یعنی بسته‌های ما اصلا به نود نمی‌رسند. "
            "آدرس و پورت نود یا باز بودن پورت UDP را بررسی کنید."
        ]
    if st["handshake_age_s"] > 180:
        log.append(f"هشدار: آخرین دست‌دادن {st['handshake_age_s']} ثانیه پیش بوده (کهنه)")
    else:
        log.append(f"دست‌دادن سالم ({st['handshake_age_s']} ثانیه پیش)")

    # The byte counters are deliberately NOT judged here. They are cumulative
    # since the interface came up, so a tunnel rebuilt seconds ago always
    # looks idle - and the raw TCP test below answers the same question
    # ("does data actually cross?") without a false alarm attached.

    # Raw TCP to a Telegram address, before DNS enters the picture. Separating
    # the two matters: a poisoned DNS answer and a dead tunnel produce the
    # same error message from a normal HTTPS request.
    try:
        socket.create_connection(("149.154.167.220", 443), timeout=8).close()
        log.append("اتصال مستقیم TCP به آی‌پی تلگرام برقرار شد - تونل داده جابه‌جا می‌کند")
    except Exception as exc:  # noqa: BLE001
        return False, log + [
            f"اتصال TCP به 149.154.167.220:443 برقرار نشد ({exc}). "
            "تونل بالاست ولی داده رد نمی‌شود - روی نود بررسی کنید که این آدرس NAT می‌شود."
        ]

    # Re-pinned on every run rather than trusted: the pin is the one piece of
    # state that can silently go stale, and refreshing it costs one UDP query.
    log += sync_hosts()

    try:
        resolved = socket.gethostbyname("api.telegram.org")
    except Exception as exc:  # noqa: BLE001
        return False, log + [f"ترجمه‌ی نام api.telegram.org ناموفق بود ({exc})"]
    if _is_sinkhole(resolved):
        return False, log + [
            f"api.telegram.org به {resolved} ترجمه شد - این آدرس چاهک فیلترینگ است، نه تلگرام. "
            "یعنی نام قبل از مسیریابی مسموم می‌شود و ثابت‌کردن آن در /etc/hosts انجام نشده."
        ]
    log.append(f"api.telegram.org به {resolved} ترجمه شد")

    ok, detail = test_telegram()
    log.append(("درخواست واقعی به تلگرام موفق بود: " if ok else "درخواست واقعی به تلگرام ناموفق بود: ") + detail)
    return ok, log


def ensure_up(tunnel) -> None:
    """Called at startup. The container is recreated on every deploy and
    takes the interface with it, so this rebuilds it from the database
    instead of expecting it to have survived."""
    if not tunnel or not tunnel.enabled:
        return
    name = tunnel.interface_name or "wg-tg"
    if interface_exists(name):
        # Docker rewrites /etc/hosts on every container start, so the pinned
        # names are gone even when the interface somehow survived.
        sync_hosts()
        return
    try:
        up(tunnel)
        logger.info("تونل تلگرام %s بعد از راه‌اندازی مجدد دوباره بالا آمد", name)
    except TunnelError as exc:
        # Never fatal: a failed tunnel must not stop the panel from booting.
        logger.error("بالا آوردن تونل تلگرام ناموفق بود: %s | %s", exc.message, exc.detail)
