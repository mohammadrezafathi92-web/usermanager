"""Sends a customer's connection credentials/config over Telegram, formatted
nicely per protocol instead of one long text dump:

  - WireGuard: an actual .conf file attachment + a QR code image (both
    scannable/importable directly by the official WireGuard app).
  - OpenVPN / L2TP: a short, clean "server / port / username / password"
    block (L2TP also gets its IPsec pre-shared key when the node has one
    configured) - none of the internal admin-facing notes that
    services/link_builder.py's config_text includes for the web panel's own
    "دریافت کانفیگ" admin view (those are implementation notes for whoever
    is wiring up delivery, not something an actual customer should see -
    now that the bot IS that delivery mechanism, that gap is exactly what
    this module closes).
  - Xray/VLESS: the vless:// link in a copyable code block + a QR code
    (most VLESS-capable apps support scanning it directly).

Used everywhere the bot hands connection info to a customer: the "حساب من"
view, right after an instant balance purchase, and right after an admin
approves a card-receipt purchase.
"""
from __future__ import annotations

import io
import logging

import qrcode
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile

logger = logging.getLogger("telegram_bot")


def _log_send_failure(exc: Exception, what: str) -> None:
    """Sends here are deliberately best-effort (a blocked bot or a bad
    connection payload shouldn't blow up a purchase/renewal flow that
    already succeeded on the panel side), but silently swallowing every
    failure made a systematic problem (e.g. a broken QR payload for every
    customer) invisible. TelegramForbiddenError just means the customer
    blocked the bot - expected often enough to log at debug; anything else
    is worth a warning."""
    if isinstance(exc, TelegramForbiddenError):
        logger.debug("%s: customer blocked the bot", what)
    else:
        logger.warning("%s failed: %s", what, exc, exc_info=True)

TYPE_LABELS = {
    "wireguard": "🔒 WireGuard",
    "openvpn": "🛡 OpenVPN",
    "l2tp": "🌐 L2TP/IPsec",
    "ikev2": "🛰 IKEv2/IPsec",
    "sstp": "🔐 SSTP",
    "xray": "⚡ V2Ray/Xray",
}

DEFAULT_PORTS = {"l2tp": 1701, "ikev2": 500, "sstp": 443}


def _qr_bytes(data: str) -> bytes:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def send_connection(bot: Bot, chat_id: int, conn: dict) -> None:
    """conn is a BotConnectionInfo-shaped dict (see schemas.py) - as
    returned by panel_bridge's create_user/renew/get_user/... calls."""
    ctype = conn.get("type")
    label = TYPE_LABELS.get(ctype, ctype)
    status = "✅ فعال" if conn.get("enabled") else "⛔️ غیرفعال"

    if ctype == "wireguard":
        text = conn.get("config_text") or ""
        if not text:
            return
        try:
            await bot.send_document(
                chat_id,
                BufferedInputFile(text.encode("utf-8"), filename=f"wireguard-{conn.get('id')}.conf"),
                caption=f"{label} — {status}",
            )
            await bot.send_photo(
                chat_id,
                BufferedInputFile(_qr_bytes(text), filename="wireguard-qr.png"),
                caption="📷 یا همین QR کد را داخل اپلیکیشن WireGuard اسکن کنید",
            )
        except Exception as exc:
            _log_send_failure(exc, f"send wireguard connection {conn.get('id')} to {chat_id}")
        return

    if ctype in ("openvpn", "l2tp", "ikev2", "sstp"):
        port = conn.get("port") or DEFAULT_PORTS.get(ctype)
        lines = [f"{label} — {status}", ""]
        lines.append(f"سرور: <code>{conn.get('server') or '-'}</code>")
        if port:
            lines.append(f"پورت: <code>{port}</code>")
        lines.append(f"نام کاربری: <code>{conn.get('username') or '-'}</code>")
        lines.append(f"رمز عبور: <code>{conn.get('password') or '-'}</code>")
        if ctype in ("l2tp", "ikev2"):
            if conn.get("psk"):
                lines.append(f"کلید IPsec (PSK): <code>{conn['psk']}</code>")
            else:
                lines.append("IPsec: غیرفعال" if ctype == "l2tp" else "احراز هویت با گواهی/تنظیمات سرور (بدون PSK)")
        try:
            await bot.send_message(chat_id, "\n".join(lines))
        except Exception as exc:
            _log_send_failure(exc, f"send {ctype} connection {conn.get('id')} to {chat_id}")
        return

    # xray / vless
    link = conn.get("link") or ""
    if not link:
        return
    try:
        await bot.send_message(chat_id, f"{label} — {status}\n\n<code>{link}</code>")
        await bot.send_photo(
            chat_id,
            BufferedInputFile(_qr_bytes(link), filename="vless-qr.png"),
            caption="📷 یا همین QR کد را داخل اپلیکیشن اسکن کنید",
        )
    except Exception as exc:
        _log_send_failure(exc, f"send xray connection {conn.get('id')} to {chat_id}")


async def send_connections(bot: Bot, chat_id: int, connections: list[dict]) -> None:
    for c in connections:
        await send_connection(bot, chat_id, c)
