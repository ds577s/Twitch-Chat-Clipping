"""Structured progress events for the web UI.

When the UI starts a run it sets CLIPPER_EVENTS to a file path; every emit() appends one JSON
line there. It also sets CLIPPER_STOP_FILE: when that file appears, the run stops the same way
Ctrl-C would (finishing clips in progress). From the plain command line both are unset and
everything here does nothing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def enabled() -> bool:
    return bool(os.environ.get("CLIPPER_EVENTS"))


def emit(kind: str, **data) -> None:
    path = os.environ.get("CLIPPER_EVENTS")
    if not path:
        return
    line = json.dumps({"type": kind, "time": round(time.time(), 3), **data}, ensure_ascii=False, default=str)
    with _lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def stop_requested() -> bool:
    path = os.environ.get("CLIPPER_STOP_FILE")
    return bool(path) and Path(path).exists()
