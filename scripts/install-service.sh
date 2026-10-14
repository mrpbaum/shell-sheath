#!/usr/bin/env bash
# Run through sudo. Re-running updates the application and restarts the service.
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this installer with sudo.' >&2
    exit 1
fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
command -v pipx >/dev/null || { echo 'Install pipx with your system package manager first.' >&2; exit 1; }
command -v systemctl >/dev/null || { echo 'This installer requires systemd.' >&2; exit 1; }

if ! id shell-sheath >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/shell-sheath \
        --shell /usr/sbin/nologin shell-sheath
fi

# Root-owned application, separate from the service's writable state directory.
umask 022
install -d -m 0755 /opt/shell-sheath
PIPX_HOME=/opt/shell-sheath/pipx PIPX_BIN_DIR=/opt/shell-sheath/bin \
    pipx install --force "$repo"
install -m 0644 "$repo/deploy/shell-sheath.service" /etc/systemd/system/shell-sheath.service
if [[ ! -e /etc/shell-sheath.env ]]; then
    install -m 0644 "$repo/deploy/shell-sheath.env.example" /etc/shell-sheath.env
else
    # Upgrade older env files without replacing explicitly configured values.
    while IFS= read -r setting; do
        [[ "$setting" == SHELL_SHEATH_*=* ]] || continue
        key=${setting%%=*}
        if ! grep -Eq "^[[:space:]]*${key}[[:space:]]*=" /etc/shell-sheath.env; then
            printf '\n%s\n' "$setting" >> /etc/shell-sheath.env
        fi
    done < "$repo/deploy/shell-sheath.env.example"
fi
systemctl daemon-reload
systemctl enable shell-sheath.service
systemctl restart shell-sheath.service
systemctl --no-pager status shell-sheath.service
printf '\nMCP endpoint: http://127.0.0.1:8765/mcp (unless overridden in /etc/shell-sheath.env)\n'
