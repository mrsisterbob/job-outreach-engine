"""Personal money-and-time snapshot store.

One JSON file per snapshot in a directory on the persistent disk (never the jobs SQLite DB).
Keeps the newest N, and refuses any snapshot carrying merchant-level or per-transaction keys.
Pure stdlib.
"""
import json
import os
import re
from datetime import datetime, timezone

SNAPSHOT_KEEP = 30
_FILE_PREFIX = "snapshot_"
_FILE_SUFFIX = ".json"
_FORBIDDEN = ("merchant", "payee", "vendor", "transaction", "txn",
              "counterparty", "memo", "line_item")


class SnapshotRejected(ValueError):
    """Snapshot carries a forbidden key or is not storable."""


def snapshot_dir() -> str:
    d = os.environ.get("PERSONAL_SNAPSHOT_DIR")
    if d and d.strip():
        return d.strip()
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "personal_snapshots")


def snapshot_keep() -> int:
    raw = os.environ.get("PERSONAL_SNAPSHOT_KEEP")
    if raw is None:
        return SNAPSHOT_KEEP
    try:
        n = int(raw.strip())
    except ValueError:
        return SNAPSHOT_KEEP
    return n if n >= 1 else SNAPSHOT_KEEP


def _normalize_key(key) -> str:
    s = str(key)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)  # split camelCase: lineItem -> line_Item
    return re.sub(r"[^a-z0-9]+", "_", s.lower())


def _walk(node, path):
    if isinstance(node, dict):
        for k, v in node.items():
            kpath = f"{path}.{k}" if path else str(k)
            norm = _normalize_key(k)
            for bad in _FORBIDDEN:
                if bad in norm:
                    raise SnapshotRejected(f"forbidden key '{kpath}' (matches '{bad}')")
            _walk(v, kpath)
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            _walk(v, f"{path}[{i}]")


def validate_snapshot(snapshot) -> None:
    if not isinstance(snapshot, dict):
        raise SnapshotRejected("snapshot must be a dict")
    try:
        _walk(snapshot, "")
    except RecursionError:
        raise SnapshotRejected("snapshot is self-referencing or nested too deeply")
    try:
        json.dumps(snapshot, indent=2, sort_keys=True)  # same arguments as the write
    except RecursionError:
        raise SnapshotRejected("snapshot is self-referencing or nested too deeply")
    except (TypeError, ValueError) as e:
        raise SnapshotRejected(f"snapshot is not JSON-serializable: {e}")


def list_snapshots() -> list:
    d = snapshot_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    names = [n for n in names if n.startswith(_FILE_PREFIX) and n.endswith(_FILE_SUFFIX)]
    return [os.path.join(d, n) for n in sorted(names)]


def save_snapshot(snapshot: dict, *, now=None) -> str:
    validate_snapshot(snapshot)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S%f")
    record = dict(snapshot)
    record["saved_at"] = now.isoformat()
    payload = json.dumps(record, indent=2, sort_keys=True)

    d = snapshot_dir()
    os.makedirs(d, exist_ok=True)
    counter = 0
    while True:
        path = os.path.join(d, f"{_FILE_PREFIX}{stamp}_{counter:03d}{_FILE_SUFFIX}")
        if not os.path.exists(path):
            break
        counter += 1
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

    existing = list_snapshots()
    excess = len(existing) - snapshot_keep()
    for old in existing:
        if excess <= 0:
            break
        if os.path.abspath(old) == os.path.abspath(path):
            continue
        try:
            os.remove(old)
            excess -= 1
        except OSError:
            pass
    return path


def load_latest():
    for path in reversed(list_snapshots()):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None
