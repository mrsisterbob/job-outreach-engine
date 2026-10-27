"""Decides which human pageviews alert Kevin, and builds the alert text. Pure.

No I/O, no clock of its own (`now` is passed in), no Telegram, no Flask. The in-memory window is
modelled on the site's resume_alert.py. The message carries only the time, the page path and the
referrer host: no IP, user agent or visitor id can reach it.
"""
from __future__ import annotations

import html
import threading

VISIT_ALERT_WINDOW_SECONDS = 600  # DESIGN CHOICE, not a measurement
VISIT_ALERT_HOURLY_CEILING = 10  # DESIGN CHOICE, not a measurement
VISIT_ALERT_MAX_TRACKED = 5000  # DESIGN CHOICE, not a measurement
VISIT_ALERT_STALE_SECONDS = 600  # DESIGN CHOICE, not a measurement
RESUME_PATH_PREFIX = "/resume/"  # DESIGN CHOICE, not a measurement
_HOUR_SECONDS = 3600
_PATH_CAP = 100
_OFF_VALUES = ("0", "false", "no", "off")


def is_alertable_path(path):
    """False for the resume PDF download path (resume_alert.py already alerts on it)."""
    return not str(path or "").lower().startswith(RESUME_PATH_PREFIX)


class VisitAlertGate:
    """One alert per distinct visitor per window, with an hourly ceiling on all alerts."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}
        self._alerts = []

    def reset(self):
        with self._lock:
            self._last.clear()
            self._alerts.clear()

    def admit(self, visitor_id, path, bot, view_epoch, now):
        """Return (ok, reason). Only an admitted view records anything."""
        with self._lock:
            for k in [k for k, t in self._last.items()
                      if now - t >= VISIT_ALERT_WINDOW_SECONDS]:
                del self._last[k]
            self._alerts = [t for t in self._alerts if now - t < _HOUR_SECONDS]
            if bot is not None:
                return False, "not_human"
            if not is_alertable_path(path):
                return False, "resume_path"
            if view_epoch is not None and now - view_epoch > VISIT_ALERT_STALE_SECONDS:
                return False, "stale"
            if visitor_id in self._last:
                return False, "repeat"
            if len(self._alerts) >= VISIT_ALERT_HOURLY_CEILING:
                return False, "ceiling"
            if len(self._last) >= VISIT_ALERT_MAX_TRACKED:
                return False, "table_full"
            if not visitor_id:
                return False, "no_visitor"
            self._last[visitor_id] = now
            self._alerts.append(now)
            return True, ""


def format_visit_alert(when_text, path, referrer):
    """Telegram HTML text: time, path (cut to 100 chars) and referrer host only."""
    esc = lambda s: html.escape(str(s or ""), quote=False)
    shown = str(path or "/")[:_PATH_CAP]
    host = str(referrer or "").strip()
    via = f"via {esc(host)}" if host else "direct"
    return (f"👀 <b>Someone opened montelattice.com</b>\n"
            f"<code>{esc(shown)}</code> · {via}\n{esc(when_text)}")


def visit_alerts_enabled(environ):
    """VISIT_ALERTS_ENABLED, default on; read per call."""
    raw = str(environ.get("VISIT_ALERTS_ENABLED", "") or "").strip().lower()
    return raw not in _OFF_VALUES
