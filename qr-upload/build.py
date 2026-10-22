"""Build the isolated, standard-library-only QR receiver experiment."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default=shutil.which("go"))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "build" / "qr-upload")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.go:
        parser.error("Go is required; pass --go with its executable path")
    source = Path(__file__).resolve().parent
    env = {**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": ""}
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
        subprocess.run(
            [args.go, "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w", "-o", str(target), "."],
            cwd=source, env=target_env, check=True,
        )
        data = target.read_bytes()
        if data[:4] != b"\x7fELF" or data[5] != 1 or struct.unpack_from("<H", data, 18)[0] != elf_machine:
            raise RuntimeError(f"Unexpected ELF target: {target.name}")
        results[target.name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
