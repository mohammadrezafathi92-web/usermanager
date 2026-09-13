#!/bin/sh
# Generates the Caddyfile from PANEL_DOMAIN, then runs Caddy.
#
# The hostname is a per-install value, so it lives in the repo-root .env
# (written by تنظیمات > دامنه و SSL - see backend/app/services/panel_tls.py)
# rather than in a tracked config file that every `git pull` would fight.
# Generating the config here keeps it in exactly one place.
set -eu

if [ -z "${PANEL_DOMAIN:-}" ]; then
    echo "PANEL_DOMAIN تنظیم نشده است - این کانتینر بدون نام دامنه کاری ندارد." >&2
    echo "دامنه را از تنظیمات > دامنه و SSL ثبت کنید." >&2
    exit 1
fi

EMAIL_LINE=""
if [ -n "${PANEL_TLS_EMAIL:-}" ]; then
    EMAIL_LINE="	email ${PANEL_TLS_EMAIL}"
fi

cat > /etc/caddy/Caddyfile <<EOF
{
${EMAIL_LINE}
	# Caddy's own admin API, off. It has no authentication and would
	# otherwise listen inside the container - nothing here uses it.
	admin off
}

${PANEL_DOMAIN} {
	encode zstd gzip

	# Everything goes to the panel's existing nginx, which already knows
	# how to split /api/ from the built frontend. Deliberately NOT a
	# second copy of that routing: two places deciding what /api/ means is
	# how they drift apart.
	reverse_proxy frontend:80 {
		header_up X-Forwarded-Proto {scheme}
		header_up X-Real-IP {remote_host}

		# Long enough for a remote-bot deploy, matching the timeout in
		# frontend/nginx.conf - a shorter one here would cut the request
		# before nginx's own limit and leave the panel showing a bare 504.
		transport http {
			read_timeout 660s
			dial_timeout 30s
		}
	}

	# A Telegram Mini App is loaded inside Telegram's own webview, which
	# frames the page. A default-deny frame policy would show a blank box
	# with no error, so the panel says explicitly who may frame it.
	header {
		X-Content-Type-Options nosniff
		Referrer-Policy strict-origin-when-cross-origin
		-Server
	}
}
EOF

echo "Caddyfile برای دامنه ${PANEL_DOMAIN} ساخته شد."
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
