import os
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
