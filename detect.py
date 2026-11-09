#!/usr/bin/python3 -IB
"""Entry point: sudo python3 -I -B /opt/homelab-dash/detect.py [--write FILE]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from homelab_dash.detect import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
