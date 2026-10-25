"""Commit-message lint, used as a commit-msg hook and in CI.

Checks:
  * the subject line is present and at most MAX_SUBJECT characters;
  * the message matches none of the disallowed patterns (case-insensitive regexes, one per line).

Disallowed patterns are site configuration, not source: they are read from the
COMMIT_MSG_DISALLOWED environment variable, or else from .git/info/commit-msg-disallowed.

Usage:
  lint_commit_message.py .git/COMMIT_EDITMSG     (commit-msg hook)
  lint_commit_message.py --range BASE..HEAD      (CI over a pushed range)

Standard library only, so CI can run it without installing the project.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MAX_SUBJECT = 100
ENV_VAR = "COMMIT_MSG_DISALLOWED"
LOCAL_FILE = Path(".git") / "info" / "commit-msg-disallowed"

# git leaves this sentinel in verbose commit templates; everything below it is diff, not message.
_SCISSORS = "# ------------------------ >8 ------------------------"


def parse_patterns(text: str) -> list[re.Pattern[str]]:
    lines = (line.strip() for line in text.splitlines())
    return [re.compile(line, re.IGNORECASE) for line in lines if line and not line.startswith("#")]


def load_patterns() -> list[re.Pattern[str]]:
    if os.environ.get(ENV_VAR, "").strip():
        return parse_patterns(os.environ[ENV_VAR])
    if LOCAL_FILE.is_file():
        return parse_patterns(LOCAL_FILE.read_text(encoding="utf-8"))
    return []


def strip_comments(message: str) -> str:
    body = message.split(_SCISSORS, 1)[0]
    return "\n".join(line for line in body.splitlines() if not line.startswith("#")).strip()


def lint(message: str, patterns: list[re.Pattern[str]]) -> list[str]:
    """Return a list of problems with a commit message (empty if it is fine)."""
    text = strip_comments(message)
    subject = text.splitlines()[0] if text else ""
    problems = []
    if not subject.strip():
        problems.append("empty subject line")
    elif len(subject) > MAX_SUBJECT:
        problems.append(f"subject longer than {MAX_SUBJECT} characters")
    problems += [f"disallowed pattern: {p.pattern}" for p in patterns if p.search(text)]
    return problems


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def check_range(rev_range: str, patterns: list[re.Pattern[str]]) -> int:
    bad = 0
    for sha in _git("rev-list", rev_range).split():
        problems = lint(_git("log", "-1", "--format=%B", sha), patterns)
        if problems:
            bad += 1
            print(f"{sha[:12]}: {'; '.join(problems)}", file=sys.stderr)
    return 1 if bad else 0


def check_file(path: Path, patterns: list[re.Pattern[str]]) -> int:
    problems = lint(path.read_text(encoding="utf-8"), patterns)
    if problems:
        print(f"Commit message rejected: {'; '.join(problems)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Commit-message lint.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("message_file", nargs="?", type=Path, help="commit message file")
    group.add_argument("--range", dest="rev_range", help="git revision range, e.g. BASE..HEAD")
    args = parser.parse_args(argv)
    patterns = load_patterns()
    if args.rev_range:
        return check_range(args.rev_range, patterns)
    return check_file(args.message_file, patterns)


if __name__ == "__main__":
    sys.exit(main())
