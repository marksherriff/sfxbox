#!/usr/bin/env python3
"""Keyboard-driven sound effects box for Raspberry Pi Linux."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import evdev  # type: ignore[import-not-found]
    from evdev import ecodes  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for runtime
    evdev = None
    ecodes = None

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"
DEFAULT_SOUNDS_DIR = BASE_DIR / "sounds"


class ConfigError(RuntimeError):
    """Raised when configuration cannot be parsed or loaded."""


class SoundController:
    """Plays sounds without overlapping playback and with a brief debounce."""

    def __init__(self, config: Dict[str, Any], debounce_seconds: float = 0.25) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._playing = False
        self._last_played_at = 0.0
        self._debounce_seconds = debounce_seconds

    def play(self, sound_path: Optional[str], *, cooldown: Optional[float] = None) -> bool:
        if not sound_path:
            return False

        resolved_path = resolve_sound_path(sound_path)
        if not os.path.isfile(resolved_path):
            print(f"Sound file not found: {resolved_path}")
            return False

        now = time.monotonic()
        effective_cooldown = cooldown if cooldown is not None else self._debounce_seconds
        if now - self._last_played_at < effective_cooldown:
            return False

        with self._lock:
            if self._playing:
                return False
            self._playing = True
            self._last_played_at = now

        def _worker() -> None:
            try:
                subprocess.run(
                    ["aplay", "-q", resolved_path],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                print("aplay is not available; install ALSA utilities to play sounds.")
            finally:
                with self._lock:
                    self._playing = False

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def play_ready(self) -> bool:
        ready_sound = self._config.get("ready_sound")
        return self.play(ready_sound, cooldown=0.0)

    def play_for_key(self, key_name: str) -> bool:
        sounds = self._config.get("sounds", {}) or {}
        sound_path = sounds.get(key_name.lower()) or sounds.get("default")
        return self.play(sound_path)


class SfxBoxService:
    def __init__(
        self,
        config: Dict[str, Any],
        *,
        debug: bool,
        device_path: Optional[str],
        dry_run: bool,
    ) -> None:
        self._config = config
        self._debug = debug or bool(config.get("debug", False))
        self._device_path = device_path
        self._dry_run = dry_run
        self._sound_controller = SoundController(
            config,
            debounce_seconds=float(config.get("debounce_seconds", 0.25)),
        )
        self._last_key_time = 0.0
        self._last_key_name: Optional[str] = None

    def run(self) -> None:
        print("Starting sfxbox...")
        self._sound_controller.play_ready()

        if self._dry_run:
            print("Dry run enabled; waiting for Ctrl+C.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("Exiting.")
            return

        if evdev is None:
            raise RuntimeError("evdev is not installed. Install it with 'pip install evdev'.")

        device = self._open_device()
        if device is None:
            raise RuntimeError("No keyboard-style input device was found.")

        print(f"Listening for keyboard input on {device}")
        try:
            for event in device.read_loop():
                if event.type != ecodes.EV_KEY or event.value not in (1, 2):
                    continue
                if event.value == 2:
                    continue

                key_name = self._get_key_name(event.code)
                if not key_name:
                    continue

                if self._debug:
                    print(f"Key pressed: {key_name}")

                if not self._should_process_key(key_name):
                    continue

                self._sound_controller.play_for_key(key_name)
        except KeyboardInterrupt:
            print("Keyboard interrupt received; shutting down.")
        finally:
            device.close()

    def _open_device(self) -> Optional[Any]:
        if self._device_path:
            return evdev.InputDevice(self._device_path)

        for device_path in evdev.list_devices():
            try:
                device = evdev.InputDevice(device_path)
            except OSError:
                continue
            capabilities = device.capabilities()
            if ecodes.EV_KEY in capabilities:
                return device
        return None

    def _should_process_key(self, key_name: str) -> bool:
        now = time.monotonic()
        same_key = self._last_key_name == key_name
        if same_key and now - self._last_key_time < self._sound_controller._debounce_seconds:
            return False
        self._last_key_time = now
        self._last_key_name = key_name
        return True

    @staticmethod
    def _get_key_name(code: int) -> Optional[str]:
        if ecodes is None:
            return None
        name = ecodes.KEY.get(code)
        if not name:
            return None
        return name.lower()


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    config_path = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    default_config: Dict[str, Any] = {
        "ready_sound": str(DEFAULT_SOUNDS_DIR / "ready.wav"),
        "debug": False,
        "debounce_seconds": 0.25,
        "sounds": {"default": str(DEFAULT_SOUNDS_DIR / "default.wav")},
    }

    if not config_path.exists():
        return default_config

    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - fallback branch
        yaml = None

    if yaml is not None:
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        except Exception as exc:  # pragma: no cover - defensive path
            raise ConfigError(f"Unable to read YAML config {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("Config root must be a mapping.")
        config = default_config.copy()
        config.update(loaded)
        if not isinstance(config.get("sounds"), dict):
            raise ConfigError("Config 'sounds' must be a mapping.")
        return config

    return _load_simple_yaml(config_path, default_config)


def _load_simple_yaml(config_path: Path, default_config: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = default_config.copy()
    current_section: Optional[str] = None

    with config_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue

            if line.startswith(" "):
                if current_section != "sounds":
                    raise ConfigError("Nested YAML entries are only supported under 'sounds'.")
                key, raw_value = [part.strip() for part in line.split(":", 1)]
                if not key:
                    raise ConfigError("Invalid mapping entry in YAML config.")
                data["sounds"][key] = _parse_scalar(raw_value)
                continue

            key, raw_value = [part.strip() for part in line.split(":", 1)]
            if not key:
                raise ConfigError("Invalid YAML key.")

            if raw_value == "":
                data[key] = {}
                current_section = key
            else:
                data[key] = _parse_scalar(raw_value)
                current_section = None

    if not isinstance(data.get("sounds"), dict):
        raise ConfigError("Config 'sounds' must be a mapping.")

    return data


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    return value


def resolve_sound_path(sound_path: str) -> str:
    expanded_path = Path(os.path.expanduser(sound_path))
    if expanded_path.is_file():
        return str(expanded_path)

    repo_local_path = BASE_DIR / "sounds" / expanded_path.name
    if repo_local_path.is_file():
        return str(repo_local_path)

    return str(expanded_path)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SFX box service")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the YAML config file")
    parser.add_argument("--device", default=None, help="Optional path to a specific input device")
    parser.add_argument("--debug", action="store_true", help="Print incoming key presses")
    parser.add_argument("--dry-run", action="store_true", help="Start without opening a HID device")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    service = SfxBoxService(
        config=config,
        debug=args.debug,
        device_path=args.device,
        dry_run=args.dry_run,
    )
    try:
        service.run()
    except Exception as exc:  # pragma: no cover - top-level guardprint
        print(f"sfxbox failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
