"""Small helpers shared by the data pipeline, the API clients and the views."""
import json
import os
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

_NO_DECOMPOSITION = str.maketrans({"đ": "d", "Đ": "D", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "ß": "ss"})


def to_int(value):
    """int(float(value)) or None when value is not numeric."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def whole(value):
    """6.0 -> 6, anything else unchanged."""
    return int(value) if isinstance(value, float) and value == int(value) else value


def ordinal_suffix(n):
    """1 -> 'st', 2 -> 'nd', 12 -> 'th' ('th' for anything that is not an int)."""
    if not isinstance(n, int):
        return "th"
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def fold(text):
    """Lower-case ASCII form used to compare names ('Jakirović' == 'jakirovic', 'Đorđe' == 'dorde')."""
    return unicodedata.normalize("NFKD", (text or "").translate(_NO_DECOMPOSITION)).encode("ascii", "ignore").decode().lower()


class Throttle:
    """Keeps at least ``interval`` seconds between the starts of two API calls.

    ``interval`` may be raised and lowered by the owner (PitchAPI backs off after a 429);
    ``min_interval`` is the floor it settles back to.
    """

    def __init__(self, min_interval, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = self.interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self._last = None

    def wait(self):
        if self._last is not None:
            remaining = self.interval - (self.clock() - self._last)
            if remaining > 0:
                self.sleep(remaining)

    def mark(self):
        self._last = self.clock()


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return utc_now().isoformat(timespec="seconds")


def read_json(path):
    """Decoded JSON file. Raises OSError (unreadable) or ValueError (not valid JSON)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json_atomic(path, data, indent=2):
    """Write via a temp file so a crash never leaves a half-written (or emptied) data file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
