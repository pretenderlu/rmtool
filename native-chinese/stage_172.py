"""Verify and append .172 payloads to local caches, without activating manifests."""

import argparse
import json
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace

from build_172 import FEATURES, FEATURES_BY_PLATFORM, FIRMWARE, RELEASE, ROOT, TARGETS, tap, verified


def verify_candidate(path, entry, feature):
    platform = entry["platform"]
    architecture, xochitl = TARGETS.get(platform, (None, None))
    if (architecture is None or entry["firmware"] != FIRMWARE
            or entry["release_version"] != RELEASE or entry["xochitl_sha256"] != xochitl
            or entry["architecture"] != architecture or entry["channel"] != "stable"
            or entry["offline_verified"] is not True or entry["device_verified"] is not False):
        raise RuntimeError("Candidate identity/verification mismatch")
    if entry["asset"] != f"rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}.tar.gz":
        raise RuntimeError("Unexpected candidate asset name")
    data = verified(path, entry["size"], entry["sha256"])
    files = entry["files"]
    expected = {f["path"]: f for f in files}
    if len(expected) != len(files):
        raise RuntimeError("Duplicate candidate file")
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.name not in expected or member.mode != expected[member.name]["mode"]:
                raise RuntimeError("Candidate member/mode mismatch")
    package = SimpleNamespace(files=tuple(SimpleNamespace(**f) for f in files))
    with tempfile.TemporaryDirectory() as temporary:
        tap.extract_verified_package(path, package, temporary)
    return data


def append_only(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise RuntimeError(f"Refusing to overwrite existing cache file: {path}")


def stage(source, cache):
    report = json.loads((source / "validation.json").read_text())
    expected_count = sum(map(len, FEATURES_BY_PLATFORM.values()))
    if report.get("status") != "offline-candidates-only" or report.get("package_count") != expected_count:
        raise RuntimeError("Complete offline validation report required")
    pending = []
    for feature in FEATURES:
        raw = (source / feature / "manifest.candidate.json").read_bytes()
        entries = json.loads(raw)["packages"]
        expected = {platform for platform, names in FEATURES_BY_PLATFORM.items() if feature in names}
        if {e["platform"] for e in entries} != expected:
            raise RuntimeError("Incomplete candidate manifest")
        for entry in entries:
            data = verify_candidate(source / feature / entry["asset"], entry, feature)
            pending.append((cache / feature / FIRMWARE / entry["asset"], data))
        pending.append((cache / feature / "manifest.172.five-device.candidate.json", raw))
    # Detect every collision before the first cache write. Never replace manifest.json.
    for path, data in pending:
        if path.exists() and path.read_bytes() != data:
            raise RuntimeError(f"Refusing to overwrite existing cache file: {path}")
    for path, data in pending:
        append_only(path, data)
    return [str(path.resolve()) for path, _ in pending]


def integrate(source, cache):
    """Append validated build outputs, then use the real application parsers."""
    abi = json.loads((source / "abi-validation.json").read_text())
    if (abi.get("status") != "PASS" or set(abi.get("targets", {})) != set(TARGETS)
            or set(abi.get("native_translator", {})) != {"aarch64", "armv7l"}):
        raise RuntimeError("Static hook/relocation evidence required")
    stage(source, cache)
    pending = []
    for feature in FEATURES:
        app = __import__("_" + feature.replace("-", "_"))
        path = ROOT / feature / "manifest.json"
        document = json.loads(path.read_bytes())
        additions = json.loads((source / feature / "manifest.candidate.json").read_bytes())["packages"]
        for addition in additions:
            entry = dict(addition)
            if feature != "tap-page-turn":
                origins = getattr(app, "REMOTE_BASE_URLS", None) or (app.COS_URL, app.GITHUB_URL)
                entry["urls"] = [f"{origin}/{entry['asset']}" for origin in origins]
            existing = [p for p in document["packages"] if p["asset"] == entry["asset"]]
            if existing and existing != [entry]:
                raise RuntimeError(f"Existing bundled record differs: {entry['asset']}")
            if not existing:
                document["packages"].append(entry)
        data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        if feature in {"reading-enhancements", "note-enhancements", "fast-mono-reading"}:
            app.parse_manifest(data, require_local_match=False)
        else:
            app.parse_manifest(data)
        cache_path = cache / feature / "manifest.json"
        if cache_path.exists():
            old = cache_path.read_bytes()
            previous = json.loads(old)
            for entry in previous["packages"]:
                if entry not in document["packages"]:
                    raise RuntimeError(f"Cache contains differing records; preserved: {cache_path}")
            if old != data:
                append_only(cache_path.with_name("manifest.before-172-five-device.json"), old)
        pending.extend(((path, data), (cache_path, data)))
    for path, data in pending:
        tap._write_atomic(path, data)
    report = dict(release_version=RELEASE, application_trust_integrated=True,
                  device_verified=False, published=False,
                  manifests=[str(path.resolve()) for path, _ in pending],
                  payload_directory=str(cache.resolve()))
    tap._write_atomic(source / "integration.json", (json.dumps(report, indent=2) + "\n").encode())
    # A fresh process will rebuild each module's cached trusted catalog.
    return [str(path.resolve()) for path, _ in pending]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "build/resources-172-five-device",
    )
    parser.add_argument("--cache", type=Path, default=ROOT / ".rmtool/cache")
    parser.add_argument("--integrate", action="store_true", help="append bundled and active cache records after application gates are updated")
    args = parser.parse_args()
    for path in (integrate(args.source, args.cache) if args.integrate else stage(args.source, args.cache)):
        print(path)
    print("Local manifests integrated; no upload/device action." if args.integrate else
          "Payloads verified and staged; active manifests unchanged.")
