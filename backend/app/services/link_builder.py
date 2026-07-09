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


def build_vless_link(connection: models.Connection, node: models.Node) -> str:
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
    host = node.xr_public_host or _fallback_host(node)
    port = node.xr_public_port or 443
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
