#!/usr/bin/python3 -IB
"""Entry point: python3 -I -B /opt/homelab-dash/serve.py --config /etc/homelab-dash/config.ini"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from homelab_dash.webserver import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
