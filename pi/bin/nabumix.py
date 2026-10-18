#!/usr/bin/env python3
"""Single-owner speaker mixer for the hapi Wyoming satellite.

The DigiAMP+ exposes one playback channel. The satellite spawns its
--snd-command afresh for every utterance (disconnect_after_stop=True) and kills
it again after AudioStop, so no other long-lived process may own the device.
This daemon is the only writer to aplay: it runs continuously, plays brown
noise on its own, and ducks the noise whenever satellite audio arrives.

Wiring:
  satellite --snd-command -> nabubridge.py -> AUDIO_SOCK -> nabumix -> aplay
  satellite voice patch  -> CTL_SOCK      -> nabumix

Control protocol: Unix datagram socket, one short UTF-8 line per datagram.
  on | off | level <float> | status
"""
import array
import os
import random
import select
import signal
import socket
import subprocess
import sys
import time

CHUNK = 1024
WIDTH = 2
BYTES_PER_CHUNK = CHUNK * WIDTH
PCM_MAX = 32767

RATE = int(os.environ.get("HAPI_SND_RATE", "22050"))
DEVICE = os.environ.get("HAPI_SND_DEVICE", "plughw:0,0")
AUDIO_SOCK = os.environ.get("HAPI_MIX_AUDIO_SOCK", "/run/nabumix/audio.sock")
CTL_SOCK = os.environ.get("HAPI_MIX_CTL_SOCK", "/run/nabumix/ctl.sock")

# Per-bridge backlog ceiling. When a client reaches this, the mixer simply stops
# reading: recv() is non-blocking, so the kernel socket buffer fills and the
# sender blocks. That backpressures the satellite to realtime and loses nothing.
# Discarding here instead would garble speech, because Home Assistant streams
# TTS far faster than realtime and the buffer would overflow constantly.
MAX_PENDING_BYTES = int(
    float(os.environ.get("HAPI_MIX_MAX_PENDING_SECONDS", "2.0")) * RATE * WIDTH
)

FADE_DOWN = float(os.environ.get("HAPI_NOISE_FADE_DOWN", "0.020"))
HOLD = float(os.environ.get("HAPI_NOISE_HOLD", "0.180"))
FADE_UP = float(os.environ.get("HAPI_NOISE_FADE_UP", "0.350"))

# Measured RMS of this filter fed uniform(-1,1) white noise on this Pi, over
# 4M samples. Dividing by it makes the requested level equal the output RMS.
# The old prototype used 1.76684, which is for unit-variance white input and
# came out ~31x too quiet here.
FILTER_RMS = 0.057195

# Offline self-test knobs.
DRY_RUN = "--dry-run" in sys.argv
MAX_SECONDS = 0.0
for flag in ("--max-seconds",):
    if flag in sys.argv:
        MAX_SECONDS = float(sys.argv[sys.argv.index(flag) + 1])

_stopping = False


def _log(msg):
    sys.stderr.write("nabumix: %s\n" % msg)
    sys.stderr.flush()


def _on_stop(signum, frame):
    global _stopping
    _stopping = True


signal.signal(signal.SIGTERM, _on_stop)
signal.signal(signal.SIGINT, _on_stop)


class Aplay:
    """Sole owner of the hardware playback channel."""

    def __init__(self, device, rate):
        self.device = device
        self.rate = rate
        self.proc = None
        self.retry_at = 0.0
        self.dry = DRY_RUN
        self.out = None
        path = os.environ.get("HAPI_MIX_TEST_OUT", "")
        if path:
            self.out = open(path, "wb", buffering=0)  # noqa: SIM115

    def _spawn(self):
        if self.dry:
            return
        cmd = [
            "aplay",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.rate),
            "-c", "1",
            "-t", "raw",
        ]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    def write(self, payload):
        if not payload:
            return
        if self.out is not None:
            self.out.write(payload)
            return
        while not _stopping:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    _log("aplay exited rc=%s; retrying" % self.proc.returncode)
                    self.proc = None
                if time.monotonic() < self.retry_at:
                    time.sleep(0.20)
                    continue
                self.retry_at = time.monotonic() + 1.0
                try:
                    self._spawn()
                except Exception as err:  # noqa: BLE001
                    _log("aplay spawn failed: %s" % err)
                    self.proc = None
                    continue
                if self.dry:
                    return
            try:
                self.proc.stdin.write(payload)
                self.proc.stdin.flush()
                return
            except (BrokenPipeError, OSError, ValueError) as err:
                _log("aplay write failed: %s" % err)
                self._kill()

    def _kill(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None

    def close(self):
        self._kill()


class Mixer:
    """Brown-noise source with a ducking envelope."""

    def __init__(self, level):
        self.level = max(0.0, min(0.5, float(level)))
        self.enabled = False
        self.gain = 0.0
        self.since_speech = HOLD + FADE_UP
        self.brown = 0.0

    def set_enabled(self, on):
        self.enabled = bool(on)

    def set_level(self, level):
        self.level = max(0.0, min(0.5, float(level)))

    def note_speech(self):
        self.since_speech = 0.0

    def tick(self, n_samples):
        """Advance the envelope by n_samples and return its amplitude."""
        self.since_speech += n_samples / float(RATE)
        if not self.enabled or self.level <= 0.0:
            self.gain = 0.0
            return 0.0
        if self.since_speech <= HOLD:
            # Fade to silence quickly so the first phoneme is never masked.
            step = self.level * (n_samples / float(RATE)) / max(FADE_DOWN, 1e-6)
            self.gain = max(0.0, self.gain - step)
        else:
            # Rise slowly back to the requested level.
            ramp = (self.since_speech - HOLD) / max(FADE_UP, 1e-6)
            target = min(1.0, ramp) * self.level
            self.gain += (target - self.gain) * min(
                1.0, (n_samples / float(RATE)) / max(FADE_UP, 1e-6)
            )
        return self.gain

    def _next_raw(self):
        white = random.uniform(-1.0, 1.0)
        self.brown = (self.brown + 0.02 * white) / 1.02
        return self.brown

    def render(self, n_samples, speech=None):
        """One output chunk: speech (optional) plus ducked brown noise."""
        gain = self.tick(n_samples)
        out = array.array("h", [0]) * n_samples
        scale = gain / FILTER_RMS
        for i in range(n_samples):
            value = self._next_raw() * scale
            if value > 0.995:
                value = 0.995
            elif value < -0.995:
                value = -0.995
            sample = int(value * PCM_MAX)
            if speech is not None:
                sample += speech[i]
                if sample > PCM_MAX:
                    sample = PCM_MAX
                elif sample < -PCM_MAX - 1:
                    sample = -PCM_MAX - 1
            elif sample > PCM_MAX:
                sample = PCM_MAX
            elif sample < -PCM_MAX - 1:
                sample = -PCM_MAX - 1
            out[i] = sample
        return out


def handle_control(mixer, text):
    parts = text.strip().split()
    if not parts:
        return
    cmd = parts[0].lower()
    if cmd == "on":
        mixer.set_enabled(True)
        _log("noise on level=%.4f" % mixer.level)
    elif cmd == "off":
        mixer.set_enabled(False)
        _log("noise off")
    elif cmd == "level":
        if len(parts) > 1:
            try:
                mixer.set_level(float(parts[1]))
                _log("noise level=%.4f" % mixer.level)
            except ValueError:
                _log("bad level: %r" % parts[1])
        else:
            _log("bad level: %r" % text.strip())
    elif cmd == "status":
        _log(
            "status enabled=%s level=%.4f gain=%.4f since_speech=%.3f"
            % (mixer.enabled, mixer.level, mixer.gain, mixer.since_speech)
        )
    else:
        _log("unknown control: %r" % text.strip())


def _bind(path, sock_type):
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, sock_type)
    sock.bind(path)
    if sock_type == socket.SOCK_STREAM:
        sock.listen(8)
    return sock


def main():
    mixer = Mixer(float(os.environ.get("HAPI_NOISE_LEVEL", "0.12")))
    aplay = Aplay(DEVICE, RATE)

    audio_srv = _bind(AUDIO_SOCK, socket.SOCK_STREAM)
    ctl_srv = _bind(CTL_SOCK, socket.SOCK_DGRAM)
    _log(
        "start audio=%s ctl=%s device=%s rate=%d level=%.4f dry=%s"
        % (AUDIO_SOCK, CTL_SOCK, DEVICE, RATE, mixer.level, DRY_RUN)
    )

    clients = []
    started = time.monotonic()

    # aplay consumes PCM at a fixed wall-clock rate, so chunks must leave here
    # on a monotonic schedule. Timing off select() instead makes output depend
    # on socket activity and underruns the device, which clicks.
    chunk_secs = CHUNK / float(RATE)
    deadline = time.monotonic()

    while not _stopping:
        if MAX_SECONDS and (time.monotonic() - started) > MAX_SECONDS:
            _log("dry-run duration reached")
            break

        now = time.monotonic()
        if now < deadline:
            timeout = deadline - now
        elif now > deadline + chunk_secs:
            # Fell behind (slow CPU, a stall). Resync rather than burst-catch up.
            deadline = now
            timeout = 0.0
        else:
            timeout = 0.0

        # Sockets that hit EOF stay open but must leave the select set:
        # a closed fd makes select() fail, and dropping every client on that
        # error silently threw away speech still buffered.
        rlist = [audio_srv, ctl_srv] + [c[0] for c in clients if not c[4]]
        try:
            ready, _, _ = select.select(rlist, [], [], timeout)
        except (OSError, ValueError) as err:
            _log("select failed (%s); dropping clients" % err)
            clients = []
            deadline = time.monotonic()
            continue

        for sock in ready:
            if sock is ctl_srv:
                try:
                    data, _addr = ctl_srv.recvfrom(4096)
                except OSError:
                    continue
                handle_control(mixer, data.decode("utf-8", "replace"))
                continue

            if sock is audio_srv:
                try:
                    conn, _ = audio_srv.accept()
                except OSError:
                    continue
                conn.setblocking(False)
                # [socket, pending bytes, bytes read, backlog hits, saw EOF]
                clients.append([conn, b"", 0, 0, False])
                _log("bridge connected (%d total)" % len(clients))
                continue

            for entry in list(clients):
                if entry[0] is not sock:
                    continue
                if entry[4]:
                    # Already saw EOF; keep draining what is buffered.
                    continue
                # Never discard. A full backlog means stop reading and let the
                # sender block; dropping bytes here is what garbled speech.
                room = MAX_PENDING_BYTES - len(entry[1])
                if room <= 0:
                    entry[3] += 1
                    continue
                try:
                    data = sock.recv(min(65536, room))
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    data = b""
                if data:
                    entry[1] += data
                    entry[2] += len(data)
                else:
                    # EOF. Do not drop entry[1]: the tail of the utterance is
                    # still buffered and must play out before we let go. Leave
                    # the fd open so it stays out of the select set cleanly.
                    entry[4] = True

        speech = None
        if clients:
            # Oldest-first so speech stays in order; one chunk per tick keeps
            # the envelope resolution fine.
            entry = clients[0]
            if len(entry[1]) >= BYTES_PER_CHUNK:
                raw = entry[1][:BYTES_PER_CHUNK]
                entry[1] = entry[1][BYTES_PER_CHUNK:]
                samples = array.array("h")
                samples.frombytes(raw)
                mixer.note_speech()
                speech = samples
            elif entry[4] and entry[1]:
                # EOF with a short tail: pad to a whole chunk so the last
                # fraction of speech is still heard, then retire the client.
                tail = entry[1]
                if len(tail) % WIDTH:
                    tail = tail[:-1]          # drop a stray half-sample
                samples = array.array("h")
                samples.frombytes(tail)
                samples.extend([0] * (CHUNK - len(samples)))
                samples = samples[:CHUNK]
                entry[1] = b""
                mixer.note_speech()
                speech = samples

        while clients and clients[0][4] and not clients[0][1]:
            entry = clients.pop(0)
            try:
                entry[0].close()
            except OSError:
                pass
            _log("bridge disconnected (%d left, %d bytes read)"
                 % (len(clients), entry[2]))
            if entry[3] > 0:
                _log("backlog hit the %d byte ceiling %d times (no data lost)"
                     % (MAX_PENDING_BYTES, entry[3]))

        # Always emit a chunk: aplay must be fed at the sample rate, and the
        # mixer owns the device for good, so silence is written explicitly
        # rather than leaving the pipe to underrun.
        if speech is not None:
            aplay.write(mixer.render(CHUNK, speech=speech).tobytes())
        else:
            aplay.write(mixer.render(CHUNK).tobytes())

        deadline += chunk_secs

    for entry in clients:
        try:
            entry[0].close()
        except OSError:
            pass
    for srv, path in ((audio_srv, AUDIO_SOCK), (ctl_srv, CTL_SOCK)):
        try:
            srv.close()
        except OSError:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    aplay.close()
    _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
