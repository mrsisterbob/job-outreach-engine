"""Score board sources against the hand-labelled truth for the 60-company sample.

  python scripts/score_source.py --sample S.csv --truth T.csv --result name=path [--result ...]
        [--company-col C] [--domain-col D] [--system-col S] [--token-col T] [--url-col U]
        [--cost name=USD | USD] [--seconds name=S | S]
  python scripts/score_source.py --sample S.csv --init-truth OUT.csv

Column flags apply to every --result file. A bare --cost/--seconds number is accepted with exactly
one --result. Nothing is written except --init-truth. Exit 2 on a bad argument or column.
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import source_scoring as ss  # noqa: E402
import warm_sample  # noqa: E402


def _per_source(values, names, flag):
    out = {}
    for v in values or []:
        name, sep, num = v.rpartition("=")
        if not sep:
            if len(names) != 1:
                raise ValueError(f"{flag} {v!r}: use name=value when scoring several sources")
            name = names[0]
        if name not in names:
            raise ValueError(f"{flag}: no --result named {name!r}")
        try:
            out[name] = float(num)
        except ValueError:
            raise ValueError(f"{flag}: {num!r} is not a number") from None
    return out


def main(argv=None, normalizer=None, repo_root=ss.REPO_ROOT):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--truth")
    ap.add_argument("--result", action="append", default=[])
    ap.add_argument("--init-truth")
    ap.add_argument("--company-col")
    ap.add_argument("--domain-col")
    ap.add_argument("--system-col")
    ap.add_argument("--token-col")
    ap.add_argument("--url-col")
    ap.add_argument("--cost", action="append")
    ap.add_argument("--seconds", action="append")
    args = ap.parse_args(argv)
    try:
        sample = warm_sample.read_sample(args.sample)
        if args.init_truth:
            ok, reason = ss.write_truth_template(sample, args.init_truth, repo_root=repo_root)
            print(reason if not ok else f"wrote {len(sample)} rows to {args.init_truth}")
            return 0 if ok else 1
        if not args.truth or not args.result:
            raise ValueError("--truth and at least one --result name=path are required")
        sources = {}
        for spec in args.result:
            name, sep, path = spec.partition("=")
            if not sep or not name or not path:
                raise ValueError(f"--result {spec!r}: expected name=path")
            sources[name] = path
        names = list(sources)
        cost = _per_source(args.cost, names, "--cost")
        seconds = _per_source(args.seconds, names, "--seconds")
        mapped = any([args.company_col, args.domain_col, args.system_col, args.token_col, args.url_col])
        if mapped:
            # No join flag: keep the default company_key join column; a file without it exits 2
            # naming its header (never silently score every row as unjoined).
            company = args.company_col or (None if args.domain_col else "company_key")
            cols = ss.Columns(company=company, domain=args.domain_col,
                              system=args.system_col, token=args.token_col, url=args.url_col)
        else:
            cols = ss.Columns()
        if normalizer is None and cols.company:
            logging.disable(logging.CRITICAL)
            import main as m  # heavy import; used only for normalize_company_for_match
            normalizer = m.normalize_company_for_match
        truth, blank = ss.read_truth(args.truth)
        scores = {}
        for name, path in sources.items():
            answers, _unjoined = ss.read_result(path, cols, sample, normalizer=normalizer)
            scores[name] = ss.score(sample, truth, answers)
            scores[name]["unlabelled"] += _unjoined
    except (ValueError, OSError) as e:  # ColumnError is a ValueError
        print(str(e))
        return 2
    for line in ss.format_report(scores, cost=cost, seconds=seconds, not_labelled=blank):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
