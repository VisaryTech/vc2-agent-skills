#!/usr/bin/env python3
"""CLI entry point for the 1c-odata skill."""
import sys

sys.dont_write_bytecode = True

from onec_lib.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
