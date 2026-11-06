"""The editable weekly 168-hour plan, stored as a small versioned file on the persistent disk.

A SETTING, not a snapshot: it lives in its own directory (env PERSONAL_PLAN_DIR, read at call
time), never in the snapshot store (pruned) or the jobs DB. Arithmetic is done in integer
quarter-hours so float rounding can never let 167.99 through.

Order of checks in save_plan: validate the entries FIRST (PlanInvalid), then the version
conflict (PlanConflict). base_version=1 when no plan exists is a conflict (a stale editor).
The stored version is the max of the readable main and .prev files, so a corrupt main never
makes version numbers go backwards (a version that existed only in the corrupt file is lost).
Pure stdlib.
"""
import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timezone

TOTAL_HOURS = 168
STEP_HOURS = 0.25
TOTAL_QUARTERS = 672

_FILE = "hours_plan.json"
_PREV = "hours_plan.prev.json"
_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


class PlanInvalid(ValueError):
    """The plan cannot be saved; the message is user-showable."""


class PlanConflict(Exception):
    """The plan was saved elsewhere since the editor loaded it."""

    def __init__(self, current_version):
        super().__init__(f"Plan changed elsewhere (now version {current_version}).")
        self.current_version = current_version


class PlanStorageError(Exception):
    """The plan file could not be written (disk full, permissions, missing mount...)."""


def _plan_dir() -> str:
    d = os.environ.get("PERSONAL_PLAN_DIR")
    if d and d.strip():
        return os.path.abspath(d.strip())
    _log.warning("hours_plan: PERSONAL_PLAN_DIR is unset; using the ephemeral folder next to the module")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "personal_plan")


def _fmt(q: int) -> str:
    h = q / 4
    return str(int(h)) if h == int(h) else f"{h:g}"


def _clean_allowed(allowed_categories) -> list:
    if not isinstance(allowed_categories, (list, tuple)) or not allowed_categories:
        raise PlanInvalid("Allowed buckets must be a non-empty list.")
    out = []
    for c in allowed_categories:
        if not isinstance(c, str) or not c.strip():
            raise PlanInvalid("Allowed buckets must be non-blank text.")
        out.append(c.strip())
    if len(set(out)) != len(out):
        raise PlanInvalid("Allowed buckets must be distinct.")
    return out


def validate_plan(entries, allowed_categories) -> list:
    """Return normalized [{"category","hours"}] in allowed_categories order, or raise PlanInvalid."""
    allowed = _clean_allowed(allowed_categories)
    if not isinstance(entries, list) or not entries:
        raise PlanInvalid("Plan must be a non-empty list of buckets.")
    quarters = {}
    for e in entries:
        if not isinstance(e, dict) or set(e.keys()) != {"category", "hours"}:
            raise PlanInvalid("Each bucket needs exactly a category and hours.")
        cat, h = e["category"], e["hours"]
        if not isinstance(cat, str) or not cat.strip():
            raise PlanInvalid("Each bucket needs a category name.")
        cat = cat.strip()
        if cat not in allowed:
            raise PlanInvalid(f"Unknown bucket '{cat}'.")
        if cat in quarters:
            raise PlanInvalid(f"Bucket '{cat}' appears twice.")
        if isinstance(h, bool) or not isinstance(h, (int, float)):
            raise PlanInvalid(f"Hours for '{cat}' must be a number.")
        if isinstance(h, float) and not math.isfinite(h):
            raise PlanInvalid(f"Hours for '{cat}' must be a finite number.")
        if h < 0 or h > TOTAL_HOURS:  # bound first: h * 4 on 1e308 would overflow
            raise PlanInvalid(f"Hours for '{cat}' must be between 0 and {TOTAL_HOURS}.")
        q = round(h * 4)
        if abs(h * 4 - q) >= 1e-9:
            raise PlanInvalid(f"Hours for '{cat}' must be a multiple of 0.25.")
        quarters[cat] = q
    missing = [c for c in allowed if c not in quarters]
    if missing:
        raise PlanInvalid(f"Missing bucket: {', '.join(missing)}.")
    total = sum(quarters.values())
    if total != TOTAL_QUARTERS:
        gap = abs(TOTAL_QUARTERS - total)
        word = "short" if total < TOTAL_QUARTERS else "over"
        raise PlanInvalid(f"Hours total {_fmt(total)}, need {TOTAL_HOURS} ({_fmt(gap)} {word}).")
    return [{"category": c, "hours": quarters[c] / 4} for c in allowed]


def _stored_ok(d) -> bool:
    """A stored plan is only trusted if it still passes the same rules as a save."""
    try:
        if not (isinstance(d, dict) and isinstance(d.get("version"), int)
                and not isinstance(d["version"], bool) and d["version"] >= 1
                and isinstance(d.get("updated_at"), str) and isinstance(d.get("hours"), list)
                and d["hours"]):
            return False
        cats = []
        for e in d["hours"]:
            if not (isinstance(e, dict) and isinstance(e.get("category"), str) and e["category"].strip()):
                return False
            cats.append(e["category"].strip())
        validate_plan(d["hours"], cats)  # types, finiteness, quarter steps, total 168, no dupes
        return True
    except Exception:
        return False


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:  # corrupt / unreadable
        _log.warning("hours_plan: cannot read %s: %s", path, exc)
        return None
    if _stored_ok(d):
        return d
    _log.warning("hours_plan: %s is not a valid plan; ignoring it", path)
    return None


def load_plan():
    """Saved plan dict, or None. Falls back to the previous file when the main one is bad."""
    try:
        d = _plan_dir()
        return _read(os.path.join(d, _FILE)) or _read(os.path.join(d, _PREV))
    except Exception as exc:
        _log.warning("hours_plan: load failed: %s", exc)
        return None


def _write_tmp(directory, data: bytes) -> str:
    """Unique tmp in the same dir, flushed and fsynced. Removed again if the write fails."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="hours_plan.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        _unlink(tmp)
        raise
    return tmp


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def save_plan(entries, allowed_categories, base_version, now=None) -> dict:
    """Validate, check base_version against the stored version, then write atomically.

    Raises PlanInvalid, PlanConflict (nothing written) or PlanStorageError (disk write failed;
    tmp files removed, existing main and .prev untouched unless .prev was already refreshed).
    """
    hours = validate_plan(entries, allowed_categories)
    if base_version is not None and (isinstance(base_version, bool) or not isinstance(base_version, int)):
        raise PlanInvalid("base_version must be a whole number or None.")
    now = now or datetime.now(timezone.utc)
    with _LOCK:
        d = _plan_dir()
        main_path = os.path.join(d, _FILE)
        prev_path = os.path.join(d, _PREV)
        main, prev = _read(main_path), _read(prev_path)
        cur_v = max([x["version"] for x in (main, prev) if x], default=None)
        if cur_v is not None and base_version != cur_v:
            raise PlanConflict(cur_v)
        if cur_v is None and base_version is not None:
            raise PlanConflict(0)
        saved = {"version": (cur_v or 0) + 1,
                 "updated_at": now.astimezone(timezone.utc).isoformat(),
                 "hours": hours}
        tmps = []
        try:
            os.makedirs(d, exist_ok=True)
            # New main is fully written and fsynced first; only then is .prev refreshed (and only
            # from a good main), and main is replaced last.
            new_tmp = _write_tmp(d, json.dumps(saved, indent=2).encode("utf-8"))
            tmps.append(new_tmp)
            if main is not None:
                prev_tmp = _write_tmp(d, json.dumps(main, indent=2).encode("utf-8"))
                tmps.append(prev_tmp)
                os.replace(prev_tmp, prev_path)
            os.replace(new_tmp, main_path)
        except OSError as exc:
            raise PlanStorageError(f"Could not save the plan: {exc}") from exc
        finally:
            for t in tmps:
                _unlink(t)  # no-op for tmp files already renamed into place
        return saved
