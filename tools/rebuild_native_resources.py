"""Rebuild native QR and reader payloads using exact, already-verified carriers."""

import argparse
import hashlib
import importlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _plugin_packages as packages
import _tap_page_turn as tap
from tools.build_beta_329 import load
from tools.native_build import verify_archive_runtime, verify_tool
from tools.validate_build_matrix import validate_native_payload

HASHTAB = "exthome/qt-resource-rebuilder/hashtab"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(command):
    result = subprocess.run(list(map(str, command)), capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode(errors="replace"))
    return result.stdout


def resource_for(package, args):
    if package.release_version == "3.29.1.0":
        root, hashtab = args.stable_resources / package.platform, "hashtab"
    elif package.release_version.startswith(("3.29.", "3.30.")):
        root, hashtab = args.firmware_work / ("beta-" + package.release_version) / package.platform, "hashtab"
    else:
        matrix = json.loads(args.matrix_config.read_text(encoding="utf-8"))["targets"]
        matches = [target for target in matrix if all(target[key] == getattr(package, key)
                   for key in ("platform", "firmware", "xochitl_sha256"))]
        if len(matches) == 1:
            root, hashtab = Path(matches[0]["qrex_root"]), matches[0]["hashtab"]
        elif package.platform in ("rm1", "rm2", "tatsu"):
            group = "matrix-172-classic" if package.release_version == "3.28.0.172" else "matrix"
            root = args.firmware_work / "tap-matrix" / group / f"{package.platform}-{package.firmware}"
            hashtab = "hashtab"
        else:
            raise RuntimeError(f"Missing exact stock resource config: {package.package_id}")
    return root, root / hashtab


def rebuild_reader(args, module, package, files, resources, hashtab, work):
    adapter = load(ROOT / "reading-enhancements/build_assets.py")
    source = ROOT / args.feature / f"qmd-src/{args.feature}-3.28.qmd"
    if args.feature == "reading-enhancements":
        source = adapter._source_for_target(source, package.firmware, package.release_version,
                                           package.platform, package.architecture, package.xochitl_sha256, work / "source")
        target = dict(id=package.package_id, qrex_root=str(resources), hashtab=str(hashtab),
                      firmware=package.firmware, release_version=package.release_version, platform=package.platform,
                      architecture=package.architecture, xochitl_sha256=package.xochitl_sha256)
        qmd = adapter._compile_and_validate(qmd_tool=args.qmd_tool, qmldiff=args.qmldiff,
                                           source=source, target=target, work=work / "validation")
        extension = "extensions.d/rmtool-highlight-snap.so"
        if extension in files:
            suffix = {"3.28.0.172": "", "3.29.0.152": "-3.29"}.get(package.release_version, "-" + package.release_version)
            binary = (ROOT / f"reading-enhancements/native/rmtool-highlight-snap{suffix}.so").read_bytes()
            validate_native_payload(vars(package), extension, binary)
            files[extension] = binary, 0o644
            files["LICENSE.rm-tweak"] = (ROOT / "reading-enhancements/native/LICENSE.rm-tweak").read_bytes().replace(b"\r\n", b"\n"), 0o644
    else:
        text = adapter._tap_source_for_release(source.read_text(encoding="utf-8"), package.release_version)
        source = work / "tap.qmd"
        source.write_text(text, encoding="utf-8", newline="\n")
        qmd = run([args.qmd_tool, "hash", "-hashtab", hashtab, source])
        output = work / "tap-compiled.qmd"
        output.write_bytes(qmd)
        run([args.qmldiff, "check-compatibility", hashtab, output])
        run([args.qmldiff, "apply-diffs", "--hashtab", hashtab, "-c", resources / "qrex-out", work / "replay", output])
        replayed = (work / "replay/qml/device/view/documentview/SceneViewGestures.qml").read_text(encoding="utf-8")
        if replayed.count("touchClicked();") + replayed.count("touchClicked()\n") < 3:
            raise RuntimeError("Tap replay omitted native touch notification")
    files[f"exthome/qt-resource-rebuilder/{args.feature}.qmd"] = qmd, 0o644


def rebuild_pinyin(args, package, files, resources, hashtab, work):
    source = ROOT / "pinyin-input/qmd/pinyin-input.qmd"
    qmd = (run([args.qmd_tool, "hash", "-hashtab", hashtab, source])
           if package.release_version.startswith(("3.29.", "3.30.")) else source.read_bytes())
    compiled = work / "pinyin-input.qmd"
    compiled.write_bytes(qmd)
    run([args.qmldiff, "check-compatibility", hashtab, compiled])
    run([args.qmldiff, "apply-diffs", "--hashtab", hashtab, "-c", resources / "qrex-out", work / "replay", compiled])
    files["exthome/qt-resource-rebuilder/pinyin-input.qmd"] = qmd, 0o644


def rebuild_weread(args, module, files, resources, work):
    if digest((resources / "hashtab").read_bytes()) != digest(files[HASHTAB][0]):
        raise RuntimeError("WeRead stock hashtab differs from its verified carrier")
    builder = load(ROOT / "weread-launcher/build_assets.py")
    qmd = builder.compile_qmd(args.qmd_tool, args.qmldiff,
        ROOT / "weread-launcher/qmd-src/weread-launcher-3.28.qmd", resources, work)
    files[module.QMD_PAYLOAD_PATH] = qmd, 0o644
    files[module.BRIDGE_PAYLOAD_PATH] = (args.weread_native / "rmtool-weread-launcher.so").read_bytes(), 0o644
    files[module.SHIM_PAYLOAD_PATH] = (args.weread_native / "rmtool-weread-fast.so").read_bytes(), 0o644


def stock_libraries_for(args, package, work):
    libraries = args.stock_libraries / package.release_version / package.platform
    if libraries.is_dir():
        return libraries
    if args.firmware_cache is None:
        raise RuntimeError(f"Missing exact stock libraries: {package.platform}/{package.release_version}")
    if args.seven_zip is None:
        raise RuntimeError("Stock-library extraction requires --seven-zip")
    verify_tool("7z", args.seven_zip)
    libraries = work / "stock-libraries"
    native_builder = load(ROOT / "native-import/build.py")
    root, _ = native_builder.stock_libraries(args, package.platform, package.release_version, libraries)
    if (digest((root / "usr/bin/xochitl").read_bytes()) != package.xochitl_sha256
            or (root / "etc/version").read_text().strip() != package.firmware):
        raise RuntimeError("Stock library root has the wrong firmware identity")
    return libraries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature", required=True, choices=("qr-upload", "tap-page-turn", "reading-enhancements", "pinyin-input", "weread-launcher"))
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--qt-host", type=Path)
    parser.add_argument("--zig", type=Path)
    parser.add_argument("--receiver-dir", type=Path)
    parser.add_argument("--weread-native", type=Path)
    parser.add_argument("--stock-libraries", type=Path)
    parser.add_argument("--python-libs", type=Path)
    parser.add_argument("--firmware-cache", type=Path)
    parser.add_argument("--seven-zip", type=Path)
    parser.add_argument("--firmware-work", type=Path, required=True)
    parser.add_argument("--stable-resources", type=Path, required=True)
    parser.add_argument("--matrix-config", type=Path, required=True)
    parser.add_argument("--release", action="append", help="Rebuild only these exact public releases; repeat for multiple releases")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verify_archive_runtime()
    verify_tool("qmd-tool", args.qmd_tool)
    verify_tool("qmldiff", args.qmldiff)
    if args.feature == "qr-upload" and not all((args.qt_host, args.zig, args.receiver_dir)):
        parser.error("QR builds require --qt-host, --zig and --receiver-dir")
    if args.feature == "weread-launcher" and not args.weread_native:
        parser.error("WeRead builds require --weread-native")
    if args.python_libs:
        sys.path.insert(0, str(args.python_libs))
    module = importlib.import_module("_" + args.feature.replace("-", "_"))
    parser_options = {} if args.feature in ("tap-page-turn", "pinyin-input") else dict(require_local_match=False)
    carrier_builder = load(ROOT / "native-chinese/build_172.py")
    document = json.loads(module.BUNDLED_MANIFEST.read_bytes())
    catalog = module.parse_manifest(json.dumps(document).encode())
    if args.release and (missing := set(args.release) - {package.release_version for package in catalog}):
        parser.error(f"No {args.feature} targets for: {', '.join(sorted(missing))}")
    args.output.mkdir(parents=True, exist_ok=True)
    report = []
    for index, package in enumerate(catalog):
        if args.release and package.release_version not in args.release:
            continue
        resources, hashtab = resource_for(package, args)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            if args.feature == "qr-upload":
                if digest(hashtab.read_bytes()) != package.file(HASHTAB).sha256:
                    raise RuntimeError(f"QR stock hashtab differs from trusted carrier: {package.package_id}")
                import _native_import_control as control
                carrier = control.select_package(control._trusted_catalog(),
                    tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256))
                receiver = args.receiver_dir / ("rmtool-qr-upload-arm64" if package.architecture == "aarch64" else "rmtool-qr-upload-arm")
                validate_native_payload(vars(package), module.RECEIVER_PAYLOAD_PATH, receiver.read_bytes())
                identity_path = work / "identity.json"
                identity_path.write_text(json.dumps({key: getattr(package, key) for key in
                    ("firmware", "platform", "architecture", "xochitl_sha256")}), encoding="utf-8", newline="\n")
                command = [sys.executable, ROOT / "qr-upload/native/build.py", "--qt-host", args.qt_host,
                           "--zig", args.zig, "--qmd-tool", args.qmd_tool, "--qmldiff", args.qmldiff,
                           "--target", resources, "--identity", identity_path, "--receiver", receiver]
                run([*command, "--output", work / "one"])
                run([*command, "--output", work / "two"])
                for name in ("rmtool-qr-upload.so", "rmtool-qr-upload.qmd"):
                    if (work / "one" / name).read_bytes() != (work / "two" / name).read_bytes():
                        raise RuntimeError(f"Non-deterministic QR payload: {name}")
                carrier_archive = packages.package_cache_path(str(ROOT / ".rmtool"), "native-import", carrier)
                extracted = packages.extract_verified_package(carrier_archive, carrier, work / "carrier")
                files = {item.path: ((extracted / item.path).read_bytes(), item.mode) for item in carrier.files
                         if item.path in module.REQUIRED_PATHS}
                files.update({module.RECEIVER_PAYLOAD_PATH: (receiver.read_bytes(), 0o755),
                              module.BRIDGE_PAYLOAD_PATH: ((work / "one/rmtool-qr-upload.so").read_bytes(), 0o644),
                              module.QMD_PAYLOAD_PATH: ((work / "one/rmtool-qr-upload.qmd").read_bytes(), 0o644)})
            else:
                archive, _ = carrier_builder._find_archive(package, (ROOT / ".rmtool", ROOT / "build", args.firmware_work))
                extracted = packages.extract_verified_package(archive, package, work / "carrier")
                files = {item.path: ((extracted / item.path).read_bytes(), item.mode) for item in package.files}
                hashtab = work / "hashtab"
                hashtab.write_bytes(files[HASHTAB][0])
                if args.feature == "pinyin-input":
                    rebuild_pinyin(args, package, files, resources, hashtab, work)
                elif args.feature == "weread-launcher":
                    rebuild_weread(args, module, files, resources, work)
                else:
                    rebuild_reader(args, module, package, files, resources, hashtab, work)
            if set(files) != {item.path for item in package.files}:
                raise RuntimeError("Native rebuild changed its payload file set")
            abi_imports = None
            if args.stock_libraries and args.feature in ("qr-upload", "weread-launcher"):
                from elftools.elf.elffile import ELFFile
                native_builder = load(ROOT / "native-import/build.py")
                bridge = work / "bridge.so"
                bridge.write_bytes(files[module.BRIDGE_PAYLOAD_PATH][0])
                abi_imports = native_builder.check_bridge(bridge,
                    stock_libraries_for(args, package, work), package.architecture, ELFFile)
            packed = tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False))
            if packed != tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False)):
                raise RuntimeError("Non-deterministic archive")
            entry = dict(document["packages"][index])
            entry.update(sha256=digest(packed), size=len(packed),
                         asset=packages.content_asset_name(re.sub(r"-sha256-[0-9a-f]{64}(?=\.tar\.gz$)", "", package.asset), digest(packed)),
                         files=[dict(path=p, mode=m, size=len(data), sha256=digest(data)) for p, (data, m) in sorted(files.items())])
            if "urls" in entry:
                entry["urls"] = [url.rsplit("/", 1)[0] + "/" + entry["asset"] for url in entry["urls"]]
            packages.write_atomic(args.output / entry["asset"], packed)
            candidate = dict(document, packages=[entry if item["asset"] == package.asset else item for item in document["packages"]])
            selected = next(item for item in module.parse_manifest(json.dumps(candidate).encode(), **parser_options)
                            if item.asset == entry["asset"])
            packages.extract_verified_package(args.output / entry["asset"], selected, work / "verify")
            document["packages"][index] = entry
            report.append(dict(platform=package.platform, release=package.release_version, asset=entry["asset"],
                               sha256=entry["sha256"], hashtab_sha256=digest(files[HASHTAB][0]),
                               qmd_replayed=True, abi_imports=abi_imports))
            print(f"PASS {args.feature}/{package.platform}/{package.release_version}", flush=True)
    manifest = (json.dumps(document, indent=2) + "\n").encode()
    updated = module.parse_manifest(manifest, **parser_options)
    for package in updated:
        if args.release and package.release_version not in args.release:
            continue
        packages.extract_verified_package(args.output / package.asset, package,
            args.output / "verified" / package.asset.removesuffix(".tar.gz"))
        packages.write_atomic(packages.package_cache_path(str(ROOT / ".rmtool"), args.feature, package),
                              (args.output / package.asset).read_bytes())
    packages.write_atomic(module.BUNDLED_MANIFEST, manifest)
    packages.write_atomic(args.output / "validation.json", (json.dumps(report, indent=2) + "\n").encode())


if __name__ == "__main__":
    main()
