# All recipes run from the repository root.
default:
    @just --list

test:
    PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# Install or update a developer CLI using pipx.
install-local:
    pipx install --force .

# Run an already installed CLI in the foreground on localhost.
serve:
    SHELL_SHEATH_TRANSPORT=http shell-sheath

# Requires pipx installed system-wide; prompts for sudo if needed.
install-service:
    sudo bash scripts/install-service.sh

status:
    systemctl --no-pager status shell-sheath.service

logs:
    sudo journalctl -u shell-sheath.service -f

restart:
    sudo systemctl restart shell-sheath.service

stop:
    sudo systemctl stop shell-sheath.service

# WARNING: Docker membership effectively grants host-root control.
enable-docker:
    sudo usermod -aG docker shell-sheath
    sudo systemctl restart shell-sheath.service
