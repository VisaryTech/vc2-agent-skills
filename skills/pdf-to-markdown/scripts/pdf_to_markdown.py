#!/usr/bin/env python3
"""Command-line entry point for local PDF evidence extraction."""
import sys

from pdf_markdown.cli import main


if __name__ == "__main__":
    sys.exit(main())
