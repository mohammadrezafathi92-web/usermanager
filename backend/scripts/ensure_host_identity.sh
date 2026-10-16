#!/usr/bin/env bash
# Create the P6 host identity once, outside the project and its DB backups.
# Never rotate, repair, follow a symlink, or print the secret on an update.
set -eu

target="${1:-/etc/usermanager/host.id}"
case "$target" in
    /*) ;;
    *) echo "STOP: host identity path must be absolute" >&2; exit 1 ;;
esac
directory="$(dirname -- "$target")"
if [ -L "$directory" ]; then
    echo "STOP: host identity directory is a symlink" >&2
    exit 1
fi
if [ ! -d "$directory" ]; then
    mkdir -m 0700 -- "$directory"
fi

if [ ! -e "$target" ] && [ ! -L "$target" ]; then
    temporary="$(mktemp "$directory/.host.id.XXXXXXXX")"
    trap 'rm -f -- "$temporary"' EXIT
    dd if=/dev/urandom of="$temporary" bs=32 count=1 iflag=fullblock status=none
    chmod 0600 "$temporary"
    # Hard-link publication is atomic and refuses to replace an existing file.
    # A concurrent installer may have published its own file first. In that
    # case validate the winner below, never replace it with this candidate.
    ln -- "$temporary" "$target" 2>/dev/null || :
    rm -- "$temporary"
    trap - EXIT
fi

if [ -L "$target" ] || [ ! -f "$target" ] || \
   [ "$(stat -c %s -- "$target")" != 32 ] || \
   [ "$(stat -c %a -- "$target")" != 600 ]; then
    echo "STOP: host identity must be a regular 32-byte file with mode 0600" >&2
    exit 1
fi
echo "Host identity is present and valid (secret not shown)."
