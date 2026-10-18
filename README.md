# hapi — Raspberry Pi voice satellite setup guide

This documents the existing install of the **hapi** Wyoming satellite: a Raspberry Pi 3B
with local wake word ("okay nabu") and a local brown-noise mixer (`nabumix`) that owns the
speaker. Use it to rebuild the device or recreate it on new hardware.

## Architecture

```
USB mic ──arecord──▶ wyoming-satellite (TCP server :10700) ◀── dials in ── Home Assistant
                          │                                              │
                          │ local wake word (okay_nabu)                  ├─ Whisper (STT)
                          ▼                                              ├─ Assist intents
                 wyoming-openwakeword (127.0.0.1:10400)                  └─ Piper (TTS)
                          │                                                     │
                          ▼                                                     ▼ TTS audio
              wake → satellite streams audio to HA ──────────▶ nabubridge.py ──▶ nabumix.py ──▶ DigiAMP+ HAT speaker
                                                                    ▲              │
        transcript hook: nabu-voice-ctl.py ──ctl.sock───────────────┴──────────────┘
                                                 (start/stop/level brown noise, ducks for TTS)
```

Key design points:

- The satellite is a **Wyoming TCP server** (`tcp://0.0.0.0:10700`); Home Assistant dials
  *in* via the "Wyoming Protocol" integration. The URI lives in the root-only file
  `/opt/wyoming/satellite.uri` so it never appears in unit files or `/proc/*/cmdline`.
- **Wake word runs on the Pi** (openWakeWord `okay_nabu`, local server on loopback:10400),
  so HA's openwakeword integration is disabled (`not_loaded`).
- **Brown noise is a local daemon**, not an HA add-on: `nabumix.py` is the *only* process
  allowed to write to the DigiAMP sound card. It generates filtered (brown) noise itself
  and ducks it (fast fade-down, slow fade-up) whenever satellite TTS audio arrives.
- Voice control of the noise works two ways:
  1. HA side: custom Assist sentences in `custom_sentences/en/nabu.yaml` (mapped onto the
     built-in `HassRespond` intent, which just speaks the response slot).
  2. Pi side: the satellite's `--transcript-command` hook (`nabu-voice-ctl.py`) regex-matches
     the transcript and sends `on`/`off`/`level` to the mixer's control socket.

## Hardware

| Part | Detail |
|---|---|
| Board | Raspberry Pi 3B, 1 GB RAM, Debian 13 (trixie), Python 3.13 |
| Speaker | Pi DIGIAMP+ 35 W HAT — ALSA card 0, mixer control `Digital`, default **75%** (≈ −26 dB; the pcm512x scale is steep and non-linear, pick by ear) |
| Mic | SunFounder USB mini mic — ALSA card 1 (`CARD=Device`), capture level **3/16** (+4.46 dB; higher clips) |
| SSH | `hapi@192.168.67.23`, key in `secrets/putty_hapi.ppk` (PuTTY format; `secrets/hapi.ppk` is an OpenSSH-format copy that is corrupted — use the PuTTY one with `plink`) |
| Home Assistant | VM at `192.168.66.3:8123` — currently answering **HTTPS** (self-signed; `curl -k`). API token in `secrets/ha-token.txt` |

## Home Assistant side

1. **STT**: Wyoming integration → **Whisper** (loaded).
2. **TTS**: Wyoming integration → **Piper** (loaded).
3. **Satellite link**: Wyoming integration → **Wyoming Protocol**, named `hapi`,
   host `192.168.67.23`, port `10700`. HA connects out to the Pi; no inbound ports on HA.
4. **openwakeword** integration: present but intentionally **disabled / not loaded** —
   wake detection moved onto the Pi so a 1 GB HA VM isn't doing it.
5. **Assist pipeline**: use the pipeline with Whisper STT + Piper TTS; expose the `hapi`
   satellite entity to Assist.
6. **Custom sentences**: copy this repo's `custom_sentences/en/nabu.yaml` to
   `/config/custom_sentences/en/nabu.yaml` on HA and reload (Developer Tools → Reload
   custom sentences, or restart HA). Sentences cover "play/stop brown noise", "brown
   noise on/off", "stop", etc. They intentionally use the built-in `HassRespond` intent —
   a custom intent name would just produce "Unknown intent" TTS.

## Raspberry Pi setup

### Directory layout

```
/opt/wyoming/                     satellite venv + all satellite-side files
/opt/wyoming/bin/                 venv bins + nabumix.py, nabubridge.py, nabu-voice-ctl.py
/opt/wyoming/models/okay_nabu.tflite   kept copy of the wake-word model
/opt/wyoming/sounds/              awake.wav, done.wav, volume_{up,down,ok}.wav
/opt/wyoming/run-satellite.sh     launcher (reads satellite.uri, sets mixer levels, execs satellite)
/opt/wyoming/satellite.uri        root-only; contains tcp://0.0.0.0:10700
/opt/wyoming-openwakeword/        separate venv for the local wake-word server
/opt/hapi-control/                tiny HTTP volume endpoint (server.py + token file)
/usr/local/bin/wyoming-satellite  fixed launcher (see note below)
```

### 1. Python venvs

Debian trixie system Python 3.13. Two venvs:

```bash
# Satellite venv
sudo python3 -m venv /opt/wyoming
sudo /opt/wyoming/bin/pip install \
  wyoming-satellite==1.0.0 wyoming==1.5.0 openwakeword==0.6.0 \
  wyoming-openwakeword==1.5.0 onnxruntime==1.30.0 numpy pyring-buffer pysilero-vad

# Local wake-word server venv (newer wyoming protocol version)
sudo python3 -m venv /opt/wyoming-openwakeword
sudo /opt/wyoming-openwakeword/bin/pip install wyoming-openwakeword==2.1.0
```

**Wheel bug workaround:** the `wyoming-satellite` 1.0.0 PyPI wheel ships a broken
console-script entry point (`wyoming_satellite:__main__.run`, but the module defines
`main`). `/usr/local/bin/wyoming-satellite` is a small wrapper that imports
`wyoming_satellite.__main__.main` and `asyncio.run`s it — recreate it if lost:

```python
#!/opt/wyoming/bin/python
import asyncio, sys
from wyoming_satellite.__main__ import main
if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

### 2. Patched satellite module

`/opt/wyoming/lib/python3.13/site-packages/wyoming_satellite/satellite.py` is **patched**
(brown-noise ducking integration, write-serialisation, etc.). The pristine wheel copy is
kept alongside as `satellite.py.pre-brownmix`. A working copy of this file also lives in
this repo as `sat_master.py` (it may drift from what is deployed — diff before trusting).
All previous experiment backups sit in `/opt/wyoming/backup-*` directories.

### 3. Brown-noise mixer (`nabumix`)

`/opt/wyoming/bin/nabumix.py` — single-owner speaker daemon:

- Continuously owns ALSA `plughw:0,0` at 22050 Hz mono; nothing else may open the card.
- Generates brown noise itself (filtered white noise, RMS-normalised; default level
  `HAPI_NOISE_LEVEL=0.12`, capped at 0.5).
- Receives TTS PCM from `nabubridge.py` over `/run/nabumix/audio.sock` (Unix stream) and
  mixes it over the noise with ducking: fade-down 20 ms, hold 180 ms, fade-up 350 ms.
  Backpressure (2 s of pending audio) is applied to the satellite rather than discarding
  samples, because HA streams TTS faster than realtime.
- Control protocol on `/run/nabumix/ctl.sock` (Unix datagram, one short line per packet):
  `on` | `off` | `level <0.0–0.5>` | `status`.

`/opt/wyoming/bin/nabubridge.py` — stateless `--snd-command` shim. The satellite spawns
and kills its snd-command after every utterance, so the mixer cannot *be* that process;
the bridge just copies stdin PCM into the mixer socket (or forwards control lines in
`control` mode).

`/opt/wyoming/bin/nabu-voice-ctl.py` — satellite `--transcript-command` hook. Reads the
transcript on stdin, matches brown-noise phrases with regexes (including
`set noise level to N%`), and sends the corresponding datagram to the ctl socket.
Bare "stop" also silences the noise; HA still runs its own media-stop intent, so nothing
is shadowed.

### 4. Audio capture / playback configuration

Set in `run-satellite.sh` at every start (because `alsa-restore.service` is static on this
image and never restores saved mixer state):

- Speaker: `amixer -c DigiAMP set Digital 75%` (override with `HAPI_VOLUME_PCT`).
  The pcm512x `Digital` control is 0.5 dB per raw step (207 = 0 dB … 0 = mute) and its
  percentage readout is linear-in-dB, which sounds very uneven — voice volume uses a
  separate gamma-2 perceptual scale (see hapi-control below).
- Mic: `amixer -c Device sset Mic 3` (override with `HAPI_MIC_LEVEL`; 0–16, 3 = +4.46 dB).

### 5. systemd units

| Unit | Purpose | Notes |
|---|---|---|
| `nabu-mixer.service` | runs `nabumix.py` as root | `RuntimeDirectory=nabumix` (no stale sockets), `Before=wyoming-satellite.service` so boot never starts the satellite before the mixer listens; restarting the satellite does not kill the noise |
| `wyoming-openwakeword.service` | local wake-word server on `127.0.0.1:10400` | `wyoming_openwakeword --threshold 0.5 --trigger-level 1 --refractory-seconds 5.0`, `DynamicUser`, BLAS/OMP threads pinned to 1 |
| `wyoming-satellite.service` | runs `run-satellite.sh` as root | `Restart=always`; needs audio + outbound TCP only |
| `hapi-volume.service` | HTTP volume control on `:8977` as user `hapi` | see below |

Enable:

```bash
sudo systemctl enable --now nabu-mixer wyoming-openwakeword wyoming-satellite hapi-volume
```

Boot order handled by the units: network/sound → nabumix → openwakeword → satellite.

### 6. Satellite launch (what `run-satellite.sh` does)

```bash
exec /usr/local/bin/wyoming-satellite \
  --name hapi \
  --uri "$URI" \                              # from /opt/wyoming/satellite.uri (tcp://0.0.0.0:10700)
  --mic-command "arecord -D plughw:CARD=Device -f S16_LE -r 16000 -c 1 -t raw" \
  --mic-command-samples-per-chunk 512 \
  --snd-command /opt/wyoming/bin/nabubridge.py \
  --wake-uri tcp://127.0.0.1:10400 \
  --wake-word-name okay_nabu \
  --awake-wav /opt/wyoming/sounds/awake.wav \
  --done-wav /opt/wyoming/sounds/done.wav \
  --transcript-command /opt/wyoming/bin/nabu-voice-ctl.py
```

Because local wake is enabled, the satellite starts the pipeline at the ASR stage
after `okay_nabu` fires; HA never re-detects the wake word.

### 7. Volume control endpoint (`hapi-control`)

`/opt/hapi-control/server.py` — deliberately tiny HTTP server on port **8977**
(token in `/opt/hapi-control/token`, HMAC-checked) that only changes the DigiAMP
playback volume. Runs as unprivileged user `hapi` (in the `audio` group — no sudo, no
escalation path). Uses the perceptual scale: 0 = −60 dB, 100 = 0 dB, gamma 2, which puts
**75 at −26 dB** — this install's default.

## Day-to-day usage

- **"Okay nabu"** → local detection → HA pipeline (STT/intent/TTS) → TTS plays through the
  mixer, ducking the noise while it speaks.
- **"Play brown noise" / "Stop brown noise" / "Brown noise on/off"** → HA speaks the
  response *and* the transcript hook flips the mixer locally (belt and braces: the HA
  sentence answers, the Pi hook actually controls the audio).
- **"Set noise level to 20 percent"** → transcript hook only (`level 0.2`).
- Manual control: `echo on|off | nc -U /run/nabumix/ctl.sock` (or
  `echo status | nc -U /run/nabumix/ctl.sock` to check state).

## Verification / troubleshooting

```bash
# On the Pi
systemctl status nabu-mixer wyoming-openwakeword wyoming-satellite hapi-volume
journalctl -u nabumix -u wyoming-satellite -u wyoming-openwakeword -f
ss -tlnp | grep -E '10700|10400|8977'      # satellite / wake / volume ports
arecord -D plughw:CARD=Device -f S16_LE -r 16000 -c 1 -t raw -d 3 /tmp/t.wav  # mic test
aplay -D plughw:0,0 /opt/wyoming/sounds/awake.wav                              # speaker test
echo status | nc -U /run/nabumix/ctl.sock  # mixer state
```

- Wake word too sensitive/insensitive: adjust `--threshold` / `--trigger-level` in
  `wyoming-openwakeword.service`.
- HA "hapi" entity unavailable: satellite listens on 10700; confirm HA's Wyoming Protocol
  entry points at `192.168.67.23:10700` and that HA's firewall allows outbound.
- Custom brown-noise sentences not firing: confirm the file is at
  `/config/custom_sentences/en/nabu.yaml` and the conversation agent was reloaded; test
  phrases in the Assist debug panel.
- The Pi cannot reach HA over plain HTTP anymore (HA answers HTTPS on 8123) — relevant for
  any scripts on the Pi that call the HA API directly.

## Repo file map

| File | Role |
|---|---|
| `hardware_details.txt` | hardware / network quick reference |
| `custom_sentences/en/nabu.yaml` | Assist sentences for brown noise (deploy to HA) |
| `sat_master.py` | earlier working copy of the satellite module (drifts from the Pi; see `pi/patched-satellite/`) |
| `pi/run-satellite.sh` | satellite launcher (deploy to `/opt/wyoming/run-satellite.sh`) |
| `pi/bin/nabumix.py` | brown-noise mixer daemon (deploy to `/opt/wyoming/bin/`) |
| `pi/bin/nabubridge.py` | snd-command bridge (deploy to `/opt/wyoming/bin/`) |
| `pi/bin/nabu-voice-ctl.py` | transcript-command hook (deploy to `/opt/wyoming/bin/`) |
| `pi/bin/hapi-control-server.py` | volume HTTP endpoint (deploy to `/opt/hapi-control/server.py`) |
| `pi/bin/wyoming-satellite-launcher.py` | fixed launcher for the broken 1.0.0 wheel entry point (deploy to `/usr/local/bin/wyoming-satellite`) |
| `pi/patched-satellite/satellite.py` | `wyoming_satellite` module as deployed — adds local noise on/off handling and mixer control on top of the PyPI 1.0.0 original |
| `pi/systemd/*.service` | the four systemd units (deploy to `/etc/systemd/system/`) |
| `secrets/putty_hapi.ppk` | SSH key for `hapi@192.168.67.23` (gitignored — never published) |
| `secrets/ha-token.txt` | HA long-lived API token (gitignored — never published) |
