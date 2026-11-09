import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("mbt", ROOT / "scripts" / "measure_browser_titles.py")
mbt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mbt)
act = mbt.act

NOW = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
CANARY = "ZZ-CANARY-TITLE-4471 hidden page"
KW_CANARY = "zz-keyword-canary-4471"


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class Fake:
    def __init__(self, payload=None, exc=None, status=200):
        self.calls, self.payload, self.exc, self.status = [], payload, exc, status

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return Resp(self.payload, self.status)


def ev(app, title, dur):
    return {"duration": dur, "data": {"app": app, "title": title}}


def run(argv, payload=None, **kw):
    lines = []
    fake = kw.pop("fake", None) or Fake(payload if payload is not None else [[]])
    rc = mbt.main(argv, aw_post=fake, now=NOW, out=lines.append)
    return rc, "\n".join(lines), fake


def synthetic():
    return [[
        ev("msedge.exe", "Alpha Report - Microsoft Edge", 3600),
        ev("msedge.exe", "Beta Report Report", 1800),
        ev("msedge.exe", "", 360),
        ev("chrome.exe", "Gamma Alpha", 1800),
        ev("firefox.exe", "Delta Zulu", 900),
        ev("code.exe", "editor file", 7200),
        ev("LockApp.exe", "lock", 600),
    ]]


def write_kw(tmp_path, obj, raw=None):
    p = tmp_path / "kw.json"
    p.write_text(raw if raw is not None else json.dumps(obj), encoding="utf-8")
    return str(p)


def test_url_gate():
    rc, out, fake = run(["--aw-url", "http://example.com:5600"])
    assert rc == 2 and fake.calls == []
    rc, _, fake = run(["--aw-url", "https://localhost:5600"])
    assert rc == 2 and fake.calls == []
    for bad in ("http://localhost\n", "http://localhost:99999", "http://localhost\t"):
        rc, _, fake = run(["--aw-url", bad])
        assert rc == 2 and fake.calls == []
    rc, _, fake = run(["--aw-url", "http://[::1]:5600"])
    assert rc == 0
    rc, _, fake = run([])
    assert rc == 0 and len(fake.calls) == 1
    assert fake.calls[0][0] == "http://localhost:5600/api/0/query/"


def test_one_request_with_shared_query_and_week():
    rc, _, fake = run([])
    assert len(fake.calls) == 1
    body = fake.calls[0][1]["json"]
    q = body["query"]
    assert q[:3] == act.AW_QUERY[:3] and q[4:] == act.AW_QUERY[4:]
    assert q[3] == 'ev = merge_events_by_keys(win, ["app","title"]);'
    b = act.week_bounds(NOW)
    assert body["timeperiods"] == [f"{b['start']}/{b['end']}"]
    rc, _, fake = run(["--week", "last"])
    start, end = fake.calls[0][1]["json"]["timeperiods"][0].split("/")
    assert start == "2026-10-26T04:00:00Z"
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    hours = (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds() / 3600
    assert hours == 169


def test_query_shape_guard(monkeypatch):
    monkeypatch.setattr(act, "AW_QUERY", list(act.AW_QUERY[:3]) + ["x;", "y;"])
    with pytest.raises(AssertionError):
        mbt.build_title_query()


def test_totals():
    rc, out, _ = run([], synthetic())
    assert rc == 0
    assert "Browser time: 2.35 h across 4 distinct titles" in out
    assert "Empty title: 0.10 h" in out
    assert "Unreadable events skipped: 0" in out
    assert "editor" not in out and "lock" not in out.lower().replace("block", "")


def test_unreadable_skipped_and_counted():
    bad = [[
        ev("msedge.exe", "ok page", 3600),
        {"data": {"app": "msedge.exe", "title": "no dur"}},
        ev("msedge.exe", "neg", -5),
        ev("msedge.exe", "nan", float("nan")),
        ev("msedge.exe", "bool", True),
        ev("msedge.exe", 42, 100),
    ]]
    rc, out, _ = run([], bad)
    assert rc == 0
    assert "Unreadable events skipped: 5" in out
    assert "Browser time: 1.00 h across 1 distinct titles" in out


def test_top_words():
    rows = {"Alpha Report - Microsoft Edge": 3600, "Beta Report Report": 1800,
            "Gamma Alpha": 1800, "Delta Zulu": 900}
    words = dict(mbt.top_words(rows))
    assert "microsoft" not in words and "edge" not in words
    assert words["alpha"] == 5400
    assert words["report"] == 5400  # repeated inside one title counts once
    assert words["gamma"] == 1800
    order = [w for w, _ in mbt.top_words(rows)]
    assert order[:2] == ["alpha", "report"]  # tie -> alphabetical
    assert order[2] == "beta" and order[3] == "gamma"
    _, out, _ = run(["--top", "2"], synthetic())
    assert "alpha | 1.50 h | 64%" in out


def test_keywords_first_match_and_totals(tmp_path):
    kw = write_kw(tmp_path, {"One": ["ALPHA"], "Two": ["report", "zulu"]})
    rc, out, _ = run(["--keywords", kw], synthetic())
    assert rc == 0
    # One: Alpha Report (1.00) + Gamma Alpha (0.50); Two: Beta Report Report (0.50) + Delta Zulu (0.25)
    assert "One: 1.50 h" in out and "Two: 0.75 h" in out
    assert "explained: 2.25 h of 2.35 h (96%)" in out
    assert "still Browsing: 0.10 h" in out


def test_keywords_percent_exact(tmp_path):
    payload = [[ev("msedge.exe", "Job Board page", 3600), ev("msedge.exe", "Other thing", 3600)]]
    kw = write_kw(tmp_path, {"Jobs": ["job board"]})
    _, out, _ = run(["--keywords", kw], payload)
    assert "explained: 1.00 h of 2.00 h (50%)" in out
    assert "still Browsing: 1.00 h" in out
    assert "aw-watcher-web" not in out


def test_under_half_note(tmp_path):
    payload = [[ev("msedge.exe", "Job Board page", 3600), ev("msedge.exe", "Other thing", 7200)]]
    kw = write_kw(tmp_path, {"Jobs": ["job board"]})
    _, out, _ = run(["--keywords", kw], payload)
    assert "(33%)" in out and "aw-watcher-web" in out


def _bad_cases():
    c = KW_CANARY
    return [
        ("notjson", None, "not json {" + c),
        ("list", [c], None),
        ("21buckets", {f"b{i}": [c] for i in range(20)} | {"b20": [c]}, None),
        ("61kw", {"a": [c] * 30, "b": [c] * 31}, None),
        ("2char", {"a": ["ab", c]}, None),
        ("41char", {"a": ["x" * 41, c]}, None),
        ("nonstr", {"a": [5, c]}, None),
        ("blankname", {"  ": [c]}, None),
        ("longname", {"n" * 41: [c]}, None),
    ]


@pytest.mark.parametrize("label,obj,raw", _bad_cases(), ids=[c[0] for c in _bad_cases()])
def test_bad_keyword_files(tmp_path, capsys, label, obj, raw):
    kw = write_kw(tmp_path, obj, raw)
    assert KW_CANARY in Path(kw).read_text(encoding="utf-8")
    rc, out, fake = run(["--keywords", kw], synthetic())
    cap = capsys.readouterr()
    assert rc == 2 and fake.calls == []
    assert KW_CANARY not in out + cap.out + cap.err


def test_exactly_60_keywords_ok(tmp_path):
    kw = write_kw(tmp_path, {"a": ["abc"] * 30, "b": ["abd"] * 30})
    rc, _, _ = run(["--keywords", kw], synthetic())
    assert rc == 0


def test_note_uses_displayed_percent(tmp_path):
    payload = [[ev("msedge.exe", "job board", 4960), ev("msedge.exe", "other", 5040)]]
    kw = write_kw(tmp_path, {"Jobs": ["job board"]})
    _, out, _ = run(["--keywords", kw], payload)
    assert "(50%)" in out and "aw-watcher-web" not in out
    payload = [[ev("msedge.exe", "job board", 4940), ev("msedge.exe", "other", 5060)]]
    _, out, _ = run(["--keywords", kw], payload)
    assert "(49%)" in out and "aw-watcher-web" in out


def test_bucket_name_sanitised(tmp_path):
    kw = write_kw(tmp_path, {"Red\x1b[31m": ["alpha"]})
    _, out, _ = run(["--keywords", kw], synthetic())
    assert "\x1b" not in out and "Red [31m: " in out


def test_missing_keyword_file(tmp_path):
    rc, _, fake = run(["--keywords", str(tmp_path / "nope.json")], synthetic())
    assert rc == 2 and fake.calls == []


def test_titles_hidden_by_default_and_clamps():
    payload = [[ev("msedge.exe", CANARY + " " + "x" * 80, 3600)] +
               [ev("msedge.exe", f"page number {i}", 10) for i in range(60)]]
    _, out, _ = run([], payload)
    assert CANARY not in out
    _, out, _ = run(["--show-titles", "3"], payload)
    assert CANARY in out
    assert all(len(l.split(" | ", 1)[1]) <= 60 for l in out.splitlines() if l.startswith("1.00 h | "))
    _, out, _ = run(["--show-titles", "999"], payload)
    assert "Top 50 exact titles" in out
    _, out, _ = run(["--top", "0"], payload)
    assert len([l for l in out.splitlines() if re.search(r"\| \d+\.\d\d h \| \d+%$", l)]) == 1


def test_aw_down_and_bad_replies():
    rc, out, _ = run([], fake=Fake(exc=ConnectionError("x")))
    assert rc == 1 and "ActivityWatch is not running at http://localhost:5600." in out
    for bad in ({"a": 1}, "text", [1, 2]):
        rc, out, _ = run([], fake=Fake(payload=bad))
        assert rc == 1 and "Unexpected reply from ActivityWatch; nothing was counted." in out
    rc, out, _ = run([], fake=Fake(payload=[]))
    assert rc == 0 and "ActivityWatch returned no events for this week." in out
    rc, out, _ = run([], fake=Fake(payload=[[]]))
    assert rc == 0 and "no events" in out
    rc, out, _ = run([], fake=Fake(status=500))
    assert rc == 1


def test_writes_nothing_and_source_clean(tmp_path):
    before_t, before_r = sorted(p.name for p in tmp_path.iterdir()), sorted(p.name for p in ROOT.iterdir())
    _, _, fake = run(["--show-titles", "5"], synthetic())
    assert len(fake.calls) == 1
    assert before_t == sorted(p.name for p in tmp_path.iterdir())
    assert before_r == sorted(p.name for p in ROOT.iterdir())
    src = (ROOT / "scripts" / "measure_browser_titles.py").read_text(encoding="utf-8")
    for bad in ("/snapshots", "ingest", "ENGINE", ".write("):
        assert bad not in src
    assert not re.search(r"open\([^)]*['\"][wa+x]", src)
    top_level = [l for l in src.splitlines() if re.match(r"(import|from) ", l)]
    assert not any("requests" in l or "urllib" in l for l in top_level)


def test_final_line_last():
    for argv, payload in (([], synthetic()), (["--show-titles", "2"], synthetic()), ([], [])):
        rc, out, _ = run(argv, payload)
        assert rc == 0
        assert out.splitlines()[-1].startswith("Nothing was saved or sent.")
