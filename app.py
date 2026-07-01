#!/usr/bin/env python3
"""Low-latency, multi-HID sound effects box for Raspberry Pi Linux."""

from __future__ import annotations

import argparse
import errno
import os
import queue
import select
import sys
import threading
import time
from pathlib import Path
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

try:
    from StreamDeck.DeviceManager import DeviceManager  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for runtime
    DeviceManager = None

try:
    from PIL import Image  # type: ignore[import-not-found]
    from StreamDeck.ImageHelpers import PILHelper  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency for runtime
    Image = None
    PILHelper = None


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
        streamdeck_enabled: Optional[bool] = None,
        hid_enabled: Optional[bool] = None,
    ) -> None:
        self._config = config
        self._debug = debug or bool(config.get("debug", False))
        self._device_paths = list(device_paths or config.get("devices") or [])
        self._dry_run = dry_run
        self._streamdeck_config = _normalize_streamdeck_config(config)
        if streamdeck_enabled is not None:
            self._streamdeck_config["enabled"] = streamdeck_enabled
        self._hid_enabled = bool(config.get("hid_enabled", True)) if hid_enabled is None else hid_enabled
        self._sound_controller = PygameSoundController(
            config,
            debounce_seconds=float(config.get("debounce_seconds", 0.25)),
        )
        self._ignored_hid_device_ids: set[str] = set()
        self._queued_keys: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._key_lock = threading.Lock()
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

        streamdeck_listener = self._open_streamdeck_listener()
        if streamdeck_listener is not None:
            self._ignored_hid_device_ids = set(streamdeck_listener.device_ids)
        devices = self._open_devices() if self._hid_enabled else []
        if not devices and streamdeck_listener is None:
            raise RuntimeError("No keyboard-style input devices or Stream Decks were found.")

        if devices:
            device_names = ", ".join(str(device) for device in devices)
            print(f"Listening for keyboard input on {device_names}")
        if streamdeck_listener is not None:
            print(f"Listening for Stream Deck input on {streamdeck_listener.device_names}")
        try:
            while True:
                self._drain_queued_keys()
                if devices:
                    try:
                        timeout = 0.05 if streamdeck_listener is not None else 0.5
                        readable, _, _ = select.select(devices, [], [], timeout)
                    except OSError as exc:
                        if exc.errno != errno.ENODEV:
                            raise
                        devices = self._remove_disconnected_devices(devices, close_all_if_unknown=True)
                        continue
                    for device in readable:
                        try:
                            self._read_ready_device(device)
                        except OSError as exc:
                            if exc.errno != errno.ENODEV:
                                raise
                            devices = self._remove_device(devices, device)
                else:
                    time.sleep(0.05)
        except KeyboardInterrupt:
            print("Keyboard interrupt received; shutting down.")
        finally:
            if streamdeck_listener is not None:
                streamdeck_listener.close()
            for device in devices:
                _close_device(device)
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

            self._handle_key(device_id, key_name)

    def _open_devices(self) -> list[Any]:
        if evdev is None:
            raise RuntimeError("evdev is not installed. Install it with 'pip install evdev'.")

        if self._device_paths:
            return [evdev.InputDevice(device_path) for device_path in self._device_paths]

        devices = []
        for device_path in evdev.list_devices():
            try:
                device = evdev.InputDevice(device_path)
            except OSError:
                continue
            if self._streamdeck_config["enabled"] and _is_ignored_streamdeck_evdev_device(
                device,
                self._ignored_hid_device_ids,
            ):
                _close_device(device)
                continue
            try:
                capabilities = device.capabilities()
            except OSError:
                _close_device(device)
                continue
            if ecodes.EV_KEY in capabilities:
                devices.append(device)
            else:
                _close_device(device)
        return devices

    def _open_streamdeck_listener(self) -> Optional["StreamDeckButtonListener"]:
        if not self._streamdeck_config["enabled"]:
            return None
        if DeviceManager is None:
            raise RuntimeError(
                "python-elgato-streamdeck is not installed. "
                "Install it with 'pip install streamdeck'."
            )

        listener = StreamDeckButtonListener(
            on_key=self._queue_key,
            debug=False,
            brightness=self._streamdeck_config["brightness"],
            only_15_key=self._streamdeck_config["only_15_key"],
            reset_on_exit=self._streamdeck_config["reset_on_exit"],
            button_images=self._streamdeck_config["images"],
            manager=DeviceManager(),
        )
        listener.open()
        if not listener.device_names:
            listener.close()
            return None
        return listener

    def _handle_key(self, device_id: str, key_name: str) -> None:
        if not self._should_process_key(device_id, key_name):
            return
        self._sound_controller.play_for_key(key_name)

    def _queue_key(self, device_id: str, key_name: str) -> None:
        self._queued_keys.put((device_id, key_name))

    def _drain_queued_keys(self) -> None:
        while True:
            try:
                device_id, key_name = self._queued_keys.get_nowait()
            except queue.Empty:
                return
            if self._debug:
                print(f"Key pressed on {device_id}: {key_name}")
            self._handle_key(device_id, key_name)

    def _should_process_key(self, device_id: str, key_name: str) -> bool:
        now = time.monotonic()
        key = (device_id, key_name)
        with self._key_lock:
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
        if name == "KEY_UNKNOWN":
            return None
        return normalize_key_name(name)

    def _remove_disconnected_devices(self, devices: list[Any], *, close_all_if_unknown: bool = False) -> list[Any]:
        remaining = []
        for device in devices:
            try:
                select.select([device], [], [], 0)
            except OSError:
                _close_device(device)
            else:
                remaining.append(device)
        if len(remaining) == len(devices) and close_all_if_unknown:
            for device in remaining:
                _close_device(device)
            return []
        return remaining

    @staticmethod
    def _remove_device(devices: list[Any], disconnected_device: Any) -> list[Any]:
        remaining = []
        for device in devices:
            if device is disconnected_device:
                _close_device(device)
            else:
                remaining.append(device)
        return remaining


class StreamDeckButtonListener:
    """Turns Stream Deck button presses into sfxbox key names."""

    def __init__(
        self,
        *,
        on_key: Any,
        debug: bool,
        brightness: Optional[int],
        only_15_key: bool,
        reset_on_exit: bool,
        button_images: dict[str, str],
        manager: Any,
    ) -> None:
        self._on_key = on_key
        self._debug = debug
        self._brightness = brightness
        self._only_15_key = only_15_key
        self._reset_on_exit = reset_on_exit
        self._button_images = button_images
        self._manager = manager
        self._decks: list[Any] = []
        self._deck_descriptions: dict[int, str] = {}

    @property
    def device_names(self) -> str:
        return ", ".join(self._deck_descriptions.get(id(deck), str(deck)) for deck in self._decks)

    @property
    def device_ids(self) -> list[str]:
        return [self._deck_descriptions.get(id(deck), str(deck)) for deck in self._decks]

    def open(self) -> None:
        for deck in self._manager.enumerate():
            if self._only_15_key and deck.key_count() != 15:
                continue
            try:
                deck.open()
                deck_description = self._describe_deck(deck)
                deck.reset()
                if self._brightness is not None:
                    deck.set_brightness(self._brightness)
                self._set_configured_key_images(deck)
                deck.set_key_callback(self._handle_streamdeck_key)
            except OSError as exc:
                if getattr(exc, "errno", None) != errno.ENODEV:
                    raise
                try:
                    deck.close()
                except OSError:
                    pass
                continue
            else:
                self._decks.append(deck)
                self._deck_descriptions[id(deck)] = deck_description

    def close(self) -> None:
        for deck in self._decks:
            try:
                if self._reset_on_exit:
                    deck.reset()
                deck.close()
            except OSError as exc:
                if getattr(exc, "errno", None) != errno.ENODEV:
                    raise
        self._decks = []
        self._deck_descriptions = {}

    def _handle_streamdeck_key(self, deck: Any, key: int, state: bool) -> None:
        if not state:
            return
        try:
            key_name = f"STREAMDECK_{key + 1}"
            device_id = f"streamdeck:{self._deck_descriptions.get(id(deck), str(deck))}"
            if self._debug:
                print(f"Key pressed on {device_id}: {key_name}")
            self._on_key(device_id, key_name)
        except Exception as exc:  # pragma: no cover - defensive callback guard
            print(f"Stream Deck callback failed: {exc}", file=sys.stderr)

    @staticmethod
    def _describe_deck(deck: Any) -> str:
        for attr_name in ("get_serial_number", "deck_type"):
            try:
                value = getattr(deck, attr_name)()
            except (AttributeError, OSError):
                continue
            if value:
                return str(value)
        return str(deck)

    def _set_configured_key_images(self, deck: Any) -> None:
        if not self._button_images:
            return
        if Image is None or PILHelper is None:
            raise RuntimeError("Stream Deck button images require Pillow. Install it with 'pip install Pillow'.")

        for key_name, image_path in self._button_images.items():
            key_index = _streamdeck_key_index(key_name, deck.key_count())
            if key_index is None:
                print(f"Ignoring invalid Stream Deck image key: {key_name}", file=sys.stderr)
                continue

            resolved_path = resolve_asset_path(image_path, default_subdir="images")
            if not os.path.isfile(resolved_path):
                print(f"Stream Deck image file not found: {resolved_path}", file=sys.stderr)
                continue

            with Image.open(resolved_path) as image:
                key_image = PILHelper.create_scaled_image(deck, image.convert("RGB"), margins=[0, 0, 0, 0])
                deck.set_key_image(key_index, PILHelper.to_native_format(deck, key_image))


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


def _is_ignored_streamdeck_evdev_device(device: Any, ignored_device_ids: set[str]) -> bool:
    device_text = " ".join(
        str(value)
        for value in (
            getattr(device, "name", ""),
            getattr(device, "path", ""),
            getattr(device, "phys", ""),
            getattr(device, "uniq", ""),
        )
        if value
    )
    normalized_device_text = device_text.lower()
    if "stream deck" in normalized_device_text or "streamdeck" in normalized_device_text:
        return True
    return any(device_id.lower() in normalized_device_text for device_id in ignored_device_ids if device_id)


def _streamdeck_key_index(key_name: str, key_count: int) -> Optional[int]:
    normalized_key = normalize_key_name(key_name)
    if normalized_key.startswith("STREAMDECK_"):
        raw_index = normalized_key.removeprefix("STREAMDECK_")
    else:
        raw_index = normalized_key
    if not raw_index.isdigit():
        return None
    key_index = int(raw_index) - 1
    if key_index < 0 or key_index >= key_count:
        return None
    return key_index


def resolve_asset_path(asset_path: str, *, default_subdir: str) -> str:
    expanded_path = os.path.expanduser(str(asset_path))
    if os.path.isabs(expanded_path):
        return expanded_path

    base_dir = DEFAULT_CONFIG_PATH.parent
    candidates = [
        base_dir / expanded_path,
        Path.cwd() / expanded_path,
        base_dir / default_subdir / os.path.basename(expanded_path),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return str(candidate)
    return str(base_dir / expanded_path)


def _close_device(device: Any) -> None:
    try:
        device.close()
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.ENODEV:
            raise


def _normalize_streamdeck_config(config: Dict[str, Any]) -> dict[str, Any]:
    raw_config = config.get("streamdeck", False)
    if isinstance(raw_config, bool):
        streamdeck_config: dict[str, Any] = {"enabled": raw_config}
    elif isinstance(raw_config, dict):
        streamdeck_config = dict(raw_config)
        streamdeck_config["enabled"] = bool(streamdeck_config.get("enabled", True))
    else:
        streamdeck_config = {"enabled": False}

    brightness = streamdeck_config.get("brightness")
    if brightness is not None:
        brightness = max(0, min(100, int(brightness)))

    return {
        "enabled": streamdeck_config["enabled"],
        "brightness": brightness,
        "only_15_key": bool(streamdeck_config.get("only_15_key", True)),
        "reset_on_exit": bool(streamdeck_config.get("reset_on_exit", True)),
        "images": _normalize_streamdeck_images(streamdeck_config.get("images", {})),
    }


def _normalize_streamdeck_images(raw_images: Any) -> dict[str, str]:
    if not isinstance(raw_images, dict):
        return {}
    return {normalize_key_name(str(key)): str(value) for key, value in raw_images.items() if isinstance(value, str)}


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
    parser.add_argument("--dry-run", action="store_true", help="Start without opening input devices")
    parser.add_argument(
        "--streamdeck",
        action="store_true",
        help="Listen to connected 15-button Stream Deck devices",
    )
    parser.add_argument(
        "--no-hid",
        action="store_true",
        help="Do not listen to keyboard-style evdev input devices",
    )
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
            streamdeck_enabled=args.streamdeck or None,
            hid_enabled=False if args.no_hid else None,
        )
        service.run()
    except (ConfigError, Exception) as exc:  # pragma: no cover - top-level guardprint
        print(f"sfxbox failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
