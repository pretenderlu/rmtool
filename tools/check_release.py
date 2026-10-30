"""Check that a release tag matches APP_VERSION and has bilingual release notes.

Usage: python tools/check_release.py [TAG]
Without TAG, GITHUB_REF_NAME is used when the workflow runs for a tag;
otherwise only the notes for the current APP_VERSION are checked.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def app_version() -> str:
    tree = ast.parse((ROOT / "rmtool.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit("APP_VERSION not found in rmtool.py")


def problems(tag: str | None) -> list[str]:
    version = app_version()
    expected = f"v{version}"
    found = []
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        found.append(f"APP_VERSION {version!r} is not MAJOR.MINOR.PATCH")
    if tag is not None and tag != expected:
        found.append(f"tag {tag} does not match APP_VERSION {version} (expected {expected})")
    notes = ROOT / "docs" / "releases" / f"{expected}.md"
    if not notes.is_file():
        found.append(f"missing release notes {notes.relative_to(ROOT).as_posix()}")
    else:
        text = notes.read_text(encoding="utf-8")
        if not text.startswith(f"# rmtool {expected}\n"):
            found.append(f"{notes.name} must start with '# rmtool {expected}'")
        for heading in ("## 中文", "## English"):
            if f"\n{heading}\n" not in text:
                found.append(f"{notes.name} is missing the '{heading}' section")
        if "revision" in text.casefold():
            found.append(f"{notes.name} mentions 'revision'")
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    if project.get("version") != version:
        found.append(f"pyproject.toml version {project.get('version')!r} differs from APP_VERSION {version}")
    for readme in ("README.md", "README.en.md"):
        link = f"[{expected}](https://github.com/pretenderlu/rmtool/releases/tag/{expected})"
        if link not in (ROOT / readme).read_text(encoding="utf-8"):
            found.append(f"{readme} does not link the current release {expected}")
    return found


def main(argv: list[str]) -> int:
    tag = argv[1] if len(argv) > 1 else None
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")
    found = problems(tag)
    for item in found:
        print(f"ERROR: {item}", file=sys.stderr)
    if not found:
        print(f"PASS: release inputs for v{app_version()} are consistent.")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
