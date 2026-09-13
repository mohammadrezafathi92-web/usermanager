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
import pathlib
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

print("\n--- readiness is decided END-TO-END, not by comparing IPs ---")
# Reported 2026-09-13: the check announced "this server is 31.171.101.237",
# an address the operator had never seen, and refused a correctly-pointed
# domain. A minute later it said 88.218.18.156 and agreed. Both came from
# asking an outside service "what is my IP" - and the backend container runs
# its own WireGuard tunnel to reach Telegram, so the answer is whichever
# route happened to be up. A value that changes minute to minute cannot
# decide anything.
with patch.object(panel_tls, "resolve", return_value=["5.6.7.8"]), \
     patch.object(panel_tls, "reaches_this_panel", return_value=(True, "ok")), \
     patch.object(panel_tls, "ports_in_use", return_value=[]):
    r = panel_tls.check_dns("panel.example.com")
    check("reaching ourselves over the name is what makes it ok", r["ok"], True)
    check("...and no IP comparison is involved at all", r["public_ip"], None)

with patch.object(panel_tls, "resolve", return_value=["5.6.7.8"]), \
     patch.object(panel_tls, "reaches_this_panel", return_value=(False, "another server")), \
     patch.object(panel_tls, "ports_in_use", return_value=[]):
    r = panel_tls.check_dns("panel.example.com")
    check("landing on someone else is not ok", r["ok"], False)
    check("...and that is a definite no, not a doubt", r["unknown"], False)

with patch.object(panel_tls, "resolve", return_value=["5.6.7.8"]), \
     patch.object(panel_tls, "reaches_this_panel", return_value=(None, "no answer")), \
     patch.object(panel_tls, "ports_in_use", return_value=[]):
    r = panel_tls.check_dns("panel.example.com")
    check("no answer is a DOUBT, not a refusal", (r["ok"], r["unknown"]), (False, True))

with patch.object(panel_tls, "resolve", return_value=[]), \
     patch.object(panel_tls, "reaches_this_panel", return_value=(None, "no answer")), \
     patch.object(panel_tls, "ports_in_use", return_value=[]):
    r = panel_tls.check_dns("panel.example.com")
    check("a record that does not exist is a definite no", (r["ok"], r["unknown"]), (False, False))
    check("...and the message says to create one", "رکورد A" in r["reason"], True)

print("\n--- the panel can prove it is itself ---")
check("the token is stable across calls", panel_tls.instance_token(), panel_tls.instance_token())
check("...and long enough not to be guessed", len(panel_tls.instance_token()) >= 16, True)
from app.routers import panel_settings as ps_router  # noqa: E402
check("it is served without a session, since the point is to be reached from outside",
      ps_router.tls_echo_router.routes[0].path, "/api/tls-echo")

print("\n--- enable() refuses before spending an issuance ---")
with tempfile.TemporaryDirectory() as tmp:
    env_path = os.path.join(tmp, ".env")
    _write_env_var(env_path, "COMPOSE_PROFILES", "license-server")
    with patch.object(panel_tls, "_root_env_path", return_value=env_path), \
         patch.object(panel_tls, "verify_host_path", return_value=tmp), \
         patch.object(panel_tls, "HOST_PROJECT_DIR", tmp), \
         patch.object(panel_tls, "resolve", return_value=["1.1.1.1"]), \
         patch.object(panel_tls, "ports_in_use", return_value=[]), \
         patch.object(panel_tls, "reaches_this_panel", return_value=(False, "another server")):
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
# The rollback only covers what can still be undone. Once the helper
# container has been handed the job it runs on its own, past the end of this
# request, so a failure THERE cannot be unwound from here - which is exactly
# what the fallback port above is for, and why the log is readable
# afterwards rather than returned.
check("the .env is put back if the helper cannot even be started",
      "rollback()" in src and "except DeployError" in src, True)
check("...and the operator is told where to find the panel if it goes wrong",
      "FALLBACK_HTTP_PORT" in src, True)

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

print("\n--- the compose work must OUTLIVE the request that starts it ---")
# Reported while testing, as a bare "خطا در ذخیره" with no detail at all:
# enabling recreates the frontend container, which is the nginx proxying the
# very request asking for it. Run as an ordinary subprocess it kills its own
# connection half-way through, so the browser gets no response - not even an
# error to show - and whether it completed is anyone's guess.
src_apply = inspect.getsource(panel_tls._apply)
check("it is handed to a sibling container", "spawn_sibling_container" in src_apply, True)
check("...and nothing is run as a plain subprocess any more",
      "_run(" in inspect.getsource(panel_tls), False)
check("enable() goes through it", "_apply(" in inspect.getsource(panel_tls.enable), True)
check("disable() too - it recreates the frontend as well",
      "_apply(" in inspect.getsource(panel_tls.disable), True)

# The sibling mounts only the project directory, so the compose binary has
# to be named by its path under THAT mount - /app/data/... exists in this
# container and nowhere in the sibling.
check("the compose binary is addressed by its host path, not /app/data",
      "backend" in src_apply and '".docker-cli"' in src_apply, True)
check("...and the log is written somewhere readable afterwards",
      "log_path=LOG_PATH" in src_apply, True)
check("turning off stops caddy BEFORE the frontend wants port 80 back",
      src_apply.index("steps.insert(0") < src_apply.index("steps.append("), True)

print("\n--- and the panel can read what it did ---")
from app.routers import panel_settings as ps  # noqa: E402
check("there is an endpoint for the log", hasattr(ps, "get_tls_log"), True)
check("...which reads the file the sibling wrote",
      "read_log" in inspect.getsource(ps.get_tls_log), True)

print("\n--- a port clash is refused BEFORE anything is written ---")
# Reported 2026-09-13 on the vendor's own server, which serves
# license.netcip.ir on 443: the profile got written, Caddy could not bind,
# and from then on EVERY `docker compose up` aborted on it - so an ordinary
# `bash update.sh` could not bring the panel up at all. One failed switch
# took out the whole stack.
with tempfile.TemporaryDirectory() as tmp:
    env_path = os.path.join(tmp, ".env")
    _write_env_var(env_path, "COMPOSE_PROFILES", "license-server")
    with patch.object(panel_tls, "_root_env_path", return_value=env_path), \
         patch.object(panel_tls, "verify_host_path", return_value=tmp), \
         patch.object(panel_tls, "HOST_PROJECT_DIR", tmp), \
         patch.object(panel_tls, "ports_in_use", return_value=["443 (nginx)"]):
        pathlib.Path(tmp, "docker-compose.yml").write_text("services: {}\n")
        try:
            panel_tls.enable("panel.example.com")
            outcome = "went ahead"
        except DeployError as exc:
            outcome = "refused"
            detail = str(exc)
        check("a taken port is refused", outcome, "refused")
        check("...naming the port and what holds it", "443" in detail and "nginx" in detail, True)
        check("...and nothing was written, so `up` still works",
              _read_env_var(env_path, "PANEL_DOMAIN"), None)
        check("...and the tls profile was never added",
              _read_env_var(env_path, "COMPOSE_PROFILES"), "license-server")

        # force is for overriding a DNS doubt. It cannot conjure a free port,
        # so it must not be able to skip this one.
        try:
            panel_tls.enable("panel.example.com", skip_dns_check=True)
            forced = "went ahead"
        except DeployError:
            forced = "refused"
        check("and «به‌هرحال ادامه بده» cannot override a port clash", forced, "refused")

print("\n--- a panel behind someone else's nginx is a supported shape, not a broken one ---")
# 2026-09-13: the vendor's own server serves license.netcip.ir on 443, so the
# panel went behind that same nginx on a second hostname. The card must
# recognise that rather than offering to seize ports another service holds.
with tempfile.TemporaryDirectory() as tmp:
    env_path = os.path.join(tmp, ".env")
    with patch.object(panel_tls, "_root_env_path", return_value=env_path):
        _write_env_var(env_path, "PANEL_DOMAIN", "panel.netcip.ir")
        _write_env_var(env_path, "PANEL_WEB_PORT", "8080")
        _write_env_var(env_path, "COMPOSE_PROFILES", "")
        check("hostname set, moved off 80, our own TLS off = a proxy in front",
              panel_tls.behind_reverse_proxy(), True)

        _write_env_var(env_path, "COMPOSE_PROFILES", "tls")
        check("...but not when WE are the one serving TLS",
              panel_tls.behind_reverse_proxy(), False)

        _write_env_var(env_path, "COMPOSE_PROFILES", "")
        _write_env_var(env_path, "PANEL_WEB_PORT", "80")
        check("...nor on an ordinary install still on port 80",
              panel_tls.behind_reverse_proxy(), False)

        _write_env_var(env_path, "PANEL_WEB_PORT", "8080")
        _write_env_var(env_path, "PANEL_DOMAIN", "")
        check("...nor with no hostname configured at all",
              panel_tls.behind_reverse_proxy(), False)

print("\n--- ...and the plain-HTTP port can be shut to the internet ---")
# Published on every interface, the panel stays reachable over http on that
# port, which walks straight around the certificate in front of it.
compose = yaml.safe_load(
    (pathlib.Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
ports = str(compose["services"]["frontend"]["ports"])
check("the bind address is configurable", "PANEL_WEB_BIND" in ports, True)
check("...and defaults to today's behaviour, so an upgrade changes nothing",
      "PANEL_WEB_BIND:-0.0.0.0" in ports, True)
check("the panel reports which it is, so it can warn",
      "bind" in panel_tls.current_state(), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("پنل می‌تواند دامنه و گواهی خودش را داشته باشد")
