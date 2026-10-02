"""Stop hook: refuse to end a turn that leaves the test suites red.

Every rule in .claude/commands/queue-run.md is advisory - prose a worker chooses to follow.
This one is not. The turn does not end until pytest is green, whatever the agent believes
about its own work.

It exists because of how this repo actually fails: a change passes its own spec while the
next real run reads back something different. A worker that skips the suite once, on a
Tuesday, leaves that broken for a week. Reading every diff catches it today; it will not
at 40k lines.

Contract (docs: code.claude.com/docs/en/hooks):
  stdin  - JSON with stop_hook_active, cwd, last_assistant_message, ...
  stdout - {"decision": "block", "reason": "..."} to keep the turn open, or nothing
  exit 0 either way; a non-zero exit is a broken hook, not a failed check

stop_hook_active is the loop guard. When it is true the previous block is already being
worked on, so blocking again would spin. Claude Code also overrides a Stop hook after 8
consecutive blocks, but we never want to reach that.

Written in Python, not the jq shell script the docs show: this machine has no jq, and a
hook that cannot parse its input fails open - which looks exactly like a passing check.
"""

import json
import subprocess
import sys

SUITES = ["test_main_integration.py", "test_pipeline_utils.py"]
TIMEOUT_SECONDS = 300


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # A malformed payload is a broken hook, not a red suite. Fail open rather than
        # wedging every turn in the session behind a parse error.
        return

    # Already blocked once this turn - Claude is fixing it. Blocking again spins.
    if payload.get("stop_hook_active"):
        return

    cwd = payload.get("cwd") or "."

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        block(f"The test suites did not finish within {TIMEOUT_SECONDS}s. Something is "
              "hanging - find it before ending the turn.")
        return
    except Exception as exc:
        # Could not run pytest at all. Say so rather than reporting a pass.
        block(f"Could not run the test suites: {exc}. The turn cannot be verified.")
        return

    if result.returncode == 0:
        return

    tail = failure_summary(result.stdout or result.stderr or "")
    block(
        "The test suites are RED, so this turn is not done.\n\n"
        f"{tail}\n\n"
        "Fix the failures and run "
        "`python -m pytest test_main_integration.py test_pipeline_utils.py -q` again. "
        "If the change is genuinely correct and the test encodes the old behavior, say so "
        "explicitly and explain why - do not delete or skip a test to get to green."
    )


def failure_summary(output, max_lines=30):
    """The part of pytest's output that names what broke.

    Taking a plain tail is wrong: with -q the run is mostly progress dots, and on a suite
    this size they pad the end of the capture, so the last 25 lines were dots and the
    reason handed back said nothing. Start at the FAILURES banner instead, and fall back
    to the lines that carry a node id or the final counts.
    """
    lines = output.strip().splitlines()

    for i, line in enumerate(lines):
        if "= FAILURES =" in line or "= ERRORS =" in line:
            return "\n".join(lines[i:i + max_lines])

    signal = [ln for ln in lines
              if ("::" in ln or " failed" in ln or " error" in ln)
              and not set(ln.strip()) <= {".", "F", "E", "s", "x", "%", "[", "]", " "}
              and not ln.strip().endswith("%]")]
    if signal:
        return "\n".join(signal[-max_lines:])

    return "\n".join(lines[-max_lines:])


def block(reason):
    json.dump({"decision": "block", "reason": reason}, sys.stdout)


if __name__ == "__main__":
    main()
