# PyInstaller runtime hook — runs before any script imports.
#
# Archipelago's worlds/__init__.py scans the worlds/ directory at import time.
# Inside a PyInstaller onefile bundle that directory may not exist on disk,
# causing a FileNotFoundError that crashes the exe before main() is reached.
#
# We patch os.scandir/os.listdir to return empty results for paths inside
# sys._MEIPASS that don't exist, so the scan silently finds nothing.
# The explicit `import worlds.american_truck_simulator` in ATSClient.py then
# registers our world via AutoWorldRegister regardless.

import os as _os
import sys as _sys

_orig_scandir = _os.scandir
_orig_listdir = _os.listdir
_meipass = getattr(_sys, "_MEIPASS", None)


def _safe_scandir(path="."):
    try:
        return _orig_scandir(path)
    except (FileNotFoundError, NotADirectoryError, OSError):
        if _meipass and str(path).startswith(_meipass):
            return iter([])
        raise


def _safe_listdir(path="."):
    try:
        return _orig_listdir(path)
    except (FileNotFoundError, NotADirectoryError, OSError):
        if _meipass and str(path).startswith(_meipass):
            return []
        raise


_os.scandir = _safe_scandir
_os.listdir = _safe_listdir
