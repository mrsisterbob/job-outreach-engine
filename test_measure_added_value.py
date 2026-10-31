"""measure_added_value tests: fake requests, made-up companies, no network."""
import importlib.util
import json
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="mav_test_")
os.environ["JOBS_DB_PATH"] = os.path.join(_TMP, "test.db")  # before import main

import main as m  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "measure_added_value", Path(__file__).parent / "scripts" / "measure_added_value.py")
mav = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mav)


class Resp:
    def __init__(self, status=200, data=None, headers=None, text="", url=None):
        self.status_code, self._data, self.headers, self.text = status, data, headers or {}, text
        self.url = url

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeRequests:
    """routes: list of (substring, Resp); first match wins; anything else is a 404."""

    def __init__(self, routes=()):
        self.routes, self.urls = list(routes), []

    def get(self, url, **kw):
        self.urls.append(url)
        for sub, resp in self.routes:
            if sub in url:
                return resp
        return Resp(404)


def row(key, name, tier="top", source="guess", domain=None):
    slug = key
    return {"company_key": key, "name": name, "tier": tier, "contacts": "9",
            "domain": domain or f"{slug}.com", "domain_source": source, "slug": slug}


def run_one(r, routes, free=None):
    cache = Path(tempfile.mkdtemp(prefix="mavc_")) / "c.json"
    j = mav.run([r], free or {}, FakeRequests(routes), cache, 0)
    return j[0], cache


def test_workable_slug_hit_is_an_extra():
    j, _ = run_one(row("zorb", "Zorb"), [("workable.com/api/v1/widget/accounts/zorb",
                                          Resp(data={"name": "Zorb", "jobs": [{}]}))])
    assert j["extras"] == ["workable"] and not j["already_resolved"]
    assert j["ceiling"] == "probe only"


def test_careers_redirect_to_workday_has_triple_shape():
    loc = "https://acme.wd5.myworkdayjobs.com/en-US/AcmeCareers"
    j, _ = run_one(row("acme", "Acme"), [("acme.com/careers", Resp(302, headers={"Location": loc}))])
    assert j["extras"] == ["workday"]
    assert j["triple"] == "acme/wd5/AcmeCareers"
    assert j["ceiling"] == "fetchable today"


def test_free_resolved_company_is_not_an_extra():
    r = row("okto", "Okto")
    j, _ = run_one(r, [("boards-api.greenhouse.io/v1/boards/okto", Resp(data={"jobs": [{}]}))],
                   free={"okto": "greenhouse"})
    assert j["already_resolved"] and j["extras"] == []
    assert "already resolved" in mav.report_lines([j])[0]


def test_email_and_guess_domains_counted_separately():
    wk = lambda s: [(f"accounts/{s}", Resp(data={"name": s, "jobs": [{}]}))]  # noqa: E731
    a = row("zorb", "Zorb", source="email", domain="zorbco.com")
    b = row("quux", "Quux", source="guess")
    ja, _ = run_one(a, [("icims", Resp(200))])
    jb, _ = run_one(b, [("quux.com/careers", Resp(302, headers={"Location": "https://quux.icims.com/jobs"}))])
    ja["domain_source"], ja["extras"] = "email", ["workable"]
    jb2 = dict(jb)
    s = mav.summarize([ja, jb2])["all"]
    assert s["by_domain_source"] == {"email": 1, "guess": 1}
    assert jb["domain_source"] == "guess" and jb["extras"] == ["icims"]
    del wk


def test_ceiling_buckets_sum_to_companies_and_icims_is_no_fetcher():
    rows = [row("a1", "A1"), row("b1", "B1"), row("c1", "C1"), row("d1", "D1")]
    routes = [("a1.com/careers", Resp(302, headers={"Location": "https://a1.icims.com/jobs"})),
              ("lever.co/v0/postings/b1", Resp(data=[{}]))]
    cache = Path(tempfile.mkdtemp(prefix="mavc_")) / "c.json"
    joined = mav.run(rows, {}, FakeRequests(routes), cache, 0)
    c = mav.summarize(joined)["all"]["ceiling"]
    assert sum(c.values()) == 4
    assert c["no fetcher"] == 1 and c["fetchable today"] == 1
    assert joined[0]["ceiling"] == "no fetcher"


def test_fetcher_systems_match_main():
    for s in mav.VENDORS + mav.PROBE_ONLY:
        assert hasattr(m, f"fetch_{s}_jobs") == (s in mav.FETCHER_SYSTEMS), s


def test_no_answer_company_reported_and_not_cached():
    j, cache = run_one(row("slow", "Slow"), [("greenhouse", Resp(503))])
    assert j["no_answer"]
    assert "no answer" in mav.report_lines([j])[0]
    assert not cache.exists() or "slow" not in json.loads(cache.read_text())


def test_result_out_refuses_inside_repo_and_writes_outside(tmp_path, capsys):
    sample = tmp_path / "s.csv"
    sample.write_text("# tiers top=1 mid=0 tail=0\ncompany_key,name,tier,contacts,domain,domain_source,slug\n"
                      "zorb,Zorb,top,9,zorb.com,guess,zorb\n", encoding="utf-8")
    free = tmp_path / "f.csv"
    free.write_text("company_key,system,token\n", encoding="utf-8")
    inside = Path(mav.ROOT) / "should_not_exist.csv"
    assert mav.main(["--sample", str(sample), "--free", str(free), "--result-out", str(inside)],
                    requests=FakeRequests()) == 2
    assert not inside.exists()
    out = tmp_path / "r.csv"
    fake = FakeRequests([("workable.com/api/v1/widget/accounts/zorb", Resp(data={"name": "Z", "jobs": [{}]}))])
    assert mav.main(["--sample", str(sample), "--free", str(free), "--result-out", str(out), "--pause", "0"],
                    requests=fake) == 0
    assert out.read_text().splitlines() == ["company_key,system,token", "zorb,workable,zorb"]
    assert mav.main(["--sample", str(sample), "--free", str(free), "--result-out", str(out)],
                    requests=fake) == 2
