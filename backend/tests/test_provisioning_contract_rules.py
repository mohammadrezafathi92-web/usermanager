"""Parent and child share identical closed matcher and adapter-version rules."""
import ast
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
from app.services import provisioning_contract_rules as rules, provisioning_contracts as parent

assert parent.adapter_version is rules.adapter_version
assert parent.validate_contract is rules.validate_contract
assert parent._FIELDS == rules.ENDPOINT_FIELDS
assert len(rules.adapter_version()) == 40
tree = ast.parse(Path(rules.__file__).read_text())
assert not any(isinstance(node, ast.ImportFrom) and node.module and (
    "models" in node.module or node.module in ("database", "config", "sqlalchemy.orm")) for node in ast.walk(tree))
for backend in ("mikrotik_wg", "softether", "xray_ssh", "threexui", "marzban", "hiddify", "marzneshin", "sui"):
    assert rules.validate_contract('{"not_exist":[]}', backend) == {"not_exist": []}
    good_http = {"kind": "http", "op": "delete", "status": 404, "json_path": "error.code", "equals": "NOT_FOUND"}
    good_rpc = {"kind": "jsonrpc", "op": "delete", "error_code": 42}
    candidates = [([], False), ({}, False), ({"not_exist": [{"kind": "http", "op": "delete", "status": 404}]}, False),
        ({"not_exist": [good_http]}, backend in ("threexui", "marzban", "hiddify", "marzneshin", "sui")),
        ({"not_exist": [good_rpc]}, backend == "softether"),
        ({"not_exist": [{**good_http, "json_path": "arbitrary.secret"}]}, False),
        ({"not_exist": [{**good_http, "equals": True}]}, False),
        ({"not_exist": [{**good_http, "wildcard": True}]}, False)]
    for value, accepted in candidates:
        try:
            rules.validate_contract(json.dumps(value), backend)
            assert accepted, (backend, value)
        except ValueError:
            assert not accepted, (backend, value)
print("PASS shared parent/child contract validation for all eight remote backends")
