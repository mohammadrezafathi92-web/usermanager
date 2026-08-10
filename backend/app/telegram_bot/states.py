from aiogram.fsm.state import State, StatesGroup


class AdminCreateUserStates(StatesGroup):
    waiting_username = State()
    picking_node = State()      # NodeCB callback expected
    picking_protocol = State()  # ProtocolCB callback expected
    waiting_quota = State()
    waiting_days = State()


class AdminSearchStates(StatesGroup):
    # username fragment the admin types to filter the bot's user list
    waiting_username = State()


class AdminRenewStates(StatesGroup):
    waiting_values = State()  # "<add_gb> <add_days>"


class AdminBalanceStates(StatesGroup):
    waiting_amount = State()  # signed toman amount to add to (or take off) a customer's wallet


class CustomerLinkStates(StatesGroup):
    waiting_username = State()


class CustomerPurchaseStates(StatesGroup):
    picking_service = State()   # RenewServiceCB callback expected - renewals only, and only
                                # when the customer has 2+ independent services to choose from
    picking_session_count = State()  # SessionCountCB callback expected - only shown when
                                      # the available packages don't all share one concurrent-
                                      # session limit (see customer.py's _start_package_picker)
    picking_package = State()   # PackageCB callback expected
    picking_node = State()      # NodeCB callback expected (only for a fresh purchase)
    picking_protocol = State()  # ProtocolCB callback expected (only for a fresh purchase)
    entering_referral_code = State()  # free text OR "promo_skip" callback - brand-new customers only
    entering_discount_code = State()  # free text OR "promo_skip" callback - shown to everyone
    entering_comment = State()  # free text OR "promo_skip" - optional service label ("new" purchases only)
    waiting_receipt = State()   # a photo message expected


class CustomerTopupStates(StatesGroup):
    picking_amount = State()       # TopupAmountCB callback expected
    waiting_custom_amount = State()  # a text message with a number expected
    waiting_receipt = State()      # a photo message expected


class AdminBroadcastStates(StatesGroup):
    waiting_text = State()   # the message to send to every telegram-linked user
    waiting_confirm = State()  # a "بله/انصراف" confirmation before actually sending


class AdminDirectMessageStates(StatesGroup):
    """«✉️ پیام به یک کاربر» - send a message to ONE specific customer
    instead of everyone (see handlers/admin_broadcast.py)."""
    waiting_target = State()  # username (or numeric telegram id) typed by the admin
    waiting_text = State()    # the message body
