# sfxbox
Simple Python app for turning a Raspberry Pi into a sound effects box for theater shows.

## What it does
- listens for keyboard-style input from a wireless HID that registers as a keyboard
- optionally listens for a 15-button Elgato Stream Deck
- prints pressed keys when debugging is enabled
- loads sound mappings from a YAML config file
- plays a ready sound when the service starts
- plays a per-key sound without interrupting another sound that is already playing
- ignores rapid repeat presses with a short debounce window

## Files
- [app.py](app.py) contains the low-latency pygame/multi-HID service logic
- [main.py](main.py) contains the original single-HID/aplay service kept for reference
- [config.yaml](config.yaml) defines the ready sound and per-key sound mappings
- [tests/test_main.py](tests/test_main.py) covers config parsing and debounce behavior

## Setup on Raspberry Pi
1. Install dependencies:
   - `sudo apt update`
   - `sudo apt install -y python3-pip`
   - `python3 -m pip install --user evdev pygame pyyaml`
   - Optional Stream Deck support: `python3 -m pip install --user streamdeck`
2. Place your WAV files in the sounds directory (or update the paths in [config.yaml](config.yaml)).
3. Run the service:
   - `python3 app.py`
4. Optional: run in debug mode to print each key press:
   - `python3 app.py --debug`
5. Optional: listen to specific HID devices instead of every keyboard-style input:
   - `python3 app.py --device /dev/input/event1 --device /dev/input/event2`
6. Optional: listen to a connected 15-button Stream Deck:
   - `python3 app.py --streamdeck`
   - Stream Deck buttons are bound as `STREAMDECK_1` through `STREAMDECK_15` in [config.yaml](config.yaml).
   - To use only the Stream Deck and skip keyboard-style HID devices: `python3 app.py --streamdeck --no-hid`

## Notes
- [app.py](app.py) preloads configured sounds into `pygame.mixer` so key presses can trigger playback without spawning `aplay`.
- If no `--device` is provided, [app.py](app.py) listens to every input device with key events. Use repeated `--device` flags when the Pi also has keyboards you do not want to trigger sounds.
- Stream Deck support uses `python-elgato-streamdeck` through the `streamdeck` Python package. The `streamdeck-ui` app is useful for setup/testing, but only one process should access the deck at a time.
- If a key has no matching sound in the YAML file, the service falls back to the `default` sound.
- A sample systemd unit is included in [sfxbox.service](sfxbox.service) if you want the service to start automatically on boot. Change its `ExecStart` to `app.py` to use the pygame/multi-HID runtime.
