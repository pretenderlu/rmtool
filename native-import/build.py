"""Build desktop native-import controls against exact official firmware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _native_chinese as carrier_backend
import _native_import_control as control
import _plugin_packages as packages
import _tap_page_turn as tap
import _xovi_standalone as shared


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(command, *, cwd=None, env=None, warning=False):
    result = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env, capture_output=True)
    if result.returncode not in ({0, 1} if warning else {0}):
        raise RuntimeError((result.stdout + result.stderr).decode(errors="replace"))
    return result.stdout


def build_helpers(go, output):
    env = {**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": ""}
    for key in ("GOOS", "GOARCH", "GOARM", "GOARM64"):
        env.pop(key, None)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        shutil.copy2(ROOT / "native-import/helper/main.go", work / "main.go")
        common = ["main.go", "desktop_native.go", "native_address.go", "private_directory.go"]
        for name in (*common[1:], "platform_linux.go", "platform_other.go",
                     "desktop_native_test.go", "native_address_test.go"):
            shutil.copy2(ROOT / "qr-upload" / name, work / name)
        platform = "platform_linux.go" if sys.platform.startswith("linux") else "platform_other.go"
        tests = [*common, platform, "desktop_native_test.go", "native_address_test.go"]
        run([go, "test", "-count=1", *tests], cwd=work, env=env)
        run([go, "vet", *tests], cwd=work, env=env)
        for architecture in ("arm", "arm64"):
            target_env = {**env, "GOOS": "linux", "GOARCH": architecture}
            if architecture == "arm":
                target_env["GOARM"] = "7"
            run([go, "build", "-trimpath", "-buildvcs=false", "-ldflags=-s -w", "-o",
                 output / f"rmtool-native-import-{architecture}", *common, "platform_linux.go"], cwd=work, env=target_env)


def build_bridge(args, architecture, output):
    shared_source = ROOT / "qr-upload/native"
    target = "aarch64-linux-gnu.2.17" if architecture == "aarch64" else "arm-linux-gnueabihf.2.4"
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        run([args.qt_host / "bin/moc.exe", "-DImportControl=NativeImportControl",
             shared_source / "ImportControl.h", "-o", work / "moc_ImportControl.cpp"])
        include = args.qt_host / "include"
        run([args.zig, "c++", "-target", target, "-std=c++17", "-shared", "-fPIC", "-O2", "-s",
             "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
             f"-Wl,--version-script,{ROOT / 'native-import/native/exports.map'}",
             "-Wl,--allow-shlib-undefined", "-DQT_NO_DEBUG", "-DRMTOOL_NATIVE_IMPORT_STANDALONE",
             "-DImportControl=NativeImportControl",
             f"-ffile-prefix-map={work}=native-import-generated", f"-fmacro-prefix-map={work}=native-import-generated",
             "-I", include, "-I", include / "QtCore", "-I", include / "QtQml", "-I", include / "QtNetwork",
             "-I", shared_source, "-I", work, ROOT / "native-import/native/main.cpp",
             shared_source / "ImportControl.cpp", "-o", output])


def stock_libraries(args, platform, release, output):
    channel = "official" if release in ("3.28.0.172", "3.29.1.0") else "official-beta"
    cache = args.firmware_cache / channel / release / platform
    record = json.loads((cache / "download.json").read_text(encoding="utf-8"))
    image = (cache / record["filename"]).read_bytes()
    if (len(image), digest(image)) != (record["size"], record["sha256"]):
        raise RuntimeError(f"Official SWU cache mismatch: {platform}/{release}")
    images = [path for path in (cache / "extracted").glob("*.ext4*") if path.suffix != ".gz"]
    if len(images) != 1:
        raise RuntimeError("Exact cached ext4 image is ambiguous")
    output.mkdir(parents=True, exist_ok=True)
    run([args.seven_zip, "x", "-y", "-aos", images[0], f"-o{output}",
         "usr/lib/libQt6Core.so.*", "usr/lib/libQt6Qml.so.*", "usr/lib/libQt6Network.so.*",
         "usr/lib/libstdc++.so.*", "usr/lib/libgcc_s.so.*", "lib/libgcc_s.so.*",
         "usr/lib/libc.so.*", "lib/libc.so.*", "lib/libc-*.so", "usr/lib/libdl.so.*", "lib/libdl.so.*",
         "usr/lib/libm.so.*", "lib/libm.so.*", "lib/libm-*.so",
         "usr/lib/libpthread.so.*", "lib/libpthread.so.*", "usr/lib/librt.so.*", "lib/librt.so.*",
         "usr/lib/libutil.so.*", "lib/libutil.so.*", "usr/lib/libresolv.so.*", "lib/libresolv.so.*",
         "lib/ld-linux*", "usr/lib/ld-linux*", "lib/ld-*.so", "usr/lib/ld-*.so",
         "etc/systemd/network/10-usb.network", "usr/lib/systemd/system/usb-ethernet-udhcpd-*.service",
         "usr/share/remarkable/webui/assets/index.js"], warning=True)
    return cache / "extracted/selected-rootfs", record


def check_elf(path, architecture, ELFFile, *, hard_float=False):
    with path.open("rb") as source:
        elf = ELFFile(source)
        expected = "EM_AARCH64" if architecture == "aarch64" else "EM_ARM"
        if elf["e_machine"] != expected or not elf.little_endian:
            raise RuntimeError(f"Unexpected ELF architecture: {path}")
        if hard_float and architecture == "armv7l" and not elf["e_flags"] & 0x400:
            raise RuntimeError("Native control must use the stock ARM hard-float ABI")
        symbols = elf.get_section_by_name(".dynsym")
        return [(symbol.name, symbol["st_shndx"], symbol["st_info"]["bind"])
                for symbol in symbols.iter_symbols()] if symbols else []


def check_bridge(bridge, libraries, architecture, ELFFile):
    imports = {name for name, section, binding in check_elf(bridge, architecture, ELFFile, hard_float=True)
               if name and section == "SHN_UNDEF" and binding != "STB_WEAK"}
    exports = set()
    for path in libraries.rglob("*.so*"):
        if path.is_file():
            exports.update(name for name, section, _binding in check_elf(path, architecture, ELFFile)
                           if section != "SHN_UNDEF")
    if imports - exports:
        raise RuntimeError(f"Unresolved stock ABI imports: {sorted(imports - exports)}")
    return len(imports)


def resource_root(args, platform, firmware, release):
    if release in ("3.29.0.153", "3.29.1.0"):
        return args.beta_resources / platform
    group = "matrix-172" if platform in ("ferrari", "chiappa") else "matrix-172-classic"
    return args.stable_resources / group / f"{platform}-{firmware}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--qt-host", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--seven-zip", type=Path, required=True)
    parser.add_argument("--python-libs", type=Path, required=True)
    parser.add_argument("--firmware-cache", type=Path, required=True)
    parser.add_argument("--stable-resources", type=Path, required=True)
    parser.add_argument("--beta-resources", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release", choices=sorted({item[0] for item in control.ALLOWED_TARGETS.values()}))
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT / "build"):
        raise RuntimeError("Output must remain below the repository build directory")
    sys.path.insert(0, str(args.python_libs))
    from elftools.elf.elffile import ELFFile

    args.output.mkdir(parents=True, exist_ok=True)
    build_helpers(args.go, args.output)
    bridges = {}
    for architecture in ("aarch64", "armv7l"):
        bridges[architecture] = args.output / f"rmtool-native-import-{architecture}.so"
        build_bridge(args, architecture, bridges[architecture])
    entries, checks = [], []
    for identity, verification in control.ALLOWED_TARGETS.items():
        platform, firmware, architecture, stock = identity
        release, channel, offline, device = verification
        if args.release and release != args.release:
            continue
        libraries = args.output / "stock" / release / platform
        rootfs, provenance = stock_libraries(args, platform, release, libraries)
        binary = (rootfs / "usr/bin/xochitl").read_bytes()
        if digest(binary) != stock or (rootfs / "etc/version").read_text().strip() != firmware:
            raise RuntimeError(f"Exact stock identity mismatch: {platform}/{release}")
        for indicator in (b"/upload", b"multipart/form-data", b"WebInterfaceEnabled"):
            if indicator not in binary and indicator.decode().encode("utf-16le") not in binary:
                raise RuntimeError(f"Missing native importer prerequisite: {indicator!r}")
        webui = (libraries / "usr/share/remarkable/webui/assets/index.js").read_bytes()
        for indicator in (b"new FormData", b'.append("file",', b'fetch("/upload",', b'method:"post"', b"status!==201"):
            if indicator not in webui:
                raise RuntimeError(f"Missing exact native web-upload contract: {indicator!r}")
        address = "10.11.99.1/27" if release == "3.28.0.172" else "10.11.99.1/29"
        network_path = ("etc/systemd/network/10-usb.network" if release == "3.28.0.172"
                        else "usr/lib/systemd/system/usb-ethernet-udhcpd-usb1.service")
        network = (libraries / network_path).read_text()
        expected_address = f"Address={address}" if release == "3.28.0.172" else f"ip address replace {address} dev usb1"
        if expected_address not in network:
            raise RuntimeError("Exact stock native address prefix differs")
        imports = check_bridge(bridges[architecture], libraries, architecture, ELFFile)
        helper_arch = "arm64" if architecture == "aarch64" else "arm"
        helper = args.output / f"rmtool-native-import-{helper_arch}"
        check_elf(helper, architecture, ELFFile)
        carrier = carrier_backend.select_package(carrier_backend._trusted_catalog(), tap.DeviceIdentity(firmware, platform, architecture, stock))
        if carrier is None and release == "3.29.0.153":
            # Reuse core code only; the new stock ABI and hashtab are checked below.
            carrier = next((p for p in carrier_backend._trusted_catalog()
                            if p.platform == platform and p.architecture == architecture and p.release_version == "3.29.0.152"), None)
        if carrier is None:
            raise RuntimeError("Exact verified shared-runtime carrier is missing")
        archive = packages.package_cache_path(str(ROOT / ".rmtool"), "native-chinese", carrier)
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            extracted = packages.extract_verified_package(archive, carrier, work / "carrier")
            files = {item.path: ((extracted / item.path).read_bytes(), item.mode)
                     for item in carrier.files if item.path in shared._COMMON_ARCHIVE_PATHS}
            resources = resource_root(args, platform, firmware, release)
            hashtab = (resources / "hashtab").read_bytes()
            if carrier.xochitl_sha256 == stock:
                if digest(hashtab) != carrier.file("exthome/qt-resource-rebuilder/hashtab").sha256:
                    raise RuntimeError("Resource hashtab differs from the exact verified carrier")
            else:
                identity_record = json.loads((resources / "identity.json").read_text())
                expected = dict(platform=platform, release_version=release, firmware=firmware, architecture=architecture,
                                xochitl_sha256=stock, hashtab_sha256=digest(hashtab), sha256=provenance["sha256"])
                if any(identity_record.get(key) != value for key, value in expected.items()):
                    raise RuntimeError("Resource provenance differs from the exact official firmware")
                files["exthome/qt-resource-rebuilder/hashtab"] = (hashtab, 0o644)
            for library in ("xovi.so", "extensions.d/qt-resource-rebuilder.so"):
                check_bridge(extracted / library, libraries, architecture, ELFFile)
            check_elf(extracted / "qmd-tool", architecture, ELFFile)
            storage = (resources / "qrex-out/qml/device/view/settings/Storage.qml").read_text(encoding="utf-8")
            if "Settings.webInterfaceEnabled = !Settings.webInterfaceEnabled" not in storage:
                raise RuntimeError("Stock native-import Settings property differs")
            qmd = run([args.qmd_tool, "hash", "-hashtab", resources / "hashtab", ROOT / "native-import/native/native-import.qmd"])
            qmd_path = work / "native-import.qmd"
            qmd_path.write_bytes(qmd)
            run([args.qmldiff, "apply-diffs", "--hashtab", resources / "hashtab", "-c", resources / "qrex-out", work / "replay", qmd_path])
            main_view = (work / "replay/qml/device/view/main/MainView.qml").read_text(encoding="utf-8")
            if any(marker not in main_view for marker in ("import cn.rmtool.NativeImport 1.0", "NativeImportControl", "settings: Settings")):
                raise RuntimeError("Native controller was not replayed into the stock main view")
            files.update({control.QMD_PAYLOAD_PATH: (qmd, 0o644),
                          control.BRIDGE_PAYLOAD_PATH: (bridges[architecture].read_bytes(), 0o644),
                          control.HELPER_PAYLOAD_PATH: (helper.read_bytes(), 0o755)})
        packed = tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False))
        asset = control._expected_asset_name(platform, firmware, release)
        packages.write_atomic(args.output / asset, packed)
        entries.append(dict(firmware=firmware, release_version=release, channel=channel, platform=platform,
            architecture=architecture, xochitl_sha256=stock, asset=asset, sha256=digest(packed), size=len(packed),
            urls=[f"{base}/{asset}" for base in control.REMOTE_BASE_URLS], package_revision=control.PACKAGE_REVISION,
            offline_verified=offline, device_verified=device,
            files=[dict(path=path, mode=mode, size=len(data), sha256=digest(data)) for path, (data, mode) in sorted(files.items())]))
        checks.append(dict(platform=platform, release=release, architecture=architecture, imports=imports,
            bridge_bytes=bridges[architecture].stat().st_size, helper_bytes=helper.stat().st_size,
            stock_xochitl=stock, swu_sha256=provenance["sha256"], address=address,
            webui_sha256=digest(webui)))
        print(f"PASS {platform}/{release}: {imports} ABI imports; native controller replayed", flush=True)
    if len({check["webui_sha256"] for check in checks}) != 1:
        raise RuntimeError("Native web-import implementations differ; review each protocol before packaging")
    document = json.loads(control.BUNDLED_MANIFEST.read_text(encoding="utf-8"))
    combined = {
        (item["platform"], item["firmware"], item["architecture"], item["xochitl_sha256"]): item
        for item in document["packages"]
    }
    for entry in entries:
        combined[(entry["platform"], entry["firmware"], entry["architecture"], entry["xochitl_sha256"])] = entry
    document["packages"] = list(combined.values())
    manifest = (json.dumps(document, indent=2) + "\n").encode()
    catalog = control.parse_manifest(manifest, require_local_match=False)
    built_identities = {
        (item["platform"], item["firmware"], item["architecture"], item["xochitl_sha256"])
        for item in entries
    }
    for package in catalog:
        identity = (package.platform, package.firmware, package.architecture, package.xochitl_sha256)
        if identity in built_identities:
            control.load_local_package(package, args.output / package.asset, str(ROOT / ".rmtool"))
    packages.write_atomic(control.BUNDLED_MANIFEST, manifest)
    control._trusted_catalog.cache_clear()
    packages.write_atomic(args.output / "validation.json", (json.dumps(checks, indent=2) + "\n").encode())
    print(f"{len(entries)} exact native-transfer packages built and cached; no publication or device deployment", flush=True)


if __name__ == "__main__":
    main()
