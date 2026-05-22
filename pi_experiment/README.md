# Raspberry Pi Porting — SPEECH_to_EMOTION

Living procedure document for porting the pipeline to a Raspberry Pi 5.
Append as we go. Do not retroactively tidy — corrections live as new dated
entries, so the trail of _what we tried and what failed_ survives. That trail
is itself ethnographic data.

---

## Hardware & target

- **Board**: Raspberry Pi 5, 16 GB RAM
- **OS**: Raspberry Pi OS (assumed 64-bit Bookworm — to confirm on first SSH)
- **Display**: none (headless from the Mac)
- **Network**: Pi must be reachable over local Wi-Fi or Ethernet from the Mac
- **Audio**: USB mic (model TBD — confirm `arecord -l` once on the Pi)

## Goal for day 1

Decide on first commit. Candidates:

- **Minimal target**: `audio_capture.py` + `prosody.py` (F0, energy, VAD)
  - OSC out to the Mac running `radar_dashboard.py` / `strip_monitor.py`.
    Defers the heavy `emotion2vec` model entirely.
- **Full target**: the whole pipeline including `emotion_model.py`. Risk:
  `funasr` / `emotion2vec` model loading on Pi memory and CPU is unknown
  territory; may need a smaller checkpoint or to keep emotion off-device.

Recommendation: minimal target first. Get the audio path and the OSC bridge
working end-to-end on the Pi today. Emotion model becomes a separate
investigation tomorrow.

---

## Phase 00 — Flashing the OS, fully headless (no monitor, no keyboard)

_Added 17 May 2026 — the original Pi was replaced with a fresh board, no OS
installed, no peripherals available. This section walks the full path from
a blank microSD card to a Pi reachable over SSH from the Mac, without ever
plugging in a monitor or keyboard._

### 00.0 What you need

- The new Raspberry Pi 5 (16 GB).
- A microSD card, **at least 16 GB**, ideally 32 GB or 64 GB, class A1/A2.
  (For a Pi 5 with a heavy software stack, 64 GB is comfortable.)
- A microSD reader for the Mac (USB-C or USB-A, depending on the Mac).
- The official Pi 5 power supply (**27 W USB-C PD**). Underpowered supplies
  on a Pi 5 produce throttling that looks like software bugs — do not
  improvise here.
- An Ethernet cable **OR** the Wi-Fi credentials of the network the Pi will
  join. Ethernet is the most reliable for a first headless boot; Wi-Fi
  works but adds one failure mode.
- The Mac on the **same local network** as the Pi will be on. If the Pi
  will join Wi-Fi network `X`, the Mac must also be on `X` (or on a wired
  segment that reaches `X`).

### 00.1 Install Raspberry Pi Imager on the Mac

Download from the official site:

- <https://www.raspberrypi.com/software/>

Install. It's a normal `.dmg`. Open it after install.

### 00.2 Configure the image — the headless-critical step

Insert the microSD card into the Mac via the reader. In Pi Imager:

1. **Choose Device** → `Raspberry Pi 5`.
2. **Choose OS** → `Raspberry Pi OS (64-bit)`. Use the standard one with
   desktop, not "Lite" — the Lite image has no desktop but also no VNC
   server pre-installed, which makes the optional later VNC route harder.
   Disk footprint is not a concern on 32 GB+.
3. **Choose Storage** → your microSD card. **Confirm twice this is the
   right disk** — Imager will erase it. If your Mac has any external drives
   mounted, eject them first to remove ambiguity.
4. **Next** → it will ask: _"Would you like to apply OS customisation
   settings?"_ → **EDIT SETTINGS**. This is the step that makes the headless
   boot work. Do not skip it.

In the customisation dialog, **General** tab:

- **Set hostname**: choose something memorable. I suggest `emotionpi`
  (so the Pi is reachable later as `emotionpi.local`). Record it here:
  `HOSTNAME = ___________`.
- **Set username and password**: pick a username and a strong password.
  Default `pi` is no longer assumed by the OS — pick whatever you like.
  Record it: `USER = ___________`. Password: write it down somewhere safe;
  you will need it for `sudo` later even after key auth is set up.
- **Configure wireless LAN** (skip if using Ethernet):
  - SSID: your Wi-Fi network name (exact case, including spaces).
  - Password: your Wi-Fi password.
  - **Wireless LAN country**: this matters more than it looks — wrong
    country can disable the Wi-Fi radio entirely on some channels. Pick
    yours (`HK` if Hong Kong, etc.).
- **Set locale settings**:
  - Time zone: e.g. `Asia/Hong_Kong`.
  - Keyboard layout: doesn't matter for headless, but set it correctly
    in case you ever plug in a keyboard.

In the customisation dialog, **Services** tab:

- **Enable SSH**: ✅ check it. Choose **"Use password authentication"** for
  now — we will switch to key auth after first connection (§0.4 below).
  (You can also pre-load a public key here if you already have one; if you
  do, paste your `~/.ssh/id_ed25519.pub` contents.)

In the customisation dialog, **Options** tab:

- "Play sound when finished" — your call.
- "Eject media when finished" — ✅ leave on, it's tidier.
- "Enable telemetry" — your call.

Click **SAVE**. Back at the previous dialog, click **YES** to apply OS
customisation, then **YES** to erase the SD card. Authenticate to macOS
when it asks for your password (Imager needs admin rights to write to the
SD card).

Writing + verification typically takes **8–15 minutes** depending on the
card and reader. When finished, Imager will eject the card. You can pull
it out.

### 00.3 First boot — patient

Insert the microSD into the Pi (the slot is on the underside, the gold
contacts face up toward the board). If using Ethernet, plug the cable in
now. Plug in the 27 W USB-C power supply.

The Pi will boot. On a **first boot of a freshly imaged card**, the Pi
does several things that take time and produce no external sign:

- Expands the root filesystem to fill the SD card.
- Applies the cloud-init-style customisations (hostname, user, Wi-Fi creds,
  SSH enable).
- Joins the network.
- Possibly reboots once.

**Wait 3–5 minutes before trying to connect.** Premature attempts produce
false-negative results that send you down debugging paths that aren't real.

Watch the LEDs:

- **Red** (PWR) — solid red means power is OK. If it blinks or is dim, the
  power supply is inadequate. Stop and use a proper 27 W PD supply.
- **Green** (ACT) — flickers when the SD card is being read. Heavy
  flickering during first boot is normal and good (the OS is expanding and
  configuring itself). When it settles to occasional brief flickers, first
  boot is done.

If you have a **wired Ethernet** connection to a switch/router with link
LEDs, the link LED on the router port should be solid; activity LED should
blink. This is the most reliable visual confirmation that the Pi is alive
on the network.

### 00.4 Find the Pi from the Mac

Try in this order, from a Mac terminal (not the heartbeat terminal):

```bash
# 1. mDNS — the cleanest path. Works if hostname was set in §00.2
#    and the network supports mDNS (most home networks do; some corporate
#    or guest networks block it).
ping -c 4 emotionpi.local       # substitute your chosen hostname
```

If that answers with an IP and round-trip times, you are done finding it.
Record the IP: `PI_IP = ___.___.___.___`.

If `ping` says `cannot resolve emotionpi.local`:

```bash
# 2. ARP scan — works regardless of mDNS, but requires the Pi to have
#    already talked on the network once. (It has, if it booted and got
#    a DHCP lease.)
arp -a | grep -iE 'dc:a6:32|d8:3a:dd|b8:27:eb|2c:cf:67'
```

The Pi 5 typically uses MAC prefix `d8:3a:dd` or `2c:cf:67`. The line will
look like `? (192.168.1.42) at d8:3a:dd:xx:xx:xx on en0 ifscope [ethernet]`.
The number in parentheses is the IP — record it.

If neither works:

```bash
# 3. Network scan — heavier. Requires nmap.
brew install nmap                              # one time
nmap -sn 192.168.1.0/24                        # adjust to your subnet
# Look for a host labelled "Raspberry Pi" or matching the Pi's MAC.
```

To find your own subnet on the Mac:

```bash
ipconfig getifaddr en0     # Wi-Fi (often)
ipconfig getifaddr en1     # Ethernet (often) — try both
# Subnet is then x.x.x.0/24 for that interface.
```

### 00.5 First SSH

```bash
ssh <USER>@emotionpi.local
# or
ssh <USER>@<PI_IP>
```

First connection: the Mac will warn it has never seen this host key. Type
`yes` to accept. Then enter the password you set in §00.2.

You should land at a prompt like:

```
<USER>@emotionpi:~ $
```

Congratulations. The Pi is now reachable. Everything downstream (key auth,
VS Code Remote-SSH, the actual project work) is incremental from here.

If SSH fails:

- `ssh: Could not resolve hostname emotionpi.local` → mDNS issue. Use the
  IP from `arp -a` instead.
- `Connection refused` → the Pi is reachable but `sshd` is not running.
  Wait another 60 s (first boot may still be finishing) and retry. If it
  persists, SSH was probably not enabled in §00.2 — re-image the card and
  recheck the Services tab.
- `Operation timed out` → network path is broken. Pi is on a different
  subnet, or firewall blocks port 22, or the Pi has not joined Wi-Fi.
  Check `arp -a` — if the Pi's MAC appears, the network is fine and the
  problem is firewall/subnet; if it does not appear, the Pi has not
  joined the network at all (most likely a Wi-Fi credential typo in §00.2).
- `Host key verification failed` → only happens on re-imaging. Clear with
  `ssh-keygen -R emotionpi.local` and `ssh-keygen -R <PI_IP>`, then retry.

### 00.6 Sanity check on the Pi

Once SSH'd in:

```bash
uname -a              # confirm aarch64, recent kernel
cat /etc/os-release   # confirm Bookworm
free -h               # confirm 16 GB visible
df -h /               # confirm SD card has space
hostname              # confirms your chosen hostname
```

Record results in the **Phase 1** section below (it expects this exact
inventory).

### 00.7 Recovery posture

Re-imaging is cheap. If anything goes wrong in §00.3–§00.5 that you can't
diagnose in 10 minutes:

1. Power off the Pi (unplug — there is no graceful shutdown available
   if you can't reach it).
2. Pull the microSD card.
3. Re-image from §00.2, double-checking the Services tab (SSH ✅) and the
   Wi-Fi credentials (typo-prone).

Three iterations from scratch is faster than one hour of debugging an
unreachable headless Pi.

---

## Phase 0 — Reach the Pi (headless setup)

### 0.1 Find the Pi on the network

From the Mac:

```bash
# If hostname was set during imaging (default is often `raspberrypi.local`)
ping raspberrypi.local

# If that fails, scan the local network
arp -a | grep -i b8:27:eb        # older Pi MAC prefix
arp -a | grep -i dc:a6:32        # Pi 4/5 MAC prefix
arp -a | grep -i d8:3a:dd        # newer Pi 5 MAC prefix
```

Record the IP here once known: `PI_IP = ___.___.___.___`

### 0.2 Enable SSH on the Pi (one-time)

If SSH was not enabled during imaging:

- On the Pi (with monitor + keyboard, one last time):
  `sudo raspi-config` → _Interface Options_ → _SSH_ → Enable.
- Or: `sudo systemctl enable --now ssh`.

Alternatively, if re-flashing: in Raspberry Pi Imager, click the gear icon
before writing, set hostname, enable SSH with password or public key, and
configure Wi-Fi there. Headless from the start.

### 0.3 First SSH from the Mac

```bash
ssh <pi-username>@raspberrypi.local
# or
ssh <pi-username>@<PI_IP>
```

Default username on a fresh Pi OS install is whatever you set during imaging
(no longer `pi` by default since 2022).

If host-key warnings appear later (e.g. after re-imaging), clear with:

```bash
ssh-keygen -R raspberrypi.local
ssh-keygen -R <PI_IP>
```

### 0.4 Set up SSH key auth (recommended)

```bash
# On the Mac
ssh-keygen -t ed25519 -C "mac-to-pi"      # if you don't already have a key
ssh-copy-id <pi-username>@<PI_IP>
```

Then edit `~/.ssh/config` on the Mac:

```
Host pi
    HostName <PI_IP or raspberrypi.local>
    User <pi-username>
    IdentityFile ~/.ssh/id_ed25519
```

Now `ssh pi` is enough.

### 0.5 Remote desktop (optional, for later)

Three options, in order of likely usefulness:

1. **VS Code Remote-SSH** — best for editing/running code on the Pi from the
   Mac. Install the _Remote - SSH_ extension on VS Code (Mac side), then
   _Connect to Host…_ → `pi`. The Pi runs a small VS Code server; you get
   the workspace as if local. **Recommended for our work.**
2. **VNC** — true desktop. `sudo raspi-config` → _Interface Options_ → _VNC_.
   On Pi OS Bookworm the default VNC server is `wayvnc` (Wayland) which is
   different from the older `RealVNC`; connect from the Mac with the
   _RealVNC Viewer_ app or `Screen Sharing.app` (vnc://<PI_IP>:5900).
3. **X11 forwarding** (`ssh -X`) — works for one-off GUI windows but is
   slow and finicky over Wi-Fi. Skip unless needed.

---

## Phase 1 — Inventory the Pi

Once SSH is working, run on the Pi:

```bash
uname -a                          # kernel + arch
cat /etc/os-release               # OS version (Bookworm? Bullseye?)
python3 --version
pip3 --version
arecord -l                        # list audio capture devices
free -h                           # confirm 16 GB visible
df -h                             # disk space
```

Record results here:

- Kernel:
- OS:
- Python:
- Audio devices:
- Free RAM:
- Free disk:

---

## Phase 2 — Get the code onto the Pi

Two options:

- **Git clone** (cleanest if the project has a remote):
  `git clone <repo-url> ~/SPEECH_to_EMOTION`
- **rsync from the Mac** (works without a remote):
  ```bash
  rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='archive' \
    ./ pi:~/SPEECH_to_EMOTION/
  ```

---

## Phase 3 — Python env on the Pi

Pi OS Bookworm enforces PEP 668 (externally-managed environment), so a venv
is mandatory:

```bash
cd ~/SPEECH_to_EMOTION
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
```

Then _minimal_ install first — do **not** try to install all of
`requirements.txt` in one shot. Start with what `audio_capture.py` and
`prosody.py` actually need (sounddevice, numpy, scipy, opensmile or
parselmouth, python-osc) and add the rest one module at a time.

---

## Phase 4 — Audio capture sanity check

Before running our code, confirm the OS sees the mic:

```bash
arecord -l                                   # device index
arecord -D plughw:1,0 -d 5 -f cd test.wav    # record 5s
aplay test.wav                               # if speakers / HDMI audio
```

Then test from Python:

```bash
python -c "import sounddevice as sd; print(sd.query_devices())"
```

---

## Phase 5 — Run the minimal pipeline

(Filled in once we're here.)

---

## Open questions to resolve as we go

- [ ] Will `opensmile` install via pip on aarch64? If not, build from source
      or use `parselmouth`.
- [ ] `emotion2vec` / `funasr` on aarch64 — wheels exist?
- [ ] Latency budget: capture → prosody → OSC, what is acceptable?
- [ ] Where does the dashboard run — on the Mac (Pi sends OSC over Wi-Fi),
      or also on the Pi (HDMI out to a screen)?
- [ ] Power: USB-C PD, official 27 W supply? Underpowered Pi 5 throttles
      in ways that look like software bugs.

---

## Log (append-only)

### 2026-05-15

- Created this folder. Heartbeat started in side terminal. About to begin
  Phase 0.
