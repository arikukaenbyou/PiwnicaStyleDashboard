#!/bin/bash
# User-local install for Arch Linux -- no sudo needed (dependencies are only checked).
#   ./install.sh            install to ~/.local, autostart on login, start now
#   ./install.sh --remove   stop and remove (config in ~/.config/piwnica-dashboard is kept)
# System-wide alternative: packaging/PKGBUILD (makepkg -si).
set -euo pipefail
cd "$(dirname "$0")"
SHARE="$HOME/.local/share/piwnica-dashboard"
BIN="$HOME/.local/bin/piwnica-dashboard"
AUTOSTART="$HOME/.config/autostart/piwnica-dashboard.desktop"
UNIT=piwnica-dashboard

stop_running() {
    systemctl --user stop "$UNIT" 2>/dev/null || true
    systemctl --user reset-failed "$UNIT" 2>/dev/null || true
    for p in $(ps -u "$(id -un)" -o pid=,args= | awk '$0 ~ /python3 -m piwnica_dashboard/ {print $1}'); do kill "$p" 2>/dev/null || true; done
}

if [ "${1:-}" = "--remove" ]; then
    stop_running
    rm -rf "$SHARE" "$BIN" "$AUTOSTART"
    echo "Removed. Config kept in ~/.config/piwnica-dashboard/."
    exit 0
fi

# dependencies (Arch package names)
need=(python python-gobject python-cairo python-numpy gtk3 xorg-xprop util-linux)
missing=()
for p in "${need[@]}"; do pacman -Q "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing packages: ${missing[*]}"
    echo "Install them with:  sudo pacman -S --needed ${missing[*]}"
    exit 1
fi
# (no `fc-list | grep -q`: with pipefail, grep exiting early makes fc-list die of SIGPIPE)
fonts=$(fc-list : family 2>/dev/null || true)
pacman -Q ttf-jetbrains-mono >/dev/null 2>&1 || [[ "$fonts" == *"JetBrains Mono"* ]] || \
    echo "Tip: sudo pacman -S ttf-jetbrains-mono  (panel font; a fallback monospace is used otherwise)"

echo "== tests"
/usr/bin/python3 -m unittest discover -s tests -t . >/dev/null 2>&1 || { echo "Tests fail on this machine -- not installing."; /usr/bin/python3 -m unittest discover -s tests -t .; exit 1; }

echo "== install to ~/.local"
rm -rf "$SHARE"
install -d "$SHARE" "$(dirname "$BIN")" "$(dirname "$AUTOSTART")"
cp -r piwnica_dashboard "$SHARE/"
find "$SHARE" -name __pycache__ -prune -exec rm -rf {} +
install -m755 packaging/piwnica-dashboard "$BIN"
sed "s|^Exec=.*|Exec=$BIN run|" packaging/piwnica-dashboard.desktop > "$AUTOSTART"

echo "== detected on this machine"
"$BIN" detect || true

echo "== start"
stop_running
if [ -n "${DISPLAY:-}" ]; then
    # own transient unit: survives the terminal it was started from
    systemd-run --user --unit="$UNIT" --collect -E DISPLAY="$DISPLAY" ${XAUTHORITY:+-E XAUTHORITY="$XAUTHORITY"} "$BIN" run >/dev/null
    sleep 3
    systemctl --user is-active --quiet "$UNIT" && echo "running (journalctl --user -u $UNIT)" || \
        { echo "failed to start:"; journalctl --user -u "$UNIT" -n 20 --no-pager; exit 1; }
else
    echo "no DISPLAY -- it will start with your next graphical login"
fi
echo "Config: ~/.config/piwnica-dashboard/config.toml   Remove: ./install.sh --remove"
