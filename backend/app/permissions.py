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

History: this used to be exactly 4 broad toggles, one per page
(manage_nodes/manage_packages/manage_tutorials/manage_settings), each
gating an entire router with no distinction between viewing and making
destructive changes. Expanded here into per-page view/edit/delete triples
(plus settings split into its actual sub-areas) at the admin's explicit
request ("هر صفحه اصلی + عملیات مهمش جدا" - each main page + its important
actions, separately). PERMISSION_GROUPS below is the grouped-by-page shape
the frontend renders as sectioned checkboxes; PERMISSION_CHOICES is the
flat key->label view used for validation/storage, derived from it.
"""

PERMISSION_GROUPS: dict[str, dict] = {
    "nodes": {
        "label": "سرورها (نودها)",
        "perms": {
            "view_nodes": "مشاهده لیست سرورها",
            "edit_nodes": "افزودن/ویرایش سرور، پیکربندی خودکار (RADIUS/SSTP/L2TP/IKEv2) و ایمپورت",
            "delete_nodes": "حذف سرور",
        },
    },
    "packages": {
        "label": "پکیج‌ها",
        "perms": {
            "view_packages": "مشاهده لیست پکیج‌ها",
            "edit_packages": "افزودن/ویرایش پکیج و فایل‌های آن",
            "delete_packages": "حذف پکیج",
        },
    },
    "tutorials": {
        "label": "آموزش",
        "perms": {
            "view_tutorials": "مشاهده آموزش‌ها",
            "edit_tutorials": "افزودن/ویرایش آموزش، مدیا و نرم‌افزار",
            "delete_tutorials": "حذف آموزش/مدیا/نرم‌افزار",
        },
    },
    "settings": {
        "label": "تنظیمات پنل",
        "perms": {
            "manage_payment_settings": "تنظیمات پرداخت، دعوت/وفاداری، پشتیبانی و پورت پنل",
            "manage_bot_settings": "تنظیمات ربات تلگرام (محلی و دیپلوی روی سرور دوم)",
            "manage_backup": "بک‌آپ و بازیابی",
            "manage_discount_codes": "کدهای تخفیف",
        },
    },
}

# manage_api_keys used to be a grantable checkbox here. routers/api_keys.py
# is now hard-locked to require_superadmin instead (see its own docstring -
# ApiKey has no owner/scope column at all, so a Seller or bypass-everything
# level-2 Admin with this checkbox could see/toggle/delete every API key in
# the system, not just their own). Removed from PERMISSION_CHOICES below on
# purpose: parse_permissions simply stops recognizing the string, so any
# admin/group that already had it stored silently loses that (already-dead)
# entry - no migration needed.

# Flat key -> label map, derived from PERMISSION_GROUPS - this is what
# parse_permissions/format_permissions validate membership against.
PERMISSION_CHOICES: dict[str, str] = {
    key: label
    for group in PERMISSION_GROUPS.values()
    for key, label in group["perms"].items()
}

# Old broad toggle -> equivalent new granular keys, applied transparently
# whenever permissions are read (see parse_permissions) so admins/groups
# saved under the old 4-toggle scheme keep exactly the access they had
# before - no destructive one-off DB migration needed, old and new keys
# simply coexist in the `permissions` column forever.
_LEGACY_EXPANSION: dict[str, list[str]] = {
    "manage_nodes": ["view_nodes", "edit_nodes", "delete_nodes"],
    "manage_packages": ["view_packages", "edit_packages", "delete_packages"],
    "manage_tutorials": ["view_tutorials", "edit_tutorials", "delete_tutorials"],
    "manage_settings": [
        "manage_payment_settings", "manage_bot_settings",
        "manage_backup", "manage_discount_codes",
    ],
}


def parse_permissions(raw: str | None) -> set[str]:
    if not raw:
        return set()
    result: set[str] = set()
    for p in (x.strip() for x in raw.split(",")):
        if p in PERMISSION_CHOICES:
            result.add(p)
        elif p in _LEGACY_EXPANSION:
            result.update(_LEGACY_EXPANSION[p])
    return result


def format_permissions(perms: set[str] | list[str]) -> str:
    return ",".join(p for p in perms if p in PERMISSION_CHOICES)


def effective_permissions(admin) -> set[str]:
    """The permission set that actually applies to this admin - their
    linked AdminPermissionGroup's permissions if they're in one
    (AdminUser.group_id), otherwise their own `permissions` column, exactly
    like before groups existed. Centralized here so deps.py/auth.py/
    routers/admins.py all agree on the same rule instead of each reading
    `admin.permissions` directly (which would silently ignore a group)."""
    if getattr(admin, "group", None) is not None:
        return parse_permissions(admin.group.permissions)
    return parse_permissions(admin.permissions)
