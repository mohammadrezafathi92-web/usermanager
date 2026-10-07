"""Two explicitly supported versions of the one ownership CHECK."""
from sqlalchemy import inspect

NAME = "ck_provrt_enforced_needs_owner"
LEGACY = "gate_mode <> 'enforced' OR owner_state IN ('active', 'draining')"
RELEASED = "gate_mode <> 'enforced' OR owner_state IN ('active', 'draining', 'released')"


def supports_release(connection):
    from .provisioning_schema import normalize_check
    checks = inspect(connection).get_check_constraints("provisioning_runtime_state")
    matching = [row for row in checks if row.get("name") == NAME]
    return len(matching) == 1 and normalize_check(matching[0].get("sqltext")) == normalize_check(RELEASED)
