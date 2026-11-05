"""PyInstaller entry point (PyInstaller needs a script, not `-m`)."""
import sys

from audio_agent.__main__ import main

sys.exit(main())
