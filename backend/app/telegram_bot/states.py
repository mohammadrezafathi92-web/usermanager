from aiogram.fsm.state import State, StatesGroup


class AdminCreateUserStates(StatesGroup):
    waiting_username = State()
    picking_node = State()      # NodeCB callback expected
    picking_protocol = State()  # ProtocolCB callback expected
    waiting_quota = State()
    waiting_days = State()


class AdminSearchStates(StatesGroup):
    waiting_username = State()


class AdminRenewStates(StatesGroup):
    waiting_values = State()  # "<add_gb> <add_days>"


class CustomerLinkStates(StatesGroup):
    waiting_username = State()


class CustomerPurchaseStates(StatesGroup):
    picking_session_count = State()  # SessionCountCB callback expected - only shown when
                                      # the available packages don't all share one concurrent-
                                      # session limit (see customer.py's _start_package_picker)
    picking_package = State()   # PackageCB callback expected
    picking_node = State()      # NodeCB callback expected (only for a fresh purchase)
    picking_protocol = State()  # ProtocolCB callback expected (only for a fresh purchase)
    entering_referral_code = State()  # free text OR "promo_skip" callback - brand-new customers only
    entering_discount_code = State()  # free text OR "promo_skip" callback - shown to everyone
    waiting_receipt = State()   # a photo message expected


class CustomerTopupStates(StatesGroup):
    picking_amount = State()       # TopupAmountCB callback expected
    waiting_custom_amount = State()  # a text message with a number expected
    waiting_receipt = State()      # a photo message expected


class AdminBroadcastStates(StatesGroup):
    waiting_text = State()   # the message to send to every telegram-linked user
    waiting_confirm = State()  # a "بله/انصراف" confirmation before actually sending
