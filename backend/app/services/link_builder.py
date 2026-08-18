"""Builds client-facing config text / share links for a connection."""
from __future__ import annotations
from urllib.parse import quote

from .. import models


def build_wireguard_config(connection: models.Connection, node: models.Node, server_public_key: str) -> str:
    return f"""[Interface]
PrivateKey = {connection.wg_private_key}
Address = {connection.wg_client_address}
DNS = {node.mt_client_dns or '1.1.1.1'}

[Peer]
PublicKey = {server_public_key}
Endpoint = {node.mt_endpoint_host}:{node.mt_endpoint_port}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""


def client_endpoint(node: models.Node) -> tuple[str, int]:
    """Where the CUSTOMER's config should point.

    External Proxy wins over everything: it is the only value a human typed
    on purpose, and unlike xr_public_host it is never overwritten by the
    3X-UI sync (see models.Node's comment on those columns).
    """
    host = (node.xr_external_host or "").strip() or node.xr_public_host or _fallback_host(node)
    port = node.xr_external_port or node.xr_public_port or 443
    return host, port


def _apply_template(template: str, connection: models.Connection, node: models.Node) -> str:
    """Rebuilds a known-good client URI for THIS connection.

    Everything about the template is preserved - scheme, every query
    parameter, transport settings the panel has no column for - and only the
    identity is swapped: the UUID, the remark, and the host/port when an
    External Proxy is configured. That is what makes this work for inbounds
    the field-by-field builder below cannot express.

    vmess:// is deliberately NOT handled here: its payload is a
    base64-encoded JSON object, not a URI, so the same string surgery would
    silently produce a corrupt link. It falls through to the plain builder
    instead of being mangled.
    """
    template = template.strip()
    if not template or "://" not in template or template.lower().startswith("vmess://"):
        return ""

    scheme, rest = template.split("://", 1)

    # Strip the remark first - it is free text and may itself contain @, ?
    # or #, which would otherwise confuse every split below.
    if "#" in rest:
        rest = rest.split("#", 1)[0]

    query = ""
    if "?" in rest:
        rest, query = rest.split("?", 1)

    # userinfo@host:port - rsplit because a UUID never contains @, but the
    # userinfo of some schemes can.
    if "@" not in rest:
        return ""
    _old_id, hostport = rest.rsplit("@", 1)

    t_host, _, t_port = hostport.partition(":")
    host = (node.xr_external_host or "").strip() or t_host
    port = node.xr_external_port or (int(t_port) if t_port.isdigit() else 443)

    remark = quote(connection.xr_email or "")
    out = f"{scheme}://{connection.xr_uuid}@{host}:{port}"
    if query:
        out += f"?{query}"
    return f"{out}#{remark}"


def build_vless_link(connection: models.Connection, node: models.Node) -> str:
    # A template, when present, is the source of truth - see _apply_template.
    if node.xr_link_template:
        from_template = _apply_template(node.xr_link_template, connection, node)
        if from_template:
            return from_template

    remark = quote(f"{connection.xr_email}")
    params = (
        f"type={node.xr_network or 'tcp'}"
        f"&encryption=none"
        f"&security={node.xr_security or 'none'}"
    )
    if node.xr_sni:
        params += f"&sni={node.xr_sni}"
    if connection.xr_flow:
        params += f"&flow={connection.xr_flow}"
    host, port = client_endpoint(node)
    return f"vless://{connection.xr_uuid}@{host}:{port}?{params}#{remark}"


def _fallback_host(node: models.Node) -> str:
    """Best-effort host if xr_public_host was never filled in/synced: for
    SSH-managed nodes fall back to the SSH host, for 3X-UI nodes fall back
    to the panel's own base_url hostname (same domain, just a different
    port than the actual inbound in most setups)."""
    if getattr(node, "xr_panel_mode", "ssh") == "3xui":
        from urllib.parse import urlparse
        return urlparse(node.xr_panel_base_url or "").hostname or ""
    return node.xr_ssh_host or ""


def render_ovpn_template(template: str, username: str, password: str) -> str:
    """Turns the admin's ready-made .ovpn template (Package.ovpn_template)
    into a customer-specific file by injecting ONLY their credentials as an
    inline <auth-user-pass> block (supported by OpenVPN 2.3+ and every
    common mobile client). The template itself is used verbatim - its own
    remote/port/cert lines are the admin's business, not the panel's (per
    the panel owner, 2026-08-09). Any existing `auth-user-pass` directive
    or previously-inlined block is stripped first so the injected
    credentials are the only ones in play."""
    out_lines: list[str] = []
    in_block = False
    for line in (template or "").splitlines():
        stripped = line.strip()
        if stripped == "<auth-user-pass>":
            in_block = True
            continue
        if stripped == "</auth-user-pass>":
            in_block = False
            continue
        if in_block:
            continue
        if stripped == "auth-user-pass" or stripped.startswith("auth-user-pass "):
            continue
        out_lines.append(line)
    body = "\n".join(out_lines).rstrip()
    return f"{body}\n\n<auth-user-pass>\n{username or ''}\n{password or ''}\n</auth-user-pass>\n"


def build_openvpn_config(connection: models.Connection, node: models.Node) -> str:
    """The panel only manages the username/password for OpenVPN (the actual
    .ovpn file needs the router's CA certificate embedded, which the panel
    does not have access to). This just returns the connection credentials
    as plain info - pair it with your own ready-made .ovpn template (built
    once from the router's exported CA) and send the finished file to the
    customer yourself (e.g. via your bot)."""
    lines = [
        f"آدرس سرور: {node.mt_endpoint_host}",
        f"پورت: {node.mt_ovpn_port or 1194}",
        f"نام کاربری: {connection.ppp_username}",
        f"رمز عبور: {connection.ppp_password}",
        "نوع VPN: OpenVPN",
        "(سرور OpenVPN و سرتیفیکیت آن مستقیما روی خود میکروتیک تنظیم شده؛ پنل فقط یوزر/پسورد را می‌سازد. "
        "فایل کانفیگ نهایی .ovpn را با همین یوزر/پسورد خودتان (مثلا از طریق ربات) برای مشتری بفرستید.)",
    ]
    return "\n".join(lines)


def build_l2tp_info(connection: models.Connection, node: models.Node) -> str:
    lines = [
        f"آدرس سرور: {node.mt_endpoint_host}",
        f"نام کاربری: {connection.ppp_username}",
        f"رمز عبور: {connection.ppp_password}",
        "نوع VPN در تنظیمات سیستم‌عامل: L2TP/IPsec",
    ]
    if node.mt_l2tp_use_ipsec and node.mt_l2tp_ipsec_secret:
        lines.append(f"کلید IPsec (Pre-shared key): {node.mt_l2tp_ipsec_secret}")
    else:
        lines.append("IPsec غیرفعال است (اتصال بدون رمزنگاری IPsec انجام می‌شود)")
    lines.append("(سرور L2TP/IPsec مستقیما روی خود میکروتیک تنظیم شده؛ پنل فقط یوزر/پسورد را می‌سازد)")
    return "\n".join(lines)


def build_ikev2_info(connection: models.Connection, node: models.Node) -> str:
    lines = [
        f"آدرس سرور: {node.mt_endpoint_host}",
        f"نام کاربری: {connection.ppp_username}",
        f"رمز عبور: {connection.ppp_password}",
        "نوع VPN در تنظیمات سیستم‌عامل: IKEv2/IPsec",
    ]
    if node.mt_ikev2_psk:
        lines.append(f"کلید IPsec (Pre-shared key): {node.mt_ikev2_psk}")
    lines.append("(سرور IKEv2/IPsec مستقیما روی خود میکروتیک تنظیم شده؛ پنل فقط یوزر/پسورد را می‌سازد)")
    return "\n".join(lines)


def build_sstp_info(connection: models.Connection, node: models.Node) -> str:
    """Like build_openvpn_config: SSTP tunnels PPP inside a TLS connection
    (needs a server certificate, not a PSK), which the panel does not have
    access to - just returns the plain credentials/port as info."""
    lines = [
        f"آدرس سرور: {node.mt_endpoint_host}",
        f"پورت: {node.mt_sstp_port or 443}",
        f"نام کاربری: {connection.ppp_username}",
        f"رمز عبور: {connection.ppp_password}",
        "نوع VPN: SSTP",
        "(سرور SSTP و سرتیفیکیت آن مستقیما روی خود میکروتیک تنظیم شده؛ پنل فقط یوزر/پسورد را می‌سازد.)",
    ]
    return "\n".join(lines)
