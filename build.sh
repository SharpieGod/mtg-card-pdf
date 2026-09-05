#!/bin/bash

uv add --dev pyinstaller
uv run pyinstaller --name "MTG Print Sheet" --windowed --onefile mtg.py