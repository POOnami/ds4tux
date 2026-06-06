"""
Configuration parser for ds4tux.
Supports TOML natively, with legacy ds4drv INI import.
"""

from __future__ import annotations
import os
import sys
import logging
from typing import Optional

logger = logging.getLogger("ds4tux.config")

DEFAULT_CONFIG_PATHS = [
    "/etc/ds4tux/config.toml",
    os.path.expanduser("~/.config/ds4tux/config.toml"),
    os.path.expanduser("~/.config/ds4tux.conf"),
    "/etc/ds4tux.conf",
]

DEVICE_MODE_MAP = {"yes": "clone", "no": "genuine", "auto": "auto"}

DEFAULT_CONFIG = {
    "led": {"r": 0, "g": 0, "b": 128},
    "led_brightness": 60,
    "mapping": "xpad",
    "device_mode": "auto",
    "gyro": False,
    "keepalive_interval": 5.0,
    "auto_reconnect": True,
    "block_kmod": False,
    "daemon": False,
}


def find_config(path: Optional[str] = None) -> Optional[str]:
    if path and os.path.exists(path):
        return path
    for p in DEFAULT_CONFIG_PATHS:
        if os.path.exists(p):
            return p
    return None


def load_config(path: Optional[str] = None) -> dict:
    config_path = find_config(path)
    if not config_path:
        logger.info("No config found, using defaults")
        return dict(DEFAULT_CONFIG)

    config = dict(DEFAULT_CONFIG)

    if config_path.endswith(".toml"):
        config = _load_toml(config_path)
    elif config_path.endswith(".conf"):
        logger.info("Migrating from legacy ds4drv config: %s", config_path)
        config = _import_ds4drv_conf(config_path)
        _migrate_to_toml(config)
    else:
        if _is_toml(open(config_path).read(256)):
            config = _load_toml(config_path)
        else:
            config = _import_ds4drv_conf(config_path)

    if "copycat" in config and "device_mode" not in config:
        config["device_mode"] = DEVICE_MODE_MAP.get(config.pop("copycat"), "auto")

    return _merge_defaults(config)


def _is_toml(content: str) -> bool:
    return "[ds4drv]" not in content and "[controller" not in content


def _load_toml(path: str) -> dict:
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            logger.error("TOML parser required. Install tomli or use Python 3.11+")
            return {}

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        logger.info("Loaded config from %s", path)
        return data
    except Exception as e:
        logger.error("Failed to parse %s: %s", path, e)
        return {}


def _import_ds4drv_conf(path: str) -> dict:
    import configparser
    cp = configparser.ConfigParser()
    cp.read(path)

    config = {}
    for section in cp.sections():
        if section == "ds4drv":
            for k, v in cp[section].items():
                if k == "led":
                    try:
                        rgb = v.lstrip("#")
                        config["led"] = {
                            "r": int(rgb[0:2], 16),
                            "g": int(rgb[2:4], 16),
                            "b": int(rgb[4:6], 16),
                        }
                    except (ValueError, IndexError):
                        pass
                elif k == "hidraw":
                    pass
                elif k == "daemon":
                    config["daemon"] = v.lower() in ("true", "yes", "1")
        elif section.startswith("controller"):
            for k, v in cp[section].items():
                if k in ("emulate-xpad", "emulate-xboxdrv"):
                    if v.lower() in ("true", "yes", "1"):
                        config["mapping"] = "xpad"
                elif k == "trackpad-mouse":
                    config["trackpad_mouse"] = v.lower() in ("true", "yes", "1")
                elif k == "led":
                    try:
                        rgb = v.lstrip("#")
                        config["led"] = {
                            "r": int(rgb[0:2], 16),
                            "g": int(rgb[2:4], 16),
                            "b": int(rgb[4:6], 16),
                        }
                    except (ValueError, IndexError):
                        pass

    return config


def _migrate_to_toml(config: dict):
    toml_path = os.path.expanduser("~/.config/ds4tux/config.toml")
    os.makedirs(os.path.dirname(toml_path), exist_ok=True)
    if os.path.exists(toml_path):
        return

    try:
        lines = ["# ds4tux configuration\n"]

        if "led" in config:
            led = config["led"]
            lines.append("[led]\n")
            lines.append(f'r = {led.get("r", 0)}\n')
            lines.append(f'g = {led.get("g", 0)}\n')
            lines.append(f'b = {led.get("b", 128)}\n')

        lines.append(f'\nmapping = "{config.get("mapping", "xpad")}"\n')
        lines.append(f'device_mode = "auto"\n')

        with open(toml_path, "w") as f:
            f.writelines(lines)
        logger.info("Migrated config to %s", toml_path)
    except Exception as e:
        logger.warning("Could not write migrated config: %s", e)


def _merge_defaults(config: dict) -> dict:
    result = dict(DEFAULT_CONFIG)
    result.update(config or {})
    return result


def _default_write_path() -> str:
    if os.geteuid() == 0:
        return "/etc/ds4tux/config.toml"
    return os.path.expanduser("~/.config/ds4tux/config.toml")


def write_config(config: dict, path: Optional[str] = None) -> str:
    if path is None:
        path = _default_write_path()
        existing = find_config()
        if existing and os.access(existing, os.W_OK):
            path = existing
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# ds4tux configuration\n\n")
        mapping = config.get("mapping", "xpad")
        f.write(f'mapping = "{mapping}"\n')
        f.write(f'led_brightness = {config.get("led_brightness", 60)}\n')
        device_mode = config.get("device_mode", "auto")
        f.write(f'device_mode = "{device_mode}"\n')
        gyro = config.get("gyro", False)
        f.write(f'gyro = {"true" if gyro else "false"}\n\n')
        led = config.get("led", {})
        f.write("[led]\n")
        f.write(f'r = {led.get("r", 0)}\n')
        f.write(f'g = {led.get("g", 0)}\n')
        f.write(f'b = {led.get("b", 128)}\n')
        devices = config.get("device", {})
        for mac, info in sorted(devices.items()):
            f.write(f'\n[device."{mac}"]\n')
            detected = info.get("detected_copycat")
            if detected is True:
                f.write(f'detected_copycat = true\n')
            elif detected is False:
                f.write(f'detected_copycat = false\n')
    return path


def save_device_info(mac: str, info: dict, path: Optional[str] = None) -> None:
    if not mac:
        return
    cfg = load_config(path)
    devices = cfg.setdefault("device", {})
    existing = devices.get(mac, {})
    existing.update(info)
    devices[mac] = existing
    write_config(cfg, path)
