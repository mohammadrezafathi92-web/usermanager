"""Inline button callback_data schemas.

IMPORTANT: whenever a field can contain a colon (usernames, node names),
keep it as the LAST declared field of the class. aiogram packs fields
joined by ":" and unpacks with a bounded split, so only the trailing field
is safe to contain extra colons - a colon in a middle field would shift
every field after it and silently break the button (this bit the reference
mikrotik-telegram-bot project with MikroTik profile names, hence the
index-based workaround there - we avoid the whole class of bug here by
always putting the free-text field last)."""
from aiogram.filters.callback_data import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    action: str
    # home, help, admin_create, admin_list, admin_pending,
    # cust_account, cust_buy, cust_renew, cust_link, cancel


class AdminListPageCB(CallbackData, prefix="alp"):
    page: int
    search: str = "-"  # "-" sentinel = no search filter


class AdminUserCB(CallbackData, prefix="au"):
    action: str  # view, toggle, renew, resetusage, delete, delete_confirm, back_list
    username: str


class NodeCB(CallbackData, prefix="node"):
    node_id: int


class ProtocolCB(CallbackData, prefix="proto"):
    protocol: str  # wireguard, openvpn, l2tp, xray


class PackageCB(CallbackData, prefix="pkg"):
    kind: str  # "new" or "renew"
    package_id: int


class TutorialCB(CallbackData, prefix="tut"):
    tutorial_id: int


class TopupAmountCB(CallbackData, prefix="topup"):
    amount: int  # 0 = "custom amount" sentinel, ask the user to type one


class PayCB(CallbackData, prefix="pay"):
    method: str  # "balance" - instant payment from the customer's wallet, skipping the receipt/approval step


class ApprovalCB(CallbackData, prefix="appr"):
    action: str  # approve, reject
    request_id: int


class ConnectionCB(CallbackData, prefix="conn"):
    # Picks one specific service out of a customer's "👤 اکانت من" list -
    # its info (config/link/QR) is only sent once this is tapped, instead of
    # dumping every service's details automatically.
    connection_id: int


class PurchaseCB(CallbackData, prefix="purc"):
    # Picks one "purchase" group in "👤 اکانت من" (see
    # keyboards.group_connections_by_purchase). `key` is the group's
    # Connection.purchase_batch (a uuid4 hex, or "c<connection_id>" for a
    # standalone/ungrouped connection) - using the actual grouping key
    # instead of a list position means this stays correct even if the
    # customer's purchase list is re-sorted (e.g. a new purchase lands)
    # between opening "اکانت من" and tapping this button.
    key: str
