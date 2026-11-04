"""Pinned native build inputs, independent of checkout and temporary paths."""

import hashlib
import json
import os
import shutil
import sys
import zlib
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name("native_build.lock.json")


def verify_tool(name, path):
    path = Path(path)
    if not path.is_file():
        found = shutil.which(str(path))
        if not found:
            raise RuntimeError(f"Missing pinned build tool: {name}")
        path = Path(found)
    expected = json.loads(LOCK.read_text(encoding="utf-8"))["tools"][name]
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError(f"Unverified build tool: {name}: {path}")
    return path.resolve()


def verify_archive_runtime():
    if sys.version_info[:2] != (3, 12) or zlib.ZLIB_RUNTIME_VERSION != "1.3.1":
        raise RuntimeError("Resource builds require Python 3.12 and zlib 1.3.1")


def verify_carrier_hashtab(path, package):
    expected = package.file("exthome/qt-resource-rebuilder/hashtab")
    data = Path(path).read_bytes()
    if len(data) != expected.size or hashlib.sha256(data).hexdigest() != expected.sha256:
        raise RuntimeError("Stock resource hashtab differs from the exact verified carrier")


def verify_tree(name, root):
    root = Path(root)
    if not root.is_dir():
        raise RuntimeError(f"Missing pinned toolchain tree: {name}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    expected = json.loads(LOCK.read_text(encoding="utf-8"))["trees"][name]
    if digest.hexdigest() != expected:
        raise RuntimeError(f"Unverified toolchain tree: {name}")
    return expected


def prefix_maps(*directories):
    result = []
    for directory, replacement in ((ROOT, "rmtool"), *directories):
        path = Path(directory).resolve()
        for spelling in dict.fromkeys((str(path), path.as_posix())):
            for flag in ("-ffile-prefix-map", "-fmacro-prefix-map", "-fdebug-prefix-map"):
                result.append(f"{flag}={spelling}={replacement}")
    return result


def check_no_build_paths(path, *directories):
    data = Path(path).read_bytes()
    for directory in (ROOT, Path.home(), *directories):
        resolved = Path(directory).resolve()
        for spelling in (str(resolved), resolved.as_posix()):
            if spelling.encode() in data or spelling.encode("utf-16le") in data:
                raise RuntimeError(f"Build-machine path embedded in native output: {path}")


def go_environment():
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GO") or key in ("CGO_ENABLED", "SOURCE_DATE_EPOCH"):
            env.pop(key)
    env.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOWORK="off", GOENV="off", GOFLAGS="", SOURCE_DATE_EPOCH="0")
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--qt-host", type=Path, required=True)
    args = parser.parse_args()
    verify_archive_runtime()
    for name, path in (("zig", args.zig), ("go", args.go), ("moc", args.qt_host / "bin/moc.exe")):
        verify_tool(name, path)
    trees = {"zig-lib": args.zig.parent / "lib", "go-src": args.go.parent.parent / "src",
             "go-tools": args.go.parent.parent / "pkg/tool", "qt-include": args.qt_host / "include",
             "qt-bin": args.qt_host / "bin"}
    for name, path in trees.items():
        print(name, verify_tree(name, path), flush=True)


if __name__ == "__main__":
    main()
