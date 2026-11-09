#!/usr/bin/python3 -IB
"""Entry point: python3 -I -B /opt/homelab-dash/collect.py --config /etc/homelab-dash/config.ini"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from homelab_dash.collector import main  # noqa: E402

if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Abandoned (hung) check threads must never keep the process alive.
    os._exit(code)
