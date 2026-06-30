#!/usr/bin/env python3
"""Low-latency, multi-HID sound effects box for Raspberry Pi Linux."""

from __future__ import annotations

import argparse
import os
import select
import sys
import threading
import time
from typing import Any, Dict, Iterable, Optional

from main import DEFAULT_CONFIG_PATH, ConfigError, load_config, normalize_key_name, resolve_sound_path

try:
    import evdev  # type: ignore[import-not-found]
    from evdev import ecodes  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for runtime
    evdev = None
    ecodes = None

try:
    import pygame  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for runtime
    pygame = None


class PygameSoundController:
    """Preloads and plays sounds through pygame.mixer with global debounce."""

    def __init__(self, config: Dict[str, Any], debounce_seconds: float = 0.25) -> None:
        self._config = config
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._last_played_at = 0.0
        self._initialized = False
        self._sounds: dict[str, Any] = {}
        self._channel: Optional[Any] = None

    def initialize(self) -> None:
        if self._initialized:
            return
        if pygame is None:
            raise RuntimeError("pygame is not installed. Install it with 'pip install pygame'.")

        with self._lock:
            if self._initialized:
                return
            buffer_size = int(self._config.get("pygame_buffer", 256))
            frequency = int(self._config.get("pygame_frequency", 44100))
            channels = int(self._config.get("pygame_channels", 2))
            pygame.mixer.pre_init(frequency=frequency, size=-16, channels=channels, buffer=buffer_size)
            pygame.mixer.init()
            self._preload_configured_sounds()
            self._initialized = True

    def shutdown(self) -> None:
        if pygame is not None and self._initialized:
            pygame.mixer.quit()
            self._initialized = False

    def play(self, sound_path: Optional[str], *, cooldown: Optional[float] = None) -> bool:
        if not sound_path:
            return False

        self.initialize()
        resolved_path = resolve_sound_path(sound_path)
        if not os.path.isfile(resolved_path):
            print(f"Sound file not found: {resolved_path}")
            return False

        now = time.monotonic()
        effective_cooldown = cooldown if cooldown is not None else self._debounce_seconds

        with self._lock:
            if now - self._last_played_at < effective_cooldown:
                return False
            if self._channel is not None and self._channel.get_busy():
                return False

            sound = self._load_sound(resolved_path)
            self._channel = sound.play()
            self._last_played_at = now
            return self._channel is not None

    def play_ready(self) -> bool:
        ready_sound = self._config.get("ready_sound")
        return self.play(ready_sound, cooldown=0.0)

    def play_for_key(self, key_name: str) -> bool:
        bindings = self._config.get("bindings") or self._config.get("sounds", {}) or {}
        if not isinstance(bindings, dict):
            return False

        normalized_key = normalize_key_name(key_name)
        sound_path = None
        for candidate in (normalized_key, key_name, key_name.lower(), normalized_key.lower() if normalized_key else None):
            if candidate and candidate in bindings:
                sound_path = bindings[candidate]
                break
        if sound_path is None:
            sound_path = bindings.get("default")
        return self.play(sound_path)

    def _load_sound(self, resolved_path: str) -> Any:
        sound = self._sounds.get(resolved_path)
        if sound is None:
            sound = pygame.mixer.Sound(resolved_path)
            self._sounds[resolved_path] = sound
        return sound

    def _preload_configured_sounds(self) -> None:
        for sound_path in _configured_sound_paths(self._config):
            resolved_path = resolve_sound_path(sound_path)
            if os.path.isfile(resolved_path):
                self._load_sound(resolved_path)


class MultiHidSfxBoxService:
    def __init__(
        self,
        config: Dict[str, Any],
        *,
        debug: bool,
        device_paths: Optional[Iterable[str]],
        dry_run: bool,
    ) -> None:
        self._config = config
        self._debug = debug or bool(config.get("debug", False))
        self._device_paths = list(device_paths or config.get("devices") or [])
        self._dry_run = dry_run
        self._sound_controller = PygameSoundController(
            config,
            debounce_seconds=float(config.get("debounce_seconds", 0.25)),
        )
        self._last_key_times: dict[tuple[str, str], float] = {}

    def run(self) -> None:
        print("Starting sfxbox...")
        self._sound_controller.initialize()
        self._sound_controller.play_ready()

        if self._dry_run:
            print("Dry run enabled; waiting for Ctrl+C.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("Exiting.")
            finally:
                self._sound_controller.shutdown()
            return

        if evdev is None:
            raise RuntimeError("evdev is not installed. Install it with 'pip install evdev'.")

        devices = self._open_devices()
        if not devices:
            raise RuntimeError("No keyboard-style input devices were found.")

        device_names = ", ".join(str(device) for device in devices)
        print(f"Listening for keyboard input on {device_names}")
        try:
            while True:
                readable, _, _ = select.select(devices, [], [])
                for device in readable:
                    self._read_ready_device(device)
        except KeyboardInterrupt:
            print("Keyboard interrupt received; shutting down.")
        finally:
            for device in devices:
                device.close()
            self._sound_controller.shutdown()

    def _read_ready_device(self, device: Any) -> None:
        for event in device.read():
            if event.type != ecodes.EV_KEY or event.value != 1:
                continue

            key_name = self._get_key_name(event.code)
            if not key_name:
                continue

            device_id = getattr(device, "path", str(device))
            if self._debug:
                print(f"Key pressed on {device_id}: {key_name}")

            if not self._should_process_key(device_id, key_name):
                continue

            self._sound_controller.play_for_key(key_name)

    def _open_devices(self) -> list[Any]:
        if self._device_paths:
            return [evdev.InputDevice(device_path) for device_path in self._device_paths]

        devices = []
        for device_path in evdev.list_devices():
            try:
                device = evdev.InputDevice(device_path)
            except OSError:
                continue
            try:
                capabilities = device.capabilities()
            except OSError:
                device.close()
                continue
            if ecodes.EV_KEY in capabilities:
                devices.append(device)
            else:
                device.close()
        return devices

    def _should_process_key(self, device_id: str, key_name: str) -> bool:
        now = time.monotonic()
        key = (device_id, key_name)
        previous_time = self._last_key_times.get(key)
        if previous_time is not None and now - previous_time < self._sound_controller._debounce_seconds:
            return False
        self._last_key_times[key] = now
        return True

    @staticmethod
    def _get_key_name(code: int) -> Optional[str]:
        if ecodes is None:
            return None
        name = ecodes.KEY.get(code)
        if not name:
            return None
        return normalize_key_name(name)


def _configured_sound_paths(config: Dict[str, Any]) -> list[str]:
    paths = []
    ready_sound = config.get("ready_sound")
    if isinstance(ready_sound, str):
        paths.append(ready_sound)
    for section_name in ("bindings", "sounds"):
        section = config.get(section_name)
        if isinstance(section, dict):
            paths.extend(value for value in section.values() if isinstance(value, str))
    return paths


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the low-latency multi-HID SFX box service")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the YAML config file")
    parser.add_argument(
        "--device",
        action="append",
        default=None,
        help="Input device path. Repeat this option to listen to multiple HID devices.",
    )
    parser.add_argument("--debug", action="store_true", help="Print incoming key presses")
    parser.add_argument("--dry-run", action="store_true", help="Start without opening HID devices")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        service = MultiHidSfxBoxService(
            config=config,
            debug=args.debug,
            device_paths=args.device,
            dry_run=args.dry_run,
        )
        service.run()
    except (ConfigError, Exception) as exc:  # pragma: no cover - top-level guardprint
        print(f"sfxbox failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
