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

# --windowed PyInstaller builds set sys.stdout and sys.stderr to None.
# Kivy writes to stderr during its __init__.py import; without valid streams
# Python's logging StreamHandler raises AttributeError which cascades into
# infinite recursion and a RecursionError before the GUI ever opens.
# Redirect to a log file so startup errors are visible for debugging.
if _sys.stdout is None or _sys.stderr is None:
    _log_dir = _os.path.dirname(_sys.executable)
    _log_path = _os.path.join(_log_dir, "ATSClient_startup.log")
    _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
    if _sys.stdout is None:
        _sys.stdout = _log_file
    if _sys.stderr is None:
        _sys.stderr = _log_file

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
