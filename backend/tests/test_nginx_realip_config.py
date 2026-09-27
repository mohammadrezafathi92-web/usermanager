"""Static guard on frontend/nginx.conf's trusted-proxy directives.

Run:  python3 backend/tests/test_nginx_realip_config.py

No nginx binary runs in CI, so nothing else would catch a future edit that
quietly drops or weakens the ngx_http_realip_module block added 2026-09-27
(see services/ip_guard.py's matching TRUSTED_PROXY_CIDRS comment for the
full story - without this, a proxy in front of nginx, like the bundled
Caddy `tls` profile, makes every visitor look like one shared IP and a
single automatic ban takes the whole panel offline for everyone at once).
This is a plain text/regex check on the config file, not a functional nginx
test - it exists to fail loudly if the directives are removed, reordered
after the location block, or if their values stop matching the backend's
own trusted set, not to validate nginx's own module behaviour.
"""
from __future__ import annotations

import pathlib
import re
import sys

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


ROOT = pathlib.Path(__file__).resolve().parents[2]
CONF_PATH = ROOT / "frontend" / "nginx.conf"
conf = CONF_PATH.read_text(encoding="utf-8")

check("nginx.conf exists where expected", CONF_PATH.is_file(), True)

# Every check below matches against comment-stripped lines only - this
# file's own comments quote the directives by name (and one full example
# line) while explaining them, so matching the raw text would find those
# prose mentions as readily as the real config.
code = "\n".join(line for line in conf.splitlines() if not line.strip().startswith("#"))

# The exact set services/ip_guard.py's DEFAULT_TRUSTED_PROXY_CIDRS trusts -
# both layers MUST agree, or a request nginx forwards as trusted could still
# be rejected (or worse, mistrusted) on the backend side.
required_trusted = ["127.0.0.1", "::1", "172.16.0.0/12"]
for cidr in required_trusted:
    check(
        f"set_real_ip_from {cidr} is present",
        bool(re.search(rf"set_real_ip_from\s+{re.escape(cidr)}\s*;", code)),
        True,
    )

check(
    "real_ip_header is X-Real-IP (matches what Caddy/the admin's own proxy sets, "
    "and what services/ip_guard.py reads)",
    bool(re.search(r"real_ip_header\s+X-Real-IP\s*;", code)),
    True,
)

check(
    "real_ip_recursive is explicitly set (off, not left to nginx's own default) - "
    "recursive mode would walk a client-supplied X-Forwarded-For chain instead of "
    "trusting only the directly-connected peer, reopening the exact spoofing gap "
    "this config exists to close",
    bool(re.search(r"real_ip_recursive\s+off\s*;", code)),
    True,
)

# Order matters: the realip directives have to take effect before
# `proxy_set_header X-Real-IP $remote_addr;` runs inside location /api/,
# since they change what $remote_addr MEANS. A future edit that moves the
# proxy_pass block above them (or into a sibling server block) would silently
# revert to trusting whichever proxy happens to be directly in front, with no
# error from nginx itself.
realip_pos = code.find("real_ip_header")
proxy_pos = code.find("proxy_set_header X-Real-IP $remote_addr;")
check(
    "the real_ip directives appear BEFORE the X-Real-IP proxy_set_header line",
    -1 < realip_pos < proxy_pos,
    True,
)

# The line the whole fix hinges on must still be there, unchanged - if this
# ever becomes `proxy_set_header X-Real-IP $http_x_real_ip;` (passing a
# client-supplied header straight through) instead, a caller hitting nginx
# directly (bypassing Caddy) could inject X-Real-IP itself, reintroducing
# spoofing at the nginx layer even with the realip module correctly configured.
check(
    "the X-Real-IP forwarded to the backend still comes from $remote_addr, "
    "not directly from a client-controllable variable",
    bool(re.search(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr\s*;", code)),
    True,
)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
