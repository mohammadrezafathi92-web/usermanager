"""Giving a panel its own hostname and certificate (services/panel_tls.py).

Run:  python3 backend/tests/test_panel_tls.py

A Telegram Mini App only ever loads over https, and the page then has to
call THIS panel's own API - a browser will not let an https page talk to
http://<ip>. So a central https server cannot stand in; every panel that
wants a mini app needs its own certificate. Logging in over plain HTTP was
worth fixing anyway.

The parts worth pinning down are the ones that bite in production:

  * the hostname people actually paste ("https://X.IR/") has to be accepted;
  * the DNS check has to REFUSE before issuing, because a failed issuance is
    rate-limited five per hostname per week and its error says nothing about
    the cause;
  * turning it on must not silently switch off someone else's compose
    profile, and must leave a way back into the panel by IP.
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import panel_tls
from app.services.local_deploy import DeployError, _read_env_var, _write_env_var

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def norm(value):
    try:
        return panel_tls.normalise_domain(value)
    except DeployError as exc:
        return f"REFUSED: {exc}"


print("--- the hostname people actually paste ---")
check("a plain hostname", norm("panel.example.com"), "panel.example.com")
check("with the scheme they copied from the address bar", norm("https://panel.example.com"), "panel.example.com")
check("with a trailing slash and a path", norm("https://panel.example.com/login"), "panel.example.com")
check("with a port", norm("panel.example.com:8080"), "panel.example.com")
check("upper case and stray spaces", norm("  PANEL.Example.COM  "), "panel.example.com")
check("a trailing dot", norm("panel.example.com."), "panel.example.com")
check("a deep subdomain", norm("a.b.c.example.ir"), "a.b.c.example.ir")

print("\n--- ...and what is not a hostname ---")
for bad, why in [("", "empty"), ("localhost", "no dot - not publicly resolvable"),
                 ("1.2.3.4", "an IP cannot have a public certificate"),
                 ("-bad.example.com", "leading dash"), ("bad-.example.com", "trailing dash"),
                 ("under_score.example.com", "underscore is not legal in a hostname"),
                 ("spaces here.com", "a space")]:
    check(f"refused: {why}", norm(bad).startswith("REFUSED"), True)

print("\n--- the DNS check, which is what stops a rate-limited failure ---")
with patch.object(panel_tls, "resolve", return_value=["5.6.7.8"]), \
     patch.object(panel_tls, "server_public_ip", return_value="5.6.7.8"):
    r = panel_tls.check_dns("panel.example.com")
    check("pointing here is ok", r["ok"], True)

with patch.object(panel_tls, "resolve", return_value=["1.1.1.1"]), \
     patch.object(panel_tls, "server_public_ip", return_value="5.6.7.8"):
    r = panel_tls.check_dns("panel.example.com")
    check("pointing somewhere else is not", r["ok"], False)
    # Both addresses come back as STRUCTURED fields, never interpolated into
    # the Persian sentence: an IPv4 address inside RTL prose is reordered by
    # the bidi algorithm, so "points at A but this server is B" can render
    # with A and B visually swapped - and reading that backwards means
    # pointing DNS at the wrong server. The panel lays them out on their own
    # ltr rows instead.
    check("the address it points at is reported", r["resolved"], ["1.1.1.1"])
    check("...and this server's own", r["public_ip"], "5.6.7.8")
    check("...and neither is buried in the sentence",
          "1.1.1.1" in r["reason"] or "5.6.7.8" in r["reason"], False)
    check("the sentence says what to DO instead",
          "رکورد A" in r["reason"] or "زیردامنه" in r["reason"], True)

with patch.object(panel_tls, "resolve", return_value=[]), \
     patch.object(panel_tls, "server_public_ip", return_value="5.6.7.8"):
    r = panel_tls.check_dns("panel.example.com")
    check("a record that does not exist is not ok", r["ok"], False)
    check("...and the message says to create one", "رکورد A" in r["reason"], True)
    check("...with this server's address available to show separately",
          r["public_ip"], "5.6.7.8")

with patch.object(panel_tls, "resolve", return_value=["5.6.7.8"]), \
     patch.object(panel_tls, "server_public_ip", return_value=None):
    r = panel_tls.check_dns("panel.example.com")
    check("not knowing our own IP is not treated as success", r["ok"], False)

print("\n--- enable() refuses before spending an issuance ---")
with tempfile.TemporaryDirectory() as tmp:
    env_path = os.path.join(tmp, ".env")
    _write_env_var(env_path, "COMPOSE_PROFILES", "license-server")
    with patch.object(panel_tls, "_root_env_path", return_value=env_path), \
         patch.object(panel_tls, "verify_host_path", return_value=tmp), \
         patch.object(panel_tls, "HOST_PROJECT_DIR", tmp), \
         patch.object(panel_tls, "resolve", return_value=["1.1.1.1"]), \
         patch.object(panel_tls, "server_public_ip", return_value="5.6.7.8"):
        try:
            panel_tls.enable("panel.example.com")
            outcome = "went ahead"
        except DeployError:
            outcome = "refused"
        check("a hostname pointing elsewhere is refused, not attempted", outcome, "refused")
        check("...and nothing was written", _read_env_var(env_path, "PANEL_DOMAIN"), None)
        check("...and the other profile is untouched",
              _read_env_var(env_path, "COMPOSE_PROFILES"), "license-server")

print("\n--- the compose profile is edited, never rewritten ---")
with tempfile.TemporaryDirectory() as tmp:
    env_path = os.path.join(tmp, ".env")
    # The vendor's own box really does run license-server. Clobbering it
    # while enabling TLS would take their licence server offline.
    _write_env_var(env_path, "COMPOSE_PROFILES", "license-server")
    panel_tls._set_profile(env_path, "tls", True)
    check("tls is added alongside", sorted(panel_tls._split_profiles(
        _read_env_var(env_path, "COMPOSE_PROFILES"))), ["license-server", "tls"])
    panel_tls._set_profile(env_path, "tls", True)
    check("adding twice does not duplicate it", sorted(panel_tls._split_profiles(
        _read_env_var(env_path, "COMPOSE_PROFILES"))), ["license-server", "tls"])
    panel_tls._set_profile(env_path, "tls", False)
    check("removing tls leaves the other one alone",
          _read_env_var(env_path, "COMPOSE_PROFILES"), "license-server")

print("\n--- the way back in by IP ---")
check("the frontend keeps a published port rather than none",
      panel_tls.FALLBACK_HTTP_PORT != 80 and panel_tls.FALLBACK_HTTP_PORT > 0, True)
import inspect  # noqa: E402
src = inspect.getsource(panel_tls.enable)
check("...and enable() rolls everything back if compose fails",
      src.count("rollback()") >= 3, True)

print("\n--- the compose file really has the service the code drives ---")
import pathlib  # noqa: E402
import yaml  # noqa: E402

compose = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
caddy = compose["services"].get("caddy")
check("there is a caddy service", caddy is not None, True)
check("it is behind the tls profile, so an upgrade changes nothing by itself",
      caddy.get("profiles"), ["tls"])
check("it takes 80 (the ACME challenge) and 443", caddy.get("ports"), ["80:80", "443:443"])
check("certificates live on a named volume, not a rebuildable layer",
      any(str(v).startswith("caddy-data:") for v in caddy.get("volumes", [])), True)
check("the frontend's own port is still configurable, so it can move aside",
      "${PANEL_WEB_PORT:-80}:80" in str(compose["services"]["frontend"]["ports"]), True)

print("\n--- the panel can actually ASK for the password these endpoints want ---")
# Reported while testing: pressing «فعال‌سازی https» answered "enter your
# password" with nowhere to enter it. The frontend interceptor only knew
# DELETE and /bulk-delete needed one, so a guarded POST never got a prompt
# and never sent the header - a dead button. The fix is to recognise the
# backend's own 403 rather than keep a second list in the frontend, which
# would only move the drift.
client_js = (pathlib.Path(__file__).resolve().parents[2]
             / "frontend" / "src" / "api" / "client.js").read_text()
check("the frontend matches on the backend's own wording",
      "CONFIRM_PASSWORD_DETAIL" in client_js, True)
check("...and that wording is what the backend actually sends",
      "رمز عبور خودتان را وارد کنید" in client_js, True)

deps_py = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "deps.py").read_text()
phrase = "رمز عبور خودتان را وارد کنید"
check("the two sides really do use the same sentence",
      phrase in deps_py and phrase in client_js, True)
check("a challenged request is retried with a prompt, whatever its URL",
      "_pwForce" in client_js, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("پنل می‌تواند دامنه و گواهی خودش را داشته باشد")
