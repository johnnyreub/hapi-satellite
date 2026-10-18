#!/usr/bin/env python3
"""Satellite snd-command bridge.

The satellite spawns its --snd-command per utterance and kills it on AudioStop
(detect_after_stop=True), so the mixer cannot be that process: it would lose its
noise state every time. This stateless shim occupies the snd-command slot. It
copies raw PCM from stdin into the mixer's audio socket and forwards control
lines from stdin (mode 'control') or signals via the control socket.

Two modes, chosen by argv[1]:
  pipe     (default) stdin bytes -> audio socket, untouched
  control  stdin text lines     -> control socket datagrams
"""
import os
import socket
import sys

AUDIO_SOCK = os.environ.get("HAPI_MIX_AUDIO_SOCK", "/run/nabumix/audio.sock")
CTL_SOCK = os.environ.get("HAPI_MIX_CTL_SOCK", "/run/nabumix/ctl.sock")
WAIT_SECONDS = float(os.environ.get("HAPI_MIX_BRIDGE_WAIT", "2.0"))


def _log(msg):
    sys.stderr.write("nabubridge: %s\n" % msg)
    sys.stderr.flush()


def send_control(line):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(line.encode("utf-8") + b"\n", CTL_SOCK)
        return True
    except OSError as err:
        _log("control send failed: %s" % err)
        return False
    finally:
        sock.close()


def wait_for(path, deadline):
    while (os.path.exists(path)) is False:
        import time

        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def run_pipe():
    import time

    deadline = time.monotonic() + WAIT_SECONDS
    if not wait_for(AUDIO_SOCK, deadline):
        _log("mixer audio socket never appeared; passing audio through silently")
        # Drain stdin so the satellite never blocks on a full pipe.
        while sys.stdin.buffer.read(8192):
            pass
        return 0

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(AUDIO_SOCK)
    except OSError as err:
        _log("mixer connect failed: %s" % err)
        while sys.stdin.buffer.read(8192):
            pass
        return 0
    try:
        while True:
            data = sys.stdin.buffer.read(8192)
            if not data:
                break
            conn.sendall(data)
    except (BrokenPipeError, OSError) as err:
        _log("audio forward stopped: %s" % err)
    finally:
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        conn.close()
    return 0


def run_control():
    for line in sys.stdin:
        line = line.strip()
        if line:
            send_control(line)
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pipe"
    if mode == "control":
        return run_control()
    return run_pipe()


if __name__ == "__main__":
    sys.exit(main())
