import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lint_commit_message.py"
_spec = importlib.util.spec_from_file_location("lint_commit_message", _SCRIPT)
assert _spec and _spec.loader
lcm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lcm)

PATTERNS = lcm.parse_patterns("# comment\nforbidden-token\nsigned-off-by:\\s*bot\n\n")


def test_parse_patterns_skips_blanks_and_comments() -> None:
    assert [p.pattern for p in PATTERNS] == ["forbidden-token", "signed-off-by:\\s*bot"]


@pytest.mark.parametrize(
    "message",
    [
        "feat: x\n\nFORBIDDEN-TOKEN in body",
        "fix: y\n\nSigned-off-by:   Bot <bot@example.com>",
        "",
        "# only a comment\n",
        "feat: " + "x" * 200,
    ],
)
def test_rejects_bad_messages(message: str) -> None:
    assert lcm.lint(message, PATTERNS)


@pytest.mark.parametrize(
    "message",
    [
        "feat(engine): add EU261 delay bands",
        "fix: handle missing arrival time\n\nSigned-off-by: Jane Doe <jane@example.com>",
    ],
)
def test_accepts_clean_messages(message: str) -> None:
    assert lcm.lint(message, PATTERNS) == []


def test_comment_lines_and_verbose_diff_are_ignored() -> None:
    msg = (
        "feat: clean\n"
        "# forbidden-token in a comment line\n"
        "# ------------------------ >8 ------------------------\n"
        "+ forbidden-token in the diff\n"
    )
    assert lcm.lint(msg, PATTERNS) == []


def test_patterns_come_from_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lcm.ENV_VAR, "alpha\nbeta")
    assert [p.pattern for p in lcm.load_patterns()] == ["alpha", "beta"]


def test_no_patterns_configured_means_format_checks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(lcm.ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert lcm.load_patterns() == []


def test_check_file_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(lcm.ENV_VAR, "forbidden-token")
    good = tmp_path / "good"
    good.write_text("feat: clean\n", encoding="utf-8")
    bad = tmp_path / "bad"
    bad.write_text("feat: x\n\nforbidden-token\n", encoding="utf-8")
    assert lcm.main([str(good)]) == 0
    assert lcm.main([str(bad)]) == 1


def _commit(repo: Path, message: str) -> None:
    (repo / "f.txt").write_text(message, encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "f.txt"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True, capture_output=True)


def test_check_range_flags_only_bad_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(lcm.ENV_VAR, "forbidden-token")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _commit(tmp_path, "feat: base")
    _commit(tmp_path, "feat: clean follow-up")
    monkeypatch.chdir(tmp_path)
    assert lcm.main(["--range", "HEAD~1..HEAD"]) == 0
    _commit(tmp_path, "feat: bad\n\nforbidden-token")
    assert lcm.main(["--range", "HEAD~2..HEAD"]) == 1
