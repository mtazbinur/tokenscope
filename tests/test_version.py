"""Tests for the single source-of-truth version (scanner.VERSION)."""

import re
import subprocess
import sys
import unittest
from pathlib import Path

from scanner import VERSION

REPO_ROOT = Path(__file__).resolve().parent.parent


def _changelog_top_version():
    """Return the version from the first '## vX.Y.Z' heading in CHANGELOG.md."""
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    for line in changelog.splitlines():
        m = re.match(r"^## v(\d+\.\d+\.\d+)(\s|$)", line)
        if m:
            return m.group(1)
    return None


class TestVersion(unittest.TestCase):
    def test_version_is_strict_semver(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_cli_reexports_same_version(self):
        # cli.py imports VERSION from scanner so `cli.py --version` reports it.
        import cli
        self.assertEqual(cli.VERSION, VERSION)

    def test_matches_changelog_heading(self):
        top = _changelog_top_version()
        self.assertEqual(
            top, VERSION,
            f"scanner.VERSION ({VERSION}) != top CHANGELOG heading ({top}). "
            "Bump both in lockstep.",
        )

    def test_cli_version_flag(self):
        """`python cli.py --version` prints the version and exits 0."""
        result = subprocess.run(
            [sys.executable, "cli.py", "--version"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), VERSION)


if __name__ == "__main__":
    unittest.main()
