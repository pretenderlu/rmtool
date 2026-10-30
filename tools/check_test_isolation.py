"""Run every test module in its own process from outside the repository.

Catches tests that only pass because another module was imported first
(circular imports) or because the working directory is the repository root.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

RUNNER = (
    "import sys, unittest\n"
    "sys.path[:0] = [sys.argv[1], sys.argv[2]]\n"
    "result = unittest.main(module=None, argv=['isolated', sys.argv[3]], exit=False).result\n"
    "sys.exit(0 if result.wasSuccessful() else 1)\n"
)


def main() -> int:
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    failed = []
    with tempfile.TemporaryDirectory(prefix="rmtool-isolation-") as outside:
        for path in sorted(TESTS.glob("test_*.py")):
            result = subprocess.run(
                [sys.executable, "-c", RUNNER, str(ROOT), str(TESTS), path.stem],
                cwd=outside,
                env=env,
                capture_output=True,
                text=True,
            )
            if result.returncode:
                failed.append(path.stem)
                print(f"FAIL {path.stem}\n{result.stderr[-4000:]}", file=sys.stderr)
    if failed:
        print("Test modules that fail in isolation: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"PASS: {len(list(TESTS.glob('test_*.py')))} test modules pass in isolation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
