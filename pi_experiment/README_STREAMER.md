# SPEECH_to_EMOTION — Pi Streamer (self-contained)

This folder is a **self-contained snapshot** of the speech-to-emotion streamer,
intended to run headlessly on a Raspberry Pi (4 / 5) and emit OSC messages
over the local network. A separate "receiver" machine consumes those messages
(via the Node.js bridge in `receiver/`, or any OSC-capable client).

Everything needed to run is inside this directory. You can copy it to a USB
key and give it to someone with a Pi.

---

## 1. Hardware

- Raspberry Pi 4 (≥ 4 GB recommended) or Pi 5 (any RAM size; 4 GB+ comfortable)
- USB microphone (this folder assumes a device named `HK-MIC1` in `config.yaml` —
  change after running `--list-devices` if yours differs)
- Network: Pi must reach the receiver machine over LAN

## 2. First-time setup on the Pi

```bash
cd pi_experiment
bash setup_pi.sh
```

This installs system dependencies (portaudio, libsndfile, ffmpeg, etc.)
and creates a Python virtual environment at `./venv` with all Python deps.

Re-runnable safely.

## 3. Identify your microphone

```bash
source venv/bin/activate
python audio_analysis_background.py --list-devices
```

Note the index or name. Then either edit `config.yaml`:

```yaml
audio_device: HK-MIC1 # or e.g. 'USB PnP Sound Device', or an integer index
```

…or pass it on the command line (next step).

## 4. Run the streamer

Default (uses `config.yaml`, emits OSC to `127.0.0.1:9000`):

```bash
python audio_analysis_background.py
```

Stream to a receiver on the LAN:

```bash
python audio_analysis_background.py --osc-ip 192.168.1.49 --osc-port 9000
```

Override the mic:

```bash
python audio_analysis_background.py --device 2
python audio_analysis_background.py --device "USB PnP"
```

The streamer is headless: it runs until you press `Ctrl+C` (or kill the
process). For background / autostart, see §6.

## 5. Test the receiver (optional, any machine)

The `receiver/` folder contains a Node.js OSC→WebSocket bridge and a
p5.js page. On the receiver machine:

```bash
cd receiver
npm install            # first time only
node bridge.js         # starts the bridge
# then open index.html in a browser
```

## 6. Keep the streamer running after logout (later)

For production: wrap the streamer in a `systemd` service or run inside
`tmux` so it survives SSH disconnect. Not required for initial testing.

## 7. Files in this folder

| File                           | Purpose                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| `audio_analysis_background.py` | Headless entry point. Wraps `strip_monitor.py` with `--no-display --osc-autostart`.  |
| `strip_monitor.py`             | Real-time engine: capture → VAD → prosody (openSMILE) → emotion (emotion2vec) → OSC. |
| `config.yaml`                  | All runtime parameters (sample rate, windows, OSC, model, etc.). CLI flags override. |
| `src/emotion_model.py`         | emotion2vec wrapper via FunASR.                                                      |
| `src/*.py`                     | Companion modules (VAD, prosody, audio capture, MIDI/CSV writers).                   |
| `requirements-pi.txt`          | Python deps, trimmed for headless Pi (no matplotlib).                                |
| `setup_pi.sh`                  | One-shot system+Python install.                                                      |
| `receiver/`                    | Optional OSC→WebSocket bridge + p5.js page for visual confirmation.                  |
| `README.md`                    | Living porting log (what we tried, what failed).                                     |
| `README_STREAMER.md`           | This file.                                                                           |
