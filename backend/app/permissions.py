"""Central definition of the checkbox-style permissions a non-superadmin
admin can be granted (see models.AdminUser.permissions - a comma-separated
subset of PERMISSION_CHOICES, stored as plain text rather than a real M2M
table since the list is small and rarely changes).

Managing users is intentionally NOT one of these choices - every admin,
super or not, can always manage users (scoped to their own group via
User.owner_admin_id for non-superadmins - see deps.py/routers/users.py).
These choices only control the OTHER panel sections. Managing other admins
is also intentionally not a grantable permission - only is_superadmin can
do that, so a sub-admin can never escalate their own or another admin's
access.
"""

PERMISSION_CHOICES: dict[str, str] = {
    "manage_nodes": "سرورها (نودها)",
    "manage_packages": "پکیج‌ها",
    "manage_tutorials": "آموزش",
    "manage_settings": "تنظیمات پنل (پرداخت، ربات تلگرام، کلیدهای API، بک‌آپ)",
}


def parse_permissions(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip() in PERMISSION_CHOICES}


def format_permissions(perms: set[str] | list[str]) -> str:
    return ",".join(p for p in perms if p in PERMISSION_CHOICES)
