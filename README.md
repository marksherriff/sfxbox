# sfxbox
Simple Python app for turning a Raspberry Pi into a sound effects box for theater shows.

## What it does
- listens for keyboard-style input from a wireless HID that registers as a keyboard
- prints pressed keys when debugging is enabled
- loads sound mappings from a YAML config file
- plays a ready sound when the service starts
- plays a per-key sound without interrupting another sound that is already playing
- ignores rapid repeat presses with a short debounce window

## Files
- [main.py](main.py) contains the service logic
- [config.yaml](config.yaml) defines the ready sound and per-key sound mappings
- [tests/test_main.py](tests/test_main.py) covers config parsing and debounce behavior

## Setup on Raspberry Pi
1. Install dependencies:
   - `sudo apt update`
   - `sudo apt install -y alsa-utils python3-pip`
   - `python3 -m pip install --user evdev pyyaml`
2. Place your WAV files in the sounds directory (or update the paths in [config.yaml](config.yaml)).
3. Run the service:
   - `python3 main.py`
4. Optional: run in debug mode to print each key press:
   - `python3 main.py --debug`

## Notes
- The default config expects sounds under `/home/bbplayers/sfxbox/sounds`.
- If a key has no matching sound in the YAML file, the service falls back to the `default` sound.
- A sample systemd unit is included in [sfxbox.service](sfxbox.service) if you want the service to start automatically on boot.
