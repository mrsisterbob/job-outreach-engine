"""Build the fixed 60-company Carmen Warm sample (20 top, 20 mid, 20 tail) outside the repo.

  python scripts/build_warm_sample.py --out <path outside the repo> [--warm PATH] [--force]

Refuses to overwrite an existing file unless --force. Prints counts only, never names.
"""
import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import warm_sample  # noqa: E402

DEFAULT_WARM = "C:/Users/kjmil/Downloads/Job_Outreach_CRM - Carmen Warm.tsv"


def main(argv=None, normalizer=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--warm", default=DEFAULT_WARM)
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if normalizer is None:
        logging.disable(logging.CRITICAL)
        import main as m  # heavy import; used only for normalize_company_for_match
        normalizer = m.normalize_company_for_match
    code, lines = warm_sample.run(args.warm, args.out, force=args.force, normalizer=normalizer)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
