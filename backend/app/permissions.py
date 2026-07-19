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
destructive changes. Expanded once into per-page view/edit/delete triples
(plus settings split into its actual sub-areas), then PRUNED HARD back
down here (3-tier hierarchy audit) once it became clear almost none of
that granularity was ever actually reachable:

- A level-2 Admin bypasses EVERY permission check unconditionally (see
  deps.py's require_permission - `hierarchy.role(admin) == ROLE_ADMIN`
  short-circuits it) - no checkbox here has EVER controlled what an Admin
  can do, only what a Seller can.
- nodes/packages: a Seller has NO real access regardless of any checkbox -
  hierarchy.accessible_node_ids is unconditionally empty for a Seller, and
  _require_package_manager (routers/packages.py) unconditionally 403s any
  Seller trying to create/edit/delete a package. Those two checkbox groups
  did nothing for anyone, ever - removed entirely (a Seller still gets
  full package VIEW + their own resale-price override unconditionally, see
  routers/packages.py's set_my_package_price - deliberately not gated by
  any checkbox at all, since that's a Seller's own data, not shared).
- tutorials-edit/delete, discount codes, and payment settings: confirmed
  with the panel owner that these must stay level-2-Admin-only, NEVER
  Seller-grantable, because Tutorial/DiscountCode/PanelSettings have no
  owner_admin_id at all - they're single panel-wide/global rows or tables.
  A Seller holding any of these checkboxes could edit/delete content or
  settings visible to and used by every OTHER Admin's and Seller's
  customers, not just their own - the opposite of this whole hierarchy
  feature's "هر ادمین یوزرمنیجر شخصی خودش رو داشته باشه" isolation
  principle. Now hard-blocked server-side for a Seller regardless of any
  checkbox (see routers/tutorials.py/discount_codes.py/panel_settings.py's
  _require_admin_tier-style checks), so removed from here too - keeping a
  checkbox that can never do anything for anyone is just confusing UI.
- bot settings / backup: own-bot (own_bot_token) and own-backup are
  already unconditionally available to every Admin/Seller with no
  checkbox at all (by design, see their own routers); the GLOBAL bot
  settings/remote-deploy and the FULL-DB backup are hard-locked to
  require_superadmin instead, also regardless of any checkbox. Removed
  for the same "does nothing for anyone" reason as api_keys before it.

What's left is the ONE thing that's both real AND safely Seller-scopable
today: viewing tutorials (read-only, harmless either way). PERMISSION_GROUPS
below is the grouped-by-page shape the frontend renders as sectioned
checkboxes; PERMISSION_CHOICES is the flat key->label view used for
validation/storage, derived from it.
"""

PERMISSION_GROUPS: dict[str, dict] = {
    "tutorials": {
        "label": "آموزش",
        "perms": {
            "view_tutorials": "مشاهده آموزش‌ها",
        },
    },
}

# Flat key -> label map, derived from PERMISSION_GROUPS - this is what
# parse_permissions/format_permissions validate membership against.
PERMISSION_CHOICES: dict[str, str] = {
    key: label
    for group in PERMISSION_GROUPS.values()
    for key, label in group["perms"].items()
}

# Old broad toggle / now-removed granular key -> equivalent SURVIVING
# key(s), applied transparently whenever permissions are read (see
# parse_permissions) so an admin/group saved under any older scheme keeps
# whatever subset of their old access is still a real, grantable thing -
# no destructive one-off DB migration needed, old and new keys simply
# coexist in the `permissions` column forever. Every mapping below that
# used to expand into now-removed keys (view_nodes, edit_nodes,
# delete_nodes, view_packages, edit_packages, delete_packages,
# edit_tutorials, delete_tutorials, manage_payment_settings,
# manage_bot_settings, manage_backup, manage_discount_codes,
# manage_api_keys) simply drops those - parse_permissions below only ever
# keeps keys that are still in PERMISSION_CHOICES today.
_LEGACY_EXPANSION: dict[str, list[str]] = {
    "manage_nodes": [],
    "manage_packages": [],
    "manage_tutorials": ["view_tutorials"],
    "manage_settings": [],
}


def parse_permissions(raw: str | None) -> set[str]:
    if not raw:
        return set()
    result: set[str] = set()
    for p in (x.strip() for x in raw.split(",")):
        if p in PERMISSION_CHOICES:
            result.add(p)
        elif p in _LEGACY_EXPANSION:
            result.update(k for k in _LEGACY_EXPANSION[p] if k in PERMISSION_CHOICES)
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
