from aiogram import Router

from . import start, admin_users, admin_pending, admin_broadcast, customer, tutorials


def _detach(router: Router) -> None:
    """aiogram only lets a Router be attached to a parent ONCE for its
    entire lifetime - Router.parent_router raises RuntimeError on a second
    attach. Our per-feature routers (start.router, customer.router, ...)
    are module-level singletons created once at import time, but
    build_router() below builds a BRAND NEW root Router every time the bot
    (re)starts (Settings save/restart button, or the customer-bot on/off
    toggle - see runner.py's restart_bot()). Without detaching first, the
    SECOND start of the bot's lifetime in this process always crashed here
    with "Router is already attached to <Router 'root'>", which silently
    killed the whole in-process bot - nothing polls anymore, so BOTH the
    admin and customer side stop responding, and every later "start" hits
    the exact same crash again since the sub-routers are still attached to
    the previous (now-discarded) root."""
    router._parent_router = None


def build_router() -> Router:
    root = Router(name="root")
    for r in (start.router, admin_users.router, admin_pending.router, admin_broadcast.router, customer.router, tutorials.router):
        _detach(r)
        root.include_router(r)
    return root
