import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app
import main


class MainModuleTests(unittest.TestCase):
    def test_simple_yaml_config_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.yaml"
            config_path.write_text(
                "ready_sound: /tmp/ready.wav\n"
                "debug: true\n"
                "sounds:\n"
                "  space: /tmp/space.wav\n",
                encoding="utf-8",
            )

            config = main.load_config(str(config_path))

            self.assertEqual(config["ready_sound"], "/tmp/ready.wav")
            self.assertTrue(config["debug"])
            self.assertEqual(config["sounds"]["space"], "/tmp/space.wav")

    def test_key_debounce_blocks_double_taps(self) -> None:
        service = main.SfxBoxService(
            config={
                "ready_sound": "/tmp/ready.wav",
                "debug": False,
                "sounds": {},
                "debounce_seconds": 0.25,
            },
            debug=False,
            device_path=None,
            dry_run=True,
        )

        self.assertTrue(service._should_process_key("space"))
        self.assertFalse(service._should_process_key("space"))

    def test_bindings_and_aplay_card_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.yaml"
            config_path.write_text(
                "ready_sound: sounds/ready.wav\n"
                "aplay_card: 0\n"
                "bindings:\n"
                "  KEY_PAGEDOWN: sounds/feed.wav\n"
                "  KEY_PAGEUP: sounds/feed.wav\n",
                encoding="utf-8",
            )

            config = main.load_config(str(config_path))
            self.assertEqual(config["bindings"]["KEY_PAGEDOWN"], "sounds/feed.wav")
            self.assertEqual(config["aplay_card"], 0)

            controller = main.SoundController(config, debounce_seconds=0.25)
            command = controller._build_aplay_command("/tmp/test.wav")
            self.assertIn("-D", command)
            self.assertEqual(command[command.index("-D") + 1], "hw:0,0")


class FakeChannel:
    def __init__(self) -> None:
        self.busy = False

    def get_busy(self) -> bool:
        return self.busy


class FakeSound:
    def __init__(self, path: str, channel: FakeChannel) -> None:
        self.path = path
        self.channel = channel
        self.play_count = 0

    def play(self) -> FakeChannel:
        self.play_count += 1
        return self.channel


class FakeMixer:
    def __init__(self) -> None:
        self.channel = FakeChannel()
        self.loaded: list[str] = []
        self.initialized = False

    def pre_init(self, **kwargs) -> None:
        self.pre_init_kwargs = kwargs

    def init(self) -> None:
        self.initialized = True

    def quit(self) -> None:
        self.initialized = False

    def Sound(self, path: str) -> FakeSound:
        self.loaded.append(path)
        return FakeSound(path, self.channel)


class FakeEcodes:
    EV_KEY = 1
    KEY = {
        30: "KEY_A",
        240: "KEY_UNKNOWN",
    }


class FakeInputDevice:
    def __init__(self, path: str, name: str, uniq: str = "") -> None:
        self.path = path
        self.name = name
        self.uniq = uniq
        self.closed = False

    def capabilities(self):
        return {FakeEcodes.EV_KEY: [30]}

    def close(self) -> None:
        self.closed = True


class AppModuleTests(unittest.TestCase):
    def test_parser_accepts_multiple_devices(self) -> None:
        args = app.parse_args(["--device", "/dev/input/event1", "--device", "/dev/input/event2"])

        self.assertEqual(args.device, ["/dev/input/event1", "/dev/input/event2"])

    def test_parser_accepts_streamdeck_flags(self) -> None:
        args = app.parse_args(["--streamdeck", "--no-hid"])

        self.assertTrue(args.streamdeck)
        self.assertTrue(args.no_hid)

    def test_pygame_controller_preloads_and_avoids_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            sound_path = Path(tmp_dir) / "beep.wav"
            sound_path.write_bytes(b"not a real wav because pygame is faked")
            mixer = FakeMixer()
            fake_pygame = mock.Mock()
            fake_pygame.mixer = mixer

            with mock.patch.object(app, "pygame", fake_pygame):
                controller = app.PygameSoundController(
                    {
                        "ready_sound": str(sound_path),
                        "bindings": {"KEY_A": str(sound_path)},
                        "sounds": {"default": str(sound_path)},
                    },
                    debounce_seconds=0.25,
                )

                self.assertTrue(controller.play_ready())
                self.assertTrue(mixer.initialized)
                self.assertEqual(mixer.loaded, [str(sound_path)])

                mixer.channel.busy = True
                self.assertFalse(controller.play_for_key("KEY_A"))

    def test_multi_hid_debounce_is_per_device(self) -> None:
        service = app.MultiHidSfxBoxService(
            config={
                "ready_sound": "/tmp/ready.wav",
                "debug": False,
                "sounds": {},
                "debounce_seconds": 0.25,
            },
            debug=False,
            device_paths=["/dev/input/event1", "/dev/input/event2"],
            dry_run=True,
        )

        self.assertTrue(service._should_process_key("/dev/input/event1", "KEY_A"))
        self.assertFalse(service._should_process_key("/dev/input/event1", "KEY_A"))
        self.assertTrue(service._should_process_key("/dev/input/event2", "KEY_A"))

    def test_unknown_evdev_key_is_ignored(self) -> None:
        with mock.patch.object(app, "ecodes", FakeEcodes):
            self.assertIsNone(app.MultiHidSfxBoxService._get_key_name(240))
            self.assertEqual(app.MultiHidSfxBoxService._get_key_name(30), "KEY_A")

    def test_streamdeck_evdev_device_is_skipped_when_streamdeck_enabled(self) -> None:
        keyboard = FakeInputDevice("/dev/input/event1", "USB Keyboard")
        streamdeck = FakeInputDevice("/dev/input/event2", "hid-generic", "AL16J204153")
        fake_evdev = mock.Mock()
        fake_evdev.list_devices.return_value = [keyboard.path, streamdeck.path]
        fake_evdev.InputDevice.side_effect = {
            keyboard.path: keyboard,
            streamdeck.path: streamdeck,
        }.__getitem__
        service = app.MultiHidSfxBoxService(
            config={
                "ready_sound": "/tmp/ready.wav",
                "debug": False,
                "sounds": {},
                "debounce_seconds": 0.25,
                "streamdeck": {"enabled": True},
            },
            debug=False,
            device_paths=None,
            dry_run=True,
        )
        service._ignored_hid_device_ids = {"AL16J204153"}

        with mock.patch.object(app, "evdev", fake_evdev), mock.patch.object(app, "ecodes", FakeEcodes):
            devices = service._open_devices()

        self.assertEqual(devices, [keyboard])
        self.assertFalse(keyboard.closed)
        self.assertTrue(streamdeck.closed)

    def test_streamdeck_listener_maps_one_based_button_names(self) -> None:
        handled_keys = []
        deck = FakeStreamDeck(key_count=15, serial_number="ABC123")
        listener = app.StreamDeckButtonListener(
            on_key=lambda device_id, key_name: handled_keys.append((device_id, key_name)),
            debug=False,
            brightness=45,
            only_15_key=True,
            reset_on_exit=True,
            manager=FakeStreamDeckManager([deck, FakeStreamDeck(key_count=32)]),
        )

        listener.open()
        deck.press(0)
        deck.press(14)
        deck.release(14)
        listener.close()

        self.assertEqual(
            handled_keys,
            [
                ("streamdeck:ABC123", "STREAMDECK_1"),
                ("streamdeck:ABC123", "STREAMDECK_15"),
            ],
        )
        self.assertTrue(deck.opened)
        self.assertEqual(deck.brightness, 45)
        self.assertEqual(deck.reset_count, 2)

    def test_streamdeck_listener_skips_deck_that_disappears_during_setup(self) -> None:
        deck = FakeStreamDeck(key_count=15, serial_number="AL16J204153", reset_error=OSError(19, "No such device"))
        listener = app.StreamDeckButtonListener(
            on_key=lambda device_id, key_name: None,
            debug=False,
            brightness=45,
            only_15_key=True,
            reset_on_exit=True,
            manager=FakeStreamDeckManager([deck]),
        )

        listener.open()

        self.assertEqual(listener.device_ids, [])
        self.assertTrue(deck.closed)


class FakeStreamDeck:
    def __init__(self, *, key_count: int, serial_number: str = "", reset_error: OSError | None = None) -> None:
        self._key_count = key_count
        self._serial_number = serial_number
        self._reset_error = reset_error
        self.callback = None
        self.opened = False
        self.closed = False
        self.brightness = None
        self.reset_count = 0

    def key_count(self) -> int:
        return self._key_count

    def get_serial_number(self) -> str:
        return self._serial_number

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def reset(self) -> None:
        if self._reset_error is not None:
            raise self._reset_error
        self.reset_count += 1

    def set_brightness(self, brightness: int) -> None:
        self.brightness = brightness

    def set_key_callback(self, callback) -> None:
        self.callback = callback

    def press(self, key: int) -> None:
        self.callback(self, key, True)

    def release(self, key: int) -> None:
        self.callback(self, key, False)


class FakeStreamDeckManager:
    def __init__(self, decks) -> None:
        self._decks = decks

    def enumerate(self):
        return self._decks


if __name__ == "__main__":
    unittest.main()
