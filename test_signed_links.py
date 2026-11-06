import pytest
from urllib.parse import parse_qs

import signed_links as sl

S = "s3cret"
NOW = 1_800_000_000


def _link(path="/money", now=NOW, **kw):
    q = parse_qs(sl.sign_path(S, path, now, **kw))
    return q["exp"][0], q["sig"][0]


def test_round_trip_and_expiry():
    exp, sig = _link()
    assert sl.verify_link(S, "/money", exp, sig, NOW)
    assert sl.verify_link(S, "/money", exp, sig, NOW + 899)
    assert not sl.verify_link(S, "/money", exp, sig, NOW + 900)
    assert not sl.verify_link(S, "/money", exp, sig, NOW + 5000)


def test_boundary():
    exp, sig = _link()
    e = int(exp)
    assert sl.verify_link(S, "/money", exp, sig, e - 1)
    assert not sl.verify_link(S, "/money", exp, sig, e)
    assert not sl.verify_link(S, "/money", exp, sig, e + 1)


def test_tampered_sig():
    exp, sig = _link()
    flipped = ("0" if sig[0] != "0" else "1") + sig[1:]
    for bad in (flipped, sig[:-1], "", None):
        assert not sl.verify_link(S, "/money", exp, bad, NOW)


def test_tampered_exp():
    exp, sig = _link()
    later = str(int(exp) + 1)
    assert not sl.verify_link(S, "/money", later, sig, NOW)


@pytest.mark.parametrize("p", ["/followups", "/money/", "/Money", "/money?x=1"])
def test_wrong_path(p):
    exp, sig = _link()
    assert not sl.verify_link(S, p, exp, sig, NOW)


def test_secrets():
    exp, sig = _link()
    assert not sl.verify_link("other", "/money", exp, sig, NOW)
    for bad in ("", None):
        with pytest.raises(ValueError):
            sl.sign_path(bad, "/money", NOW)
    e = NOW + 100
    blank_sig = sl.hmac.new(b"", f"/money\n{e}".encode(), "sha256").hexdigest()
    assert not sl.verify_link("", "/money", str(e), blank_sig, NOW)
    assert not sl.verify_link(None, "/money", str(e), blank_sig, NOW)


@pytest.mark.parametrize("bad", ["-5", "+900", " 900", "1e3", "٩٠٠", "", None, "9" * 40, [], 900])
def test_malformed_exp(bad):
    _, sig = _link()
    assert sl.verify_link(S, "/money", bad, sig, NOW) is False


@pytest.mark.parametrize("bad", ["é" * 64, b"abc", 123, ["x"], None])
def test_non_ascii_sig(bad):
    exp, _ = _link()
    assert sl.verify_link(S, "/money", exp, bad, NOW) is False


def test_ttl_clamp():
    exp, _ = _link(ttl=99999)
    assert int(exp) == NOW + sl.MAX_TTL_SECONDS
    for t in (0, -1):
        with pytest.raises(ValueError):
            sl.sign_path(S, "/money", NOW, ttl=t)


def test_far_future_forgery():
    e = NOW + 10_000
    sig = sl.make_signature(S, "/money", e)
    assert not sl.verify_link(S, "/money", str(e), sig, NOW)
