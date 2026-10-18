#!/usr/bin/env python3
"""LAN control endpoint for the hapi Wyoming satellite.

Deliberately tiny: it only changes the DigiAMP+ playback volume.
Runs as the unprivileged "hapi" user, which is in the audio group, so no
sudo is needed and a compromise cannot escalate.
"""
import hmac
import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("HAPI_CONTROL_HOST", "0.0.0.0")
PORT = int(os.environ.get("HAPI_CONTROL_PORT", "8977"))
TOKEN_FILE = os.environ.get("HAPI_CONTROL_TOKEN", "/opt/hapi-control/token")
CARD = "0"
CONTROL = "Digital"

# Measured on this DigiAMP+ (pcm512x "Digital" control, raw range 0-207):
#   207 = 0.00 dB, 191 = -8.00, 175 = -16.00, 159 = -24.00, 155 = -26.00,
#   127 = -40.00,  63 = -72.00,  15 = -96.00,    0 = mute
# i.e. exactly 0.5 dB per raw step. The control's own percentage readout is
# linear in dB, which makes equal steps sound very uneven, so voice uses a
# separate perceptual scale below.
DB_PER_STEP = 0.5
# Perceptual scale: 0 = MIN_DB, 100 = 0 dB, with a gamma curve so steps
# sound even. GAMMA=2 with MIN_DB=-60 deliberately puts 75 at -26 dB,
# which is this install's existing default level.
MIN_DB = -60.0
GAMMA = 2.0

STATE_RE = re.compile(
    r"Playback\s+(\d+)\s+\[(\d+)%\]\s+\[(-?[\d.]+)dB\]"
)
LIMITS_RE = re.compile(r"Limits:\s*Playback\s+(\d+)\s+-\s+(\d+)")

# Fallback only; the real maximum is read from the control's own limits.
DEFAULT_MAX_RAW = 207


def _amixer(*args):
    return subprocess.run(
        ["amixer", "-c", CARD, *args],
        capture_output=True, text=True, check=True,
    ).stdout


def max_raw():
    """Full-scale raw value for this control (207 on a pcm512x)."""
    limits = LIMITS_RE.search(_amixer("sget", CONTROL))
    return int(limits.group(2)) if limits else DEFAULT_MAX_RAW


def read_volume():
    """Return the current mixer state."""
    match = STATE_RE.search(_amixer("sget", CONTROL))
    if not match:
        raise RuntimeError("could not parse amixer output")
    raw, pct, db = int(match.group(1)), int(match.group(2)), float(match.group(3))
    if db < -1000:  # amixer reports -99999.99 when the control is muted
        db = float(MIN_DB)
        raw = 0
    return {
        "raw": raw,
        "mixer_pct": pct,
        "db": round(db, 2),
        "pct": pct_for_db(db),
    }


def db_for_pct(pct):
    """Map a 0-100 perceptual volume request onto dB."""
    return MIN_DB + (-MIN_DB) * (max(0.0, min(100.0, pct)) / 100.0) ** GAMMA


def pct_for_db(db):
    """Inverse of db_for_pct, so relative steps stay perceptually even."""
    ratio = max(0.0, min(1.0, (db - MIN_DB) / (-MIN_DB)))
    return round(100.0 * ratio ** (1.0 / GAMMA), 1)


def raw_for_db(db):
    return max(0, min(max_raw(), round(max_raw() + db / DB_PER_STEP)))


def set_pct(pct):
    """Set volume from a 0-100 request and return the resulting state."""
    _amixer("sset", CONTROL, str(raw_for_db(db_for_pct(pct))))
    state = read_volume()
    state["requested_pct"] = round(pct, 1)
    return state


def nudge(delta_pct):
    """Adjust relative to the current perceptual volume."""
    return set_pct(read_volume()["pct"] + delta_pct)


class Handler(BaseHTTPRequestHandler):
    server_version = "hapi-control/1.0"

    def _token_ok(self):
        supplied = self.headers.get("X-Hapi-Token") or ""
        if not supplied:
            query = self.path.partition("?")[2]
            for pair in query.split("&"):
                if pair.startswith("token="):
                    supplied = pair[6:]
                    break
        return bool(supplied) and hmac.compare_digest(supplied, TOKEN)

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._reply(200, {"ok": True})
        elif path == "/volume":
            self._reply(200, read_volume())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/volume":
            self._reply(404, {"error": "not found"})
            return
        if not self._token_ok():
            self._reply(403, {"error": "bad or missing token"})
            return
        query = dict(
            pair.split("=", 1) for pair in self.path.partition("?")[2].split("&") if "=" in pair
        )
        try:
            if "pct" in query:
                result = set_pct(float(query["pct"]))
            elif "delta" in query:
                result = nudge(float(query["delta"]))
            else:
                self._reply(400, {"error": "need pct=<0-100> or delta=<n>"})
                return
        except (RuntimeError, ValueError) as err:
            self._reply(500, {"error": str(err)})
            return
        print("volume change: %s" % result, flush=True)
        self._reply(200, result)

    def log_message(self, fmt, *a):
        print("%s %s" % (self.address_string(), fmt % a), flush=True)


with open(TOKEN_FILE) as handle:
    TOKEN = handle.read().strip()

if __name__ == "__main__":
    print("listening on %s:%d" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
