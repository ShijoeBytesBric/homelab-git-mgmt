#!/usr/bin/env python3
"""
Check that every ServiceMonitor has a matching Service in the same app dir.

Usage: python scripts/check_servicemonitor.py
Exit code 0 = pass, 1 = fail.

Not shipped in v1 — create this file when you add your first ServiceMonitor.
"""

import sys
import yaml
from pathlib import Path


def main():
    print("ServiceMonitor pairing check not yet implemented.")
    print("Create scripts/check_servicemonitor.py when you add your first ServiceMonitor.")
    sys.exit(0)


if __name__ == "__main__":
    main()
