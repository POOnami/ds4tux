#!/bin/sh
# install.sh — Install ds4tux and its system integration.
# Usage: [sudo] ./install.sh [--uninstall] [--user]

set -e

self="$(cd "$(dirname "$0")" && pwd)"
cd "$self"

# ── helpers ────────────────────────────────────────────────────────────
info()  { printf "\033[36m==>\033[0m %s\n" "$*"; }
warn()  { printf "\033[33m==>\033[0m %s\n" "$*" >&2; }
err()   { printf "\033[31m==>\033[0m %s\n" "$*" >&2; exit 1; }
sudocmd() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else command -v sudo >/dev/null && sudo "$@"; fi
}

# ── flags ──────────────────────────────────────────────────────────────
mode="install"
userinstall=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) mode="uninstall" ;;
        --user)      userinstall=true ;;
        --help|-h)
            echo "Usage: [sudo] $0 [--uninstall] [--user]"
            echo ""
            echo "  Install ds4tux system-wide (requires root):"
            echo "    sudo $0"
echo "      Creates isolated venv at /opt/ds4tux/venv,"
echo "      symlinks /usr/bin/ds4tux, installs"
            echo "      udev rules, config, and optionally the system service."
            echo ""
            echo "  Install for current user only (no sudo needed):"
            echo "    $0 --user"
            echo "      Creates isolated venv under ~/.local/share/ds4tux/,"
            echo "      symlinks ~/.local/bin/ds4tux."
            echo "      system service is not available in this mode."
            echo ""
            echo "  Uninstall:"
            echo "    sudo $0 --uninstall"
            exit 0
            ;;
        *) err "Unknown option: $arg" ;;
    esac
done

# ── detect init system ─────────────────────────────────────────────────
detect_init() {
    if command -v systemctl >/dev/null 2>&1; then
        echo "systemd"
    elif command -v rc-update >/dev/null 2>&1; then
        echo "openrc"
    else
        err "No supported init system found (systemd or openrc). Install manually."
    fi
}

init="$(detect_init)"

# ── paths ──────────────────────────────────────────────────────────────
venv_root="/opt/ds4tux/venv"
venv_bin="$venv_root/bin"
venv_python="$venv_bin/python3"

user_venv_dir="${XDG_DATA_HOME:-$HOME/.local/share}/ds4tux/venv"
user_bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"

udev_dir="/etc/udev/rules.d"
modprobe_dir="/etc/modprobe.d"
conf_dir_sys="/etc/ds4tux"
conf_dir_user="${XDG_CONFIG_HOME:-$HOME/.config}/ds4tux"

if [ "$init" = "systemd" ]; then
    svc_dir="/etc/systemd/system"
    svc_name="ds4tux.service"
    svc_file="$svc_dir/$svc_name"
    svc_src="$self/systemd/$svc_name"
else
    svc_dir="/etc/init.d"
    svc_name="ds4tux"
    svc_file="$svc_dir/$svc_name"
    svc_src="$self/openrc/$svc_name"
fi

# ── uninstall ──────────────────────────────────────────────────────────
if [ "$mode" = "uninstall" ]; then
    info "Uninstalling ds4tux..."

    if [ "$(id -u)" -eq 0 ]; then
        # Stop and remove service
        if [ "$init" = "systemd" ]; then
            sudocmd systemctl stop "$svc_name" 2>/dev/null || true
            sudocmd systemctl disable "$svc_name" 2>/dev/null || true
        else
            sudocmd rc-service ds4tux stop 2>/dev/null || true
            sudocmd rc-update delete ds4tux 2>/dev/null || true
        fi
        sudocmd rm -f "$svc_file"

        # Remove udev rules
        sudocmd rm -f "$udev_dir/50-ds4tux.rules"
        sudocmd rm -f "$udev_dir/50-ds4tux-audio.rules"
        sudocmd rm -f "$modprobe_dir/ds4tux.conf"
        sudocmd udevadm control --reload-rules 2>/dev/null || true

        # Remove system venv + symlink
        sudocmd rm -rf /opt/ds4tux
        sudocmd rm -f /usr/bin/ds4tux

        # Remove system config (ask first)
        if [ -d "$conf_dir_sys" ]; then
            printf "Remove %s? [y/N] " "$conf_dir_sys"
            read -r confirm
            case "$confirm" in
                y|Y) sudocmd rm -rf "$conf_dir_sys" ;;
            esac
        fi
    fi

    # User-level cleanup
    rm -rf "$user_venv_dir" 2>/dev/null || true
    rm -f "$user_bin_dir/ds4tux" 2>/dev/null || true

    # User config (ask first)
    if [ -d "$conf_dir_user" ]; then
        printf "Remove %s? [y/N] " "$conf_dir_user"
        read -r confirm
        case "$confirm" in
            y|Y) rm -rf "$conf_dir_user" ;;
        esac
    fi

    info "ds4tux uninstalled."
    exit 0
fi

# ── install ─────────────────────────────────────────────────────────────

# Check for system-level Python packages that can't be pip-installed.
# On failure we print the distro-specific package name and continue — the
# venv uses --system-site-packages so a later manual install will work too.
_check_gi() {
    python3 -c "import gi" 2>/dev/null && return 0
    if command -v pacman >/dev/null 2>&1; then
        warn "Missing: python-gobject  (install: pacman -S python-gobject)"
    elif command -v apt-get >/dev/null 2>&1; then
        warn "Missing: python3-gi  (install: apt install python3-gi)"
    elif command -v dnf >/dev/null 2>&1; then
        warn "Missing: python3-gobject  (install: dnf install python3-gobject)"
    else
        warn "Missing: python-gi / PyGObject (install via your package manager)"
    fi
}
_check_gi

# 1. Install Python package in a venv (system-site-packages so that
#    system-level modules like PyGObject / gi are accessible).
if [ "$(id -u)" -eq 0 ] && ! $userinstall; then
    info "Installing ds4tux into system venv at $venv_root..."
    python3 -m venv --system-site-packages "$venv_root"
    "$venv_python" -m pip install --no-input "$self"
    ln -sf "$venv_bin/ds4tux" /usr/bin/ds4tux
else
    info "Installing ds4tux into user venv at $user_venv_dir..."
    python3 -m venv --system-site-packages "$user_venv_dir"
    "$user_venv_dir/bin/python3" -m pip install --no-input "$self"
    mkdir -p "$user_bin_dir"
    ln -sf "$user_venv_dir/bin/ds4tux" "$user_bin_dir/ds4tux"
fi

# 2. Install udev rules (system-wide install only)
if [ "$(id -u)" -eq 0 ] && ! $userinstall; then
    printf "Install udev rules for controller access? [Y/n] "
    read -r install_udev || install_udev="y"
    case "$install_udev" in
        n|N) info "Skipping udev rules." ;;
        *)
            info "Installing udev rules..."
            sudocmd mkdir -p "$udev_dir"
            sudocmd cp "$self/udev/50-ds4tux.rules" "$udev_dir/"
            sudocmd cp "$self/udev/50-ds4tux-audio.rules" "$udev_dir/"
            sudocmd udevadm control --reload-rules 2>/dev/null || true
            sudocmd udevadm trigger 2>/dev/null || true
            ;;
    esac
fi

# 3. Install default config
if [ "$(id -u)" -eq 0 ] && ! $userinstall; then
    sudocmd mkdir -p "$conf_dir_sys"
    if [ ! -f "$conf_dir_sys/config.toml" ]; then
        sudocmd cp "$self/ds4tux.conf.example" "$conf_dir_sys/config.toml"
    fi
fi
if [ ! -f "$conf_dir_user/config.toml" ]; then
    mkdir -p "$conf_dir_user" 2>/dev/null || true
    cp "$self/ds4tux.conf.example" "$conf_dir_user/config.toml" 2>/dev/null || true
fi

# 4. Install and enable service (system-wide install only)
if [ "$(id -u)" -ne 0 ] || $userinstall; then
    info "Service not installed (run 'sudo $0' for system-wide install with service)."
    echo "  ds4tux binary: $user_bin_dir/ds4tux"
    echo "  (make sure $user_bin_dir is in your PATH)"
else
    printf "Install and enable the %s service? [Y/n] " "$init"
    read -r install_svc || install_svc="y"
    case "$install_svc" in
        n|N) info "Skipping service installation." ;;
        *)
            info "Installing service file..."
            sudocmd mkdir -p "$svc_dir"
            sudocmd cp "$svc_src" "$svc_file"
            if [ "$init" != "systemd" ]; then
                sudocmd chmod +x "$svc_file"
            fi

            info "Enabling service..."
            if [ "$init" = "systemd" ]; then
                sudocmd systemctl daemon-reload
                sudocmd systemctl enable "$svc_name"
                echo "  Start now:  sudo systemctl start $svc_name"
                echo "  Status:     systemctl status $svc_name"
                echo "  Logs:       journalctl -u $svc_name -f"
            else
                sudocmd rc-update add ds4tux default
                echo "  Start now:  sudo rc-service ds4tux start"
                echo "  Status:     rc-service ds4tux status"
            fi
            ;;
    esac
fi

echo ""
info "Installation complete."
echo ""
if [ "$(id -u)" -eq 0 ] && ! $userinstall; then
    echo "  ds4tux bin:  /usr/bin/ds4tux  (symlink → $venv_bin/ds4tux)"
    echo "  venv:        $venv_root"
else
    echo "  ds4tux bin:  $user_bin_dir/ds4tux  (symlink → $user_venv_dir/bin/ds4tux)"
fi
echo "  Config:      $conf_dir_user/config.toml"
echo "  Uninstall:   sudo $0 --uninstall"
