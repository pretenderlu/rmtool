"""Build the isolated, standard-library-only QR receiver experiment."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.native_build import go_environment, verify_tool


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default=shutil.which("go"))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "build" / "qr-upload")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.go:
        parser.error("Go is required; pass --go with its executable path")
    args.go = str(verify_tool("go", args.go))
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary)
        for path in (*source.glob("*.go"), source / "go.mod", *sorted((source / "web").rglob("*"))):
            if path.is_file():
                target = checkout / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))
        build(args, checkout)


def build(args, source):
    env = go_environment()
    for key in ("GOOS", "GOARCH", "GOARM", "GOARM64"):
        env.pop(key, None)
    subprocess.run([args.go, "test", "./..."], cwd=source, env=env, check=True)
    subprocess.run([args.go, "vet", "./..."], cwd=source, env=env, check=True)
    args.output.mkdir(parents=True, exist_ok=True)
    results = {}
    for architecture, elf_machine in (("arm", 40), ("arm64", 183)):
        target = args.output / f"rmtool-qr-upload-{architecture}"
        target_env = {**env, "GOOS": "linux", "GOARCH": architecture}
        if architecture == "arm":
            target_env["GOARM"] = "7"
        command = [args.go, "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w", "-o", str(target), "."]
        subprocess.run(command, cwd=source, env=target_env, check=True)
        data = target.read_bytes()
        subprocess.run(command, cwd=source, env=target_env, check=True)
        if target.read_bytes() != data:
            raise RuntimeError("Non-deterministic QR helper")
        if data[:4] != b"\x7fELF" or data[5] != 1 or struct.unpack_from("<H", data, 18)[0] != elf_machine:
            raise RuntimeError(f"Unexpected ELF target: {target.name}")
        results[target.name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
