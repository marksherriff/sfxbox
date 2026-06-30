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


class AppModuleTests(unittest.TestCase):
    def test_parser_accepts_multiple_devices(self) -> None:
        args = app.parse_args(["--device", "/dev/input/event1", "--device", "/dev/input/event2"])

        self.assertEqual(args.device, ["/dev/input/event1", "/dev/input/event2"])

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


if __name__ == "__main__":
    unittest.main()
