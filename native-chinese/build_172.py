"""Build .172 offline candidates without changing application trust or published assets."""

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _tap_page_turn as tap

FEATURES = (
    "tap-page-turn", "fast-mono-reading", "native-chinese", "pinyin-input",
    "reading-enhancements", "note-enhancements",
)
HASHTAB = "exthome/qt-resource-rebuilder/hashtab"
COMMON = ("xovi.so", "extensions.d/qt-resource-rebuilder.so", "qmd-tool", HASHTAB)
RELEASE = "3.28.0.172"
FIRMWARE = "20260827113527"
XOCHITL = {
    "ferrari": "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021",
    "chiappa": "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4",
}


def module(feature):
    spec = importlib.util.spec_from_file_location(
        feature.replace("-", "_") + "_builder", ROOT / feature / "build_assets.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def digest(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def verified(path, size, sha256):
    data = path.read_bytes()
    if (len(data), digest(data)) != (size, sha256):
        raise RuntimeError(f"Input identity mismatch: {path}")
    return data


def pack(files):
    data = tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False))
    if data != tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False)):
        raise RuntimeError("Non-deterministic archive")
    return data


def predecessor(feature, platform, cache):
    app = __import__("_" + feature.replace("-", "_"))
    packages = app.parse_manifest((ROOT / feature / "manifest.json").read_bytes())
    matches = [p for p in packages if p.platform == platform and p.release_version == "3.28.0.169"]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one current predecessor: {feature}/{platform}")
    package = matches[0]
    for path in cache.rglob(package.asset):
        if path.stat().st_size == package.size and digest(path.read_bytes()) == package.sha256:
            with tempfile.TemporaryDirectory() as temporary:
                extracted = tap.extract_verified_package(path, package, temporary)
                files = {f.path: ((extracted / f.path).read_bytes(), f.mode) for f in package.files}
            return package, files
    raise RuntimeError(f"Missing verified current predecessor: {feature}/{package.asset}")


def replay(qmds, target, args, work):
    work.mkdir(parents=True)
    hashtabs = work / "hashtabs"
    hashtabs.mkdir()
    hashtab = Path(target["qrex_root"]) / target["hashtab"]
    (hashtabs / "hashtab-target").write_bytes(hashtab.read_bytes())
    paths = []
    qmd_dir = work / "qmd"
    qmd_dir.mkdir()
    for index, (name, data) in enumerate(qmds):
        path = qmd_dir / f"{index:02d}-{name}.qmd"
        path.write_bytes(data)
        paths.append(path)
    commands = [
        ([str(args.qmd_tool), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmd_dir)], b"ALL OK"),
        ([str(args.qmldiff), "apply-diffs", "--hashtab", str(hashtab), "-c",
          str(Path(target["qrex_root"]) / "qrex-out"), str(work / "replay"), *map(str, paths)], None),
    ]
    for command, required in commands:
        result = subprocess.run(command, capture_output=True)
        if result.returncode or (required and required not in result.stdout + result.stderr):
            raise RuntimeError((result.stdout + result.stderr).decode(errors="replace"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--firmware-cache", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=ROOT / "build")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--qt-bin", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    # Only a dedicated build directory may receive unpublished candidates.
    if not args.output_dir.is_relative_to(ROOT / "build"):
        raise RuntimeError("Output must be below the repository build directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "translations/build_172.py"),
                    "--qt-bin", str(args.qt_bin), "--output-dir", str(args.output_dir / "translations")], check=True)
    records = json.loads((args.research / "firmware-172-identity.json").read_text())
    matrix = json.loads((args.research / "resource-matrix-172.json").read_text())["targets"]
    if len(records) != 2 or {r["platform"] for r in records} != set(XOCHITL):
        raise RuntimeError("Expected both exact official identities")
    if len(matrix) != 2 or {r["platform"] for r in matrix} != set(XOCHITL):
        raise RuntimeError("Expected both recovered resource trees")
    entries = {feature: [] for feature in FEATURES}
    checks = []
    for record in records:
        platform = record["platform"]
        target = dict(next(t for t in matrix if t["platform"] == platform))
        if (record["internal_version"], record["release_version"],
            record["files"]["usr/bin/xochitl"]["sha256"]) != (FIRMWARE, RELEASE, XOCHITL[platform]):
            raise RuntimeError("Unexpected official firmware identity")
        if (target["firmware"], target["xochitl_sha256"], target["release_version"]) != (FIRMWARE, XOCHITL[platform], RELEASE):
            raise RuntimeError("Resource tree identity mismatch")
        rootfs = args.firmware_cache / platform / "extracted/selected-rootfs"
        for name, info in record["files"].items():
            verified(rootfs / name, info["size"], info["sha256"])
        verified(args.firmware_cache / platform / record["filename"], record["size"], record["sha256"])
        hashtab = verified(Path(target["qrex_root"]) / target["hashtab"], target["hashtab_size"], target["hashtab_sha256"])
        target["id"] = f"{platform}-{FIRMWARE}"
        payloads = {}
        qmds = {}
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for feature in FEATURES:
                base, files = predecessor(feature, platform, args.cache_root)
                files[HASHTAB] = (hashtab, 0o644)
                qmd_path = f"exthome/qt-resource-rebuilder/{feature}.qmd"
                if feature in {"reading-enhancements", "note-enhancements"}:
                    builder = module(feature)
                    source = builder._source_for_release(ROOT / feature / f"qmd-src/{feature}-3.28.qmd", RELEASE, work / feature / "source")
                    files[qmd_path] = (builder._compile_and_validate(
                        qmd_tool=args.qmd_tool, qmldiff=args.qmldiff, source=source,
                        target=target, work=work / feature / "validation"), 0o644)
                if feature == "native-chinese":
                    catalog = (args.output_dir / f"translations/reMarkable_zh_CN-{RELEASE}-{platform}.qm").read_bytes()
                    if catalog != files["native-chinese/reMarkable_zh_CN.qm"][0]:
                        raise RuntimeError("Rebuilt Chinese catalog differs from audited predecessor")
                    files["native-chinese/reMarkable_zh_CN.qm"] = (catalog, 0o644)
                qmds[feature] = files[qmd_path][0]
                replay([(feature, qmds[feature])], target, args, work / f"single-{feature}")
                payloads[feature] = files
                asset = f"rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}.tar.gz"
                data = pack(files)
                entry = dict(firmware=FIRMWARE, release_version=RELEASE, platform=platform,
                             architecture="aarch64", channel="stable", xochitl_sha256=XOCHITL[platform],
                             asset=asset, size=len(data), sha256=digest(data),
                             offline_verified=True, device_verified=False,
                             files=[dict(path=p, size=len(d), sha256=digest(d), mode=m) for p, (d, m) in sorted(files.items())])
                if hasattr(base, "package_revision"):
                    entry["package_revision"] = base.package_revision
                output = args.output_dir / feature / asset
                tap._write_atomic(output, data)
                entries[feature].append(entry)
                print(f"{platform}/{feature}: {len(data)} {digest(data)}", flush=True)
            for feature in FEATURES:
                if any(payloads[feature][p] != payloads["tap-page-turn"][p] for p in COMMON):
                    raise RuntimeError(f"Shared runtime mismatch: {platform}/{feature}")
            combinations = [
                ("native-chinese", "pinyin-input", "reading-enhancements", "note-enhancements"),
                ("native-chinese", "pinyin-input", "tap-page-turn", "fast-mono-reading", "note-enhancements"),
            ]
            for index, names in enumerate(combinations):
                for reverse in (False, True):
                    order = tuple(reversed(names)) if reverse else names
                    replay([(n, qmds[n]) for n in order], target, args, work / f"combined-{index}-{reverse}")
                    checks.append(dict(platform=platform, order=order, result="PASS"))
    for feature, packages in entries.items():
        document = dict(schema_version=1, packages=packages)
        tap._write_atomic(args.output_dir / feature / "manifest.candidate.json", (json.dumps(document, indent=2) + "\n").encode())
    report = dict(release_version=RELEASE, status="offline-candidates-only", device_verified=False,
                  application_enabled=False, package_count=sum(map(len, entries.values())), checks=checks,
                  blocker="Application exact-target allowlists and bundled manifests require separate integration; no publication or device test performed.")
    tap._write_atomic(args.output_dir / "validation.json", (json.dumps(report, indent=2) + "\n").encode())
    print("PASS: 12 offline candidates; application integration pending", flush=True)


if __name__ == "__main__":
    main()
