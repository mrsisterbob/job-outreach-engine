"""measure_free_resolver tests: fake HTTP routed by host, real temp DB, made-up companies."""
import importlib.util
import json
import os
import sqlite3
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mfr_test_")
os.environ["JOBS_DB_PATH"] = os.path.join(_TMP, "test.db")  # before import main (main.py reads it at import)

import pytest  # noqa: E402

import main as m  # noqa: E402
import source_scoring as ss  # noqa: E402
import warm_sample as ws  # noqa: E402

ROOT = Path(ws.__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mfr", ROOT / "scripts" / "measure_free_resolver.py")
mfr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mfr)


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def fake_http(routes, seen=None):
    """routes: {(slug, system): Resp or Exception}; default 404."""
    def get(url, *a, **kw):
        system = mfr._system_of(url)
        slug = url.split("/boards/")[1].split("/")[0] if system == "greenhouse" else \
            url.split("/postings")[0].split("/companies/")[-1] if system == "smartrecruiters" else \
            url.split("?")[0].rstrip("/").split("/")[-1]
        if seen is not None:
            seen.append((slug, system, kw.get("headers")))
        r = routes.get((slug, system), Resp(404))
        if isinstance(r, Exception):
            raise r
        return r
    return get


def row(name, tier="top"):
    return {"company_key": name.lower().replace(" ", ""), "name": name, "tier": tier}


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    m.init_db()
    monkeypatch.setattr(mfr.time, "sleep", lambda s: None, raising=False)
    yield


def db_state():
    with sqlite3.connect(m.DB_PATH) as c:
        return (c.execute("SELECT * FROM company_identities").fetchall(),
                c.execute("SELECT * FROM search_filters").fetchall())


def measure(rows, routes, monkeypatch, seen=None):
    monkeypatch.setattr(m.requests, "get", fake_http(routes, seen))
    return mfr.run_measure(rows, m, pause=0.4, sleep=lambda s: None)


def test_greenhouse_hit_resolves_and_csv_row(monkeypatch, tmp_path):
    out = measure([row("Zorblat Labs")], {("zorblatlabs", "greenhouse"): Resp(200, {"jobs": [1]})}, monkeypatch)
    r = out["results"][0]
    assert (r["status"], r["system"], r["token"]) == ("resolved", "greenhouse", "zorblatlabs")
    p = tmp_path / "r.csv"
    mfr.write_result(out["results"], p)
    assert p.read_text().splitlines() == ["company_key,system,token", "zorblatlabs,greenhouse,zorblatlabs"]


def test_all_404_is_clean_miss(monkeypatch):
    out = measure([row("Quimby Works")], {}, monkeypatch)
    assert out["results"][0]["status"] == "clean miss"
    assert out["requests"] == 4


def test_lever_503_is_error_not_miss(monkeypatch):
    out = measure([row("Quimby Works")], {("quimbyworks", "lever"): Resp(503)}, monkeypatch)
    assert out["results"][0]["status"] == "error"
    assert out["per_system"]["lever"]["failure"] == 1


def test_exception_is_error(monkeypatch):
    out = measure([row("Quimby Works")], {("quimbyworks", "ashby"): TimeoutError("x")}, monkeypatch)
    assert out["results"][0]["status"] == "error"


def test_smartrecruiters_zero_vs_three(monkeypatch):
    out = measure([row("Plinko Corp")], {("plinkocorp", "smartrecruiters"): Resp(200, {"totalFound": 0})}, monkeypatch)
    assert out["results"][0]["status"] == "clean miss"
    assert out["sr_zero"] == 1
    out = measure([row("Plinko Corp")], {("plinkocorp", "smartrecruiters"): Resp(200, {"totalFound": 3})}, monkeypatch)
    assert out["results"][0]["status"] == "resolved"
    assert out["results"][0]["system"] == "smartrecruiters"


def test_cold_run_probes_slug_already_in_real_filter(monkeypatch):
    with sqlite3.connect(m.DB_PATH) as c:
        c.execute("INSERT OR REPLACE INTO search_filters (key, value_json) VALUES (?, ?)",
                  ("ats_company_slugs", json.dumps(["zorblatlabs"])))
    assert "zorblatlabs" in m.get_filter("ats_company_slugs", [])
    seen = []
    out = measure([row("Zorblat Labs")], {("zorblatlabs", "lever"): Resp(200, [{"id": 1}])}, monkeypatch, seen)
    assert out["results"][0]["status"] == "resolved"
    assert seen  # probed despite being "already tracked"
    assert "zorblatlabs" in m.get_filter("ats_company_slugs", [])  # real getter restored


def test_run_writes_nothing(monkeypatch):
    before = db_state()
    orig = (m.set_filter, m.upsert_company_identity, m.get_filter, m.crm_post, m.crm_get, m.requests.get)
    out = measure([row("Zorblat Labs")], {("zorblatlabs", "greenhouse"): Resp(200, {"jobs": [1]})}, monkeypatch)
    assert out["filter_writes"] == [("ats_company_slugs", ["zorblatlabs"])]  # recorder saw the hit
    assert db_state() == before
    assert (m.set_filter, m.upsert_company_identity, m.get_filter, m.crm_post, m.crm_get) == orig[:5]


def test_crm_stubs_raise_during_run(monkeypatch):
    def sneaky(name, errors=None):
        m.crm_post({"action": "x"})
    monkeypatch.setattr(m, "auto_expand_ats_slug", sneaky)
    monkeypatch.setattr(m.requests, "get", fake_http({}))
    with pytest.raises(RuntimeError, match="read-only"):
        mfr.run_measure([row("Zorblat Labs")], m, sleep=lambda s: None)


def test_non_company_is_skipped(monkeypatch):
    seen = []
    out = measure([row("cousin of a friend")], {}, monkeypatch, seen)
    assert out["results"][0]["status"] == "skipped"
    assert seen == []


def test_user_agent_pause_and_counts(monkeypatch):
    seen, pauses = [], []
    monkeypatch.setattr(m.requests, "get", fake_http({}, seen))
    out = mfr.run_measure([row("Quimby Works", "top"), row("Plinko Corp", "tail")], m, pause=0.4,
                          sleep=pauses.append)
    assert all(h["User-Agent"] == mfr.UA for _, _, h in seen)
    assert pauses == [0.4] * 7
    lines = mfr.report_lines(out)
    assert any(l.startswith("TIER tail n=1") for l in lines)
    assert "REQUESTS 8" in lines


def test_csv_round_trips_through_source_scoring(monkeypatch, tmp_path):
    out = measure([row("Zorblat Labs")], {("zorblatlabs", "greenhouse"): Resp(200, {"jobs": [1]})}, monkeypatch)
    p = tmp_path / "r.csv"
    mfr.write_result(out["results"], p)
    sample = [{"company_key": "zorblatlabs", "name": "Zorblat Labs", "tier": "top", "contacts": "1",
               "domain": "", "domain_source": "", "slug": "zorblatlabs"}]
    answers, unjoined = ss.read_result(p, ss.Columns(), sample, normalizer=lambda s: s)
    assert answers == {"zorblatlabs": ("greenhouse", "zorblatlabs")} and unjoined == 0


def test_main_refuses_overwrite_and_repo_paths(tmp_path):
    rows = [{"company_key": "a", "name": "A Co", "tier": t, "contacts": 1, "domain": "", "domain_source": "", "slug": "a"}
            for t in ws.TIERS]
    sample = tmp_path / "s.csv"
    ok, why = ws.write_sample(rows, sample, repo_root=tmp_path / "elsewhere")
    assert ok, why
    existing = tmp_path / "o.csv"
    existing.write_text("x")
    assert mfr.main(["--sample", str(sample), "--out", str(existing)], m=m) == 2
    assert mfr.main(["--sample", str(sample), "--out", str(ROOT / "o.csv")], m=m) == 2


def test_non_json_200_is_error(monkeypatch):
    out = measure([row("Quimby Works")], {("quimbyworks", "greenhouse"): Resp(200, None)}, monkeypatch)
    assert out["results"][0]["status"] == "error"


def test_429_is_error(monkeypatch):
    out = measure([row("Quimby Works")], {("quimbyworks", "ashby"): Resp(429)}, monkeypatch)
    assert out["results"][0]["status"] == "error"


def test_unexpected_exception_is_error_and_run_continues(monkeypatch):
    real = m.auto_expand_ats_slug

    def flaky(name, errors=None):
        if name == "Boom Inc":
            raise ValueError("x")
        return real(name, errors=errors)
    monkeypatch.setattr(m, "auto_expand_ats_slug", flaky)
    monkeypatch.setattr(m.requests, "get", fake_http({}))
    out = mfr.run_measure([row("Boom Inc"), row("Quimby Works")], m, sleep=lambda s: None)
    assert [r["status"] for r in out["results"]] == ["error", "clean miss"]


def test_ctrl_c_keeps_what_was_measured(monkeypatch):
    real = m.auto_expand_ats_slug

    def stop(name, errors=None):
        if name == "Second Co":
            raise KeyboardInterrupt
        return real(name, errors=errors)
    monkeypatch.setattr(m, "auto_expand_ats_slug", stop)
    monkeypatch.setattr(m.requests, "get", fake_http({}))
    out = mfr.run_measure([row("First Co"), row("Second Co")], m, sleep=lambda s: None)
    assert len(out["results"]) == 1 and out["interrupted"]
    assert any(l.startswith("INTERRUPTED") for l in mfr.report_lines(out))


def test_main_success_path(monkeypatch, tmp_path, capsys):
    rows = [{"company_key": "zorblatlabs", "name": "Zorblat Labs", "tier": t, "contacts": 1,
             "domain": "", "domain_source": "", "slug": "zorblatlabs"} for t in ws.TIERS]
    sample = tmp_path / "s.csv"
    ok, why = ws.write_sample(rows, sample, repo_root=tmp_path / "elsewhere")
    assert ok, why
    monkeypatch.setattr(m.requests, "get", fake_http({("zorblatlabs", "greenhouse"): Resp(200, {"jobs": [1]})}))
    before = db_state()
    outp = tmp_path / "out" / "r.csv"
    assert mfr.main(["--sample", str(sample), "--out", str(outp)], m=m, sleep=lambda s: None) == 0
    printed = capsys.readouterr().out
    assert "ALL n=3 resolved=3" in printed and "User-Agent" in printed
    assert outp.read_text().splitlines()[0] == "company_key,system,token"
    assert db_state() == before
    assert not (ROOT / "r.csv").exists()


def test_main_csv_write_failure_prints_after_report(monkeypatch, tmp_path, capsys):
    rows = [{"company_key": "a", "name": "Quimby Works", "tier": t, "contacts": 1,
             "domain": "", "domain_source": "", "slug": "a"} for t in ws.TIERS]
    sample = tmp_path / "s.csv"
    ws.write_sample(rows, sample, repo_root=tmp_path / "elsewhere")
    monkeypatch.setattr(m.requests, "get", fake_http({}))

    def boom(*a, **k):
        raise PermissionError("no")
    monkeypatch.setattr(mfr, "write_result", boom)
    assert mfr.main(["--sample", str(sample), "--out", str(tmp_path / "r.csv")], m=m, sleep=lambda s: None) == 1
    out = capsys.readouterr().out
    assert out.index("ALL n=3") < out.index("could not write")
