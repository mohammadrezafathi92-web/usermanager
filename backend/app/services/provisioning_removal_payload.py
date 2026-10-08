"""Immutable private deletion payload; no queries, transport or public repr.

Only removal identity is admitted. Password and WireGuard private-key
fields must be NULL. The Xray UUID identifies the exact remote object.
"""
from dataclasses import dataclass

IDENTITY_FIELDS = frozenset(("wg_interface", "wg_peer_name", "wg_public_key", "wg_client_address",
    "wg_gateway_with_prefix", "xr_inbound_tag", "xr_panel_inbound_id", "xr_email", "account_username", "flow"))
CREDENTIAL_FIELDS = frozenset(("wg_private_key", "password", "uuid"))


class RemovalPayloadUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class RemovalPayload:
    identity: tuple
    uuid: object

    def __post_init__(self):
        if type(self.identity) is not tuple or len(self.identity) != len(IDENTITY_FIELDS) or any(
                type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str for pair in self.identity):
            raise RemovalPayloadUnavailable("removal_payload_invalid")
        values = dict(self.identity)
        if set(values) != IDENTITY_FIELDS or self.uuid is not None and type(self.uuid) is not str:
            raise RemovalPayloadUnavailable("removal_payload_invalid")
        for key, value in self.identity:
            expected_type = int if key == "xr_panel_inbound_id" else str
            if value is not None and type(value) is not expected_type:
                raise RemovalPayloadUnavailable("removal_payload_invalid")

    @classmethod
    def from_fields(cls, identity, credential):
        if type(identity) is not dict or type(credential) is not dict or set(identity) != IDENTITY_FIELDS or (
                set(credential) != CREDENTIAL_FIELDS or credential["wg_private_key"] is not None or
                credential["password"] is not None):
            raise RemovalPayloadUnavailable("removal_payload_invalid")
        return cls(tuple(sorted(identity.items())), credential["uuid"])

    def matches(self, row):
        expected = {name: row[name] if name != "wg_gateway_with_prefix" else None for name in IDENTITY_FIELDS}
        return all(type(value) is type(expected[key]) and value == expected[key] for key, value in self.identity) and (
            type(self.uuid) is type(row["staged_xr_uuid"]) and self.uuid == row["staged_xr_uuid"])
