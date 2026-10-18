#!/bin/bash
# Wyoming satellite launcher for hapi (Raspberry Pi 3B, USB mic + DigiAMP+ HAT).
#
# The satellite runs as a Wyoming TCP SERVER (tcp://0.0.0.0:10700); Home
# Assistant dials IN to it via the "Wyoming Protocol" integration.
#
# URI is read from /opt/wyoming/satellite.uri so it is not exposed in the
# unit file or in /proc/*/cmdline.
set -euo pipefail

URI_FILE=/opt/wyoming/satellite.uri
[ -r "$URI_FILE" ] || { echo "FATAL: $URI_FILE missing/unreadable" >&2; exit 1; }
URI=$(cat "$URI_FILE")
[ -n "$URI" ] || { echo "FATAL: $URI_FILE is empty" >&2; exit 1; }

# Mic  : USB Audio, card 1. plughw does the 44.1kHz -> 16kHz resample.
# Snd  : Raspberry Pi DigiAMP+ HAT, card 0. Piper emits 22050Hz mono S16_LE.
#
# Playback volume: ALSA mixer 'Digital' on card 0 (DigiAMP+ HAT).
# The pcm512x pvolume scale is very steep and NOT linear in percent:
#   100% = 0.00 dB | 75% = -26.0 dB | 50% = -51.5 dB | 25% = -77.5 dB
# so pick by ear, not by arithmetic. Override with HAPI_VOLUME_PCT=<0-100>.
# Done here rather than via alsactl/alsa-restore because alsa-restore.service is
# 'static' on this image and never runs, so saved mixer state would not survive
# a reboot.
VOLUME_PCT="${HAPI_VOLUME_PCT:-75}"
if ! amixer -c DigiAMP set Digital "${VOLUME_PCT}%" >/dev/null 2>&1; then
  echo "WARNING: could not set DigiAMP volume (control Digital on DigiAMP)" >&2
fi

# Mic capture gain: USB mic, card 1, level 3 of 16 (+4.46 dB). Higher levels clip.
# Override with HAPI_MIC_LEVEL=<0-16>.
MIC_LEVEL="${HAPI_MIC_LEVEL:-3}"
if ! amixer -c Device sset Mic "${MIC_LEVEL}" >/dev/null 2>&1; then
  echo "WARNING: could not set mic capture gain (control Mic on USB PnP mic card)" >&2
fi

exec /usr/local/bin/wyoming-satellite \
  --name hapi \
  --uri "$URI" \
  --mic-command "arecord -D plughw:CARD=Device -f S16_LE -r 16000 -c 1 -t raw" \
  --mic-command-samples-per-chunk 512 \
  --snd-command /opt/wyoming/bin/nabubridge.py \
  --wake-uri tcp://127.0.0.1:10400 \
  --wake-word-name okay_nabu \
  --awake-wav /opt/wyoming/sounds/awake.wav \
  --done-wav /opt/wyoming/sounds/done.wav \
  --transcript-command /opt/wyoming/bin/nabu-voice-ctl.py
