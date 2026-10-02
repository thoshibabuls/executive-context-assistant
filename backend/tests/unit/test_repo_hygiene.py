"""Slice 0.1: secrets stay out of version control."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_env_file_is_git_ignored() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO, check=False)
    assert result.returncode == 0, ".env must be ignored by git"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_env_file_is_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"], cwd=REPO, capture_output=True, check=False
    )
    assert tracked.returncode != 0, ".env is tracked by git"


def test_env_example_contains_no_secret_values() -> None:
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line
    )
    assert values.get("GEMINI_API_KEY", "") == ""
    assert values.get("SENTRY_DSN", "") == ""
    assert not re.search(r"AIza[0-9A-Za-z_\-]{20,}", text)
