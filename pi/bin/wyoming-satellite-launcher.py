#!/opt/wyoming/bin/python
"""Launcher for wyoming-satellite.

The PyPI 1.0.0 wheel ships a broken console-script entry point
(`wyoming_satellite:__main__.run`) but __main__ actually defines `main`,
so the generated /opt/wyoming/bin/wyoming-satellite crashes with
AttributeError. This wrapper calls the real entry point.
"""
import asyncio, sys
from wyoming_satellite.__main__ import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
