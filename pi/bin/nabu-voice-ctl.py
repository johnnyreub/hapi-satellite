#!/usr/bin/env python3
"""Voice -> nabumix bridge (satellite --transcript-command).

The satellite pipes the transcribed text into stdin. Match noise commands and
forward them to the nabumix control socket (/run/nabumix/ctl.sock):

  "stop (the) (brown) noise" / "brown noise off"   -> off
  "play/start/turn on (the) (brown) noise"         -> on
  bare "stop"                                      -> off (HA still answers)
  "set noise (level) to N (percent|%)"             -> level N/100

Everything else is ignored so normal HA intents are unaffected.
"""
import os
import re
import socket
import sys

CTL_SOCK = os.environ.get("HAPI_MIX_CTL_SOCK", "/run/nabumix/ctl.sock")


def send(line):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto((line + "\n").encode("utf-8"), CTL_SOCK)
        s.close()
        return True
    except OSError as err:
        sys.stderr.write("nabu-voice-ctl: send failed: %s\n" % err)
        return False


def main():
    text = sys.stdin.read().strip().lower()
    if not text:
        return

    m = re.search(r"noise\b.*?\b(\d{1,3})\s*(?:%|percent)", text)
    if m:
        send("level %s" % (int(m.group(1)) / 100.0))

    if re.search(r"\b(stop|turn off|switch off)\b.*\bnoise\b|\bnoise\b.*\b(off|stop)\b", text):
        send("off")
    elif re.search(r"\b(play|start|turn on|switch on)\b.*\bnoise\b|\bnoise\b.*\b(on|play)\b", text):
        send("on")
    elif re.fullmatch(r"(please\s+)?stop[.!\s]*", text):
        # Bare "stop": also silence the noise. HA separately runs its own
        # media-stop intent (timers/players), so nothing is shadowed.
        send("off")


if __name__ == "__main__":
    main()
