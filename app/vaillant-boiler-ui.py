#!/usr/bin/env python3
"""Poll ebusd for Vaillant BAI values, append them to a CSV file and serve a
live web UI. The code is in the vaillant_boiler_ui package next to this file;
see vaillant_boiler_ui/main.py for usage."""

from vaillant_boiler_ui.main import main

if __name__ == "__main__":
    main()
