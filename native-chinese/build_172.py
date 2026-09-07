"""Build exact 3.28.0.172 candidates for every supported device family."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import _tap_page_turn as tap

RELEASE = "3.28.0.172"
FIRMWARE = "20260827113527"
COLOR_PLATFORMS = {"ferrari", "chiappa"}
TARGETS = {
    "ferrari": ("aarch64", "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"),
    "chiappa": ("aarch64", "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"),
    "tatsu": ("aarch64", "fa674d2ca3d8002602ce4b1b92280b96bcdf2cf32b6a42ec62ae178a1fad1fe3"),
    "rm1": ("armv7l", "1f4fbb6e14650704b5b036e482da9948e73553178f6116ad464ab072f7b90117"),
    "rm2": ("armv7l", "071d85beef3ef2d4cc0e11002140b27b82a2cc04a2ed740a5669f591069b77df"),
}
FEATURES_BY_PLATFORM = {
    "ferrari": ("tap-page-turn", "fast-mono-reading", "native-chinese", "pinyin-input", "reading-enhancements", "note-enhancements"),
    "chiappa": ("tap-page-turn", "fast-mono-reading", "native-chinese", "pinyin-input", "reading-enhancements", "note-enhancements"),
    "tatsu": ("tap-page-turn", "native-chinese", "pinyin-input"),
    "rm1": ("tap-page-turn", "native-chinese", "pinyin-input"),
    "rm2": ("tap-page-turn", "native-chinese", "pinyin-input"),
}
FEATURES = tuple(dict.fromkeys(feature for names in FEATURES_BY_PLATFORM.values() for feature in names))
HASHTAB = "exthome/qt-resource-rebuilder/hashtab"
COMMON = ("xovi.so", "extensions.d/qt-resource-rebuilder.so", "qmd-tool", HASHTAB)
PINYIN_SERVER_SIZE = "18481336"
PINYIN_SERVER_SHA256 = "ab1935dac1e91a86e7b704f9feb0de985e009f366590448fe9c6ec5e400901bf"
ARM_TRANSLATOR = (2888, "9569d723d4057f741fcb70522b90a69e11aa5c75998cee8a6dcb69ad668be722")


def digest(data):
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


def _catalog(feature):
    app = __import__("_" + feature.replace("-", "_"))
    return app.parse_manifest((ROOT / feature / "manifest.json").read_bytes())


def _package(entry):
    return SimpleNamespace(
        **{key: value for key, value in entry.items() if key not in {"files", "urls"}},
        files=tuple(SimpleNamespace(**item) for item in entry["files"]),
    )


def _find_archive(package, roots):
    for root in roots:
        if not root.is_dir():
            continue
        for path in (root / package.asset, *root.rglob(package.asset)):
            if path.is_file() and path.stat().st_size == package.size:
                data = path.read_bytes()
                if digest(data) == package.sha256:
                    return path, data
    raise RuntimeError(f"Missing verified archive: {package.asset}")


def _extract_files(path, package):
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(path, package, temporary)
        return {
            item.path: (
                extracted.joinpath(*PurePosixPath(item.path).parts).read_bytes(),
                item.mode,
            )
            for item in package.files
        }


def predecessor(platform, roots):
    matches = [
        package for package in _catalog("tap-page-turn")
        if package.platform == platform and package.release_version == "3.27.3.0"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one classic predecessor: {platform}")
    package = matches[0]
    path, _data = _find_archive(package, roots)
    return package, _extract_files(path, package)


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


def _entry(feature, platform, files):
    architecture, xochitl = TARGETS[platform]
    data = pack(files)
    entry = {
        "firmware": FIRMWARE, "release_version": RELEASE, "platform": platform,
        "architecture": architecture, "channel": "stable", "xochitl_sha256": xochitl,
        "asset": f"rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}.tar.gz",
        "size": len(data), "sha256": digest(data), "offline_verified": True,
        "device_verified": False,
        "files": [
            {"path": path, "size": len(value), "sha256": digest(value), "mode": mode}
            for path, (value, mode) in sorted(files.items())
        ],
    }
    return entry, data


def _pinyin_service(server):
    source = (ROOT / "pinyin-input/rmtool-pinyin-input.service").read_text()
    if PINYIN_SERVER_SIZE not in source or PINYIN_SERVER_SHA256 not in source:
        raise RuntimeError("Pinyin service template identity changed")
    return source.replace(PINYIN_SERVER_SIZE, str(len(server))).replace(
        PINYIN_SERVER_SHA256, digest(server)
    ).encode()


def _legacy_payloads(platform, base_files, target, catalogs, rmkit_root):
    architecture, _xochitl = TARGETS[platform]
    runtime = {path: base_files[path] for path in COMMON}
    runtime[HASHTAB] = ((Path(target["qrex_root"]) / target["hashtab"]).read_bytes(), 0o644)
    tap_files = dict(base_files)
    tap_files[HASHTAB] = runtime[HASHTAB]
    tap_files["exthome/qt-resource-rebuilder/tap-page-turn.qmd"] = (
        (Path(target["qrex_root"]) / "tap-page-turn.qmd").read_bytes(), 0o644
    )

    native_files = dict(runtime)
    native_files["exthome/qt-resource-rebuilder/native-chinese.qmd"] = (
        (ROOT / "native-chinese/qmd/ferrari-3.28.0.166.qmd").read_bytes(), 0o644
    )
    translator = ROOT / "native-chinese" / (
        "native-chinese-translator-armv7.so" if architecture == "armv7l"
        else "native-chinese-translator.so"
    )
    translator_gate = ARM_TRANSLATOR if architecture == "armv7l" else (
        3976, "4408c4ecf1e2774cbbc10374aae544e3d600525663eba8893cdceb83374b8734"
    )
    native_files["extensions.d/native-chinese-translator.so"] = (
        verified(translator, *translator_gate), 0o644
    )
    native_files["native-chinese/reMarkable_zh_CN.qm"] = (catalogs[platform], 0o644)

    pinyin_files = dict(runtime)
    hook_name = "ime_hook-armv7.so" if architecture == "armv7l" else "ime_hook.so"
    server_name = "ime-server-armv7" if architecture == "armv7l" else "ime-server"
    server = (rmkit_root / "dist" / server_name).read_bytes()
    pinyin_files.update({
        "exthome/qt-resource-rebuilder/pinyin-input.qmd": ((ROOT / "pinyin-input/qmd/pinyin-input.qmd").read_bytes(), 0o644),
        "exthome/qt-resource-rebuilder/zh_CN.rcc": ((ROOT / "pinyin-input/zh_CN.rcc").read_bytes(), 0o644),
        "pinyin-input/ime_hook.so": ((rmkit_root / "dist" / hook_name).read_bytes(), 0o644),
        "pinyin-input/ime-server": (server, 0o755),
        "pinyin-input/rmtool-pinyin-input.service": (_pinyin_service(server), 0o644),
        "pinyin-input/NOTICE-rmkit.md": ((rmkit_root / "NOTICE.md").read_bytes(), 0o644),
        "pinyin-input/LICENSE-rmkit": ((rmkit_root / "LICENSE").read_bytes(), 0o644),
    })
    return {"tap-page-turn": tap_files, "native-chinese": native_files, "pinyin-input": pinyin_files}


def _preserved_color(entries, roots, output):
    for feature in FEATURES_BY_PLATFORM["ferrari"]:
        document = json.loads((ROOT / feature / "manifest.json").read_text())
        for platform in sorted(COLOR_PLATFORMS):
            matches = [
                item for item in document["packages"]
                if item["platform"] == platform and item["release_version"] == RELEASE
            ]
            if len(matches) != 1:
                raise RuntimeError(f"Expected one existing .172 package: {feature}/{platform}")
            package = _package(matches[0])
            _path, data = _find_archive(package, roots)
            tap._write_atomic(output / feature / package.asset, data)
            entries[feature].append(matches[0])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--firmware-cache", type=Path, required=True)
    parser.add_argument("--cache-root", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--qt-bin", type=Path, required=True)
    parser.add_argument("--rmkit-root", type=Path, default=Path(r"E:\rmkit-cn-v1.1.1"))
    args = parser.parse_args()
    args.output_dir = args.output_dir.resolve()
    if not args.output_dir.is_relative_to(ROOT / "build"):
        raise RuntimeError("Output must be below the repository build directory")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    roots = tuple(args.cache_root) or (ROOT / "build", ROOT / ".rmtool/cache")

    subprocess.run([
        sys.executable, str(ROOT / "translations/build_172.py"),
        "--qt-bin", str(args.qt_bin), "--output-dir", str(args.output_dir / "translations")
    ], check=True)
    records = json.loads((args.research / "firmware-172-legacy-identity.json").read_text())
    matrix = json.loads((args.research / "resource-matrix-172-legacy.json").read_text())["targets"]
    legacy_platforms = set(TARGETS) - COLOR_PLATFORMS
    if {r["platform"] for r in records} != legacy_platforms or {r["platform"] for r in matrix} != legacy_platforms:
        raise RuntimeError("Expected exact Tatsu/RM1/RM2 evidence")

    entries = {feature: [] for feature in FEATURES}
    checks = []
    _preserved_color(entries, roots, args.output_dir)
    catalogs = {
        "tatsu": verified(args.output_dir / f"translations/reMarkable_zh_CN-{RELEASE}-tatsu.qm",
                          192400, "2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1"),
    }
    legacy_catalog = verified(args.output_dir / f"translations/reMarkable_zh_CN-{RELEASE}-rm1-rm2.qm",
                              205621, "0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485")
    catalogs.update(rm1=legacy_catalog, rm2=legacy_catalog)

    abi = {
        "status": "PASS",
        "targets": {
            platform: {"architecture": TARGETS[platform][0], "result": "PRESERVED"}
            for platform in sorted(COLOR_PLATFORMS)
        },
        "native_translator": {},
    }
    for record in records:
        platform = record["platform"]
        architecture, xochitl = TARGETS[platform]
        target = next(t for t in matrix if t["platform"] == platform)
        if (record["internal_version"], record["release_version"], record["files"]["usr/bin/xochitl"]["sha256"]) != (FIRMWARE, RELEASE, xochitl):
            raise RuntimeError("Unexpected official firmware identity")
        if (target["firmware"], target["xochitl_sha256"], target["architecture"]) != (FIRMWARE, xochitl, architecture):
            raise RuntimeError("Resource tree identity mismatch")
        rootfs = args.firmware_cache / platform / "extracted/selected-rootfs"
        for name, info in record["files"].items():
            verified(rootfs / name, info["size"], info["sha256"])
        verified(args.firmware_cache / platform / record["filename"], record["size"], record["sha256"])
        verified(Path(target["qrex_root"]) / target["hashtab"], target["hashtab_size"], target["hashtab_sha256"])

        _base, base_files = predecessor(platform, roots)
        payloads = _legacy_payloads(platform, base_files, target, catalogs, args.rmkit_root)
        qmds = {}
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for feature, files in payloads.items():
                qmd_path = f"exthome/qt-resource-rebuilder/{feature}.qmd"
                qmds[feature] = files[qmd_path][0]
                replay([(feature, qmds[feature])], target, args, work / f"single-{feature}")
                entry, data = _entry(feature, platform, files)
                tap._write_atomic(args.output_dir / feature / entry["asset"], data)
                entries[feature].append(entry)
                print(f"{platform}/{feature}: {len(data)} {digest(data)}", flush=True)
            for feature, files in payloads.items():
                if any(files[path] != payloads["tap-page-turn"][path] for path in COMMON):
                    raise RuntimeError(f"Shared runtime mismatch: {platform}/{feature}")
            for order in (tuple(payloads), tuple(reversed(payloads))):
                replay([(name, qmds[name]) for name in order], target, args, work / ("combined-" + order[0]))
                checks.append({"platform": platform, "order": order, "result": "PASS"})
        abi["targets"][platform] = {"architecture": architecture, "result": "PASS"}
        translator = payloads["native-chinese"]["extensions.d/native-chinese-translator.so"][0]
        abi["native_translator"][architecture] = {"size": len(translator), "sha256": digest(translator)}

    for feature, packages in entries.items():
        expected = {platform for platform, names in FEATURES_BY_PLATFORM.items() if feature in names}
        if {entry["platform"] for entry in packages} != expected:
            raise RuntimeError(f"Incomplete feature matrix: {feature}")
        document = {"schema_version": 1, "packages": packages}
        tap._write_atomic(args.output_dir / feature / "manifest.candidate.json", (json.dumps(document, indent=2) + "\n").encode())
    tap._write_atomic(args.output_dir / "abi-validation.json", (json.dumps(abi, indent=2) + "\n").encode())
    report = {
        "release_version": RELEASE, "status": "offline-candidates-only", "device_verified": False,
        "application_enabled": False, "package_count": sum(map(len, entries.values())),
        "preserved_color_package_count": 12, "checks": checks,
        "blocker": "No publication or device test performed.",
    }
    tap._write_atomic(args.output_dir / "validation.json", (json.dumps(report, indent=2) + "\n").encode())
    print("PASS: 21 offline candidates; application integration pending", flush=True)


if __name__ == "__main__":
    main()
