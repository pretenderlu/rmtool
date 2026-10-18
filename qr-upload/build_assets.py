"""Package the replay-checked Move bridge and its pinned receiver for rmtool."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _plugin_packages as packages
import _qr_upload as qr
import _reading_enhancements as reading
import _tap_page_turn as tap


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receiver", type=Path, required=True)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--carrier", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--release", choices=[value[0] for value in qr.ALLOWED_TARGETS.values()], default="3.28.0.172")
    args = parser.parse_args()
    identity, verification = next((key, value) for key, value in qr.ALLOWED_TARGETS.items() if value[0] == args.release)
    platform, firmware, architecture, stock = identity
    carrier = reading.select_package(reading._trusted_catalog(), tap.DeviceIdentity(firmware, platform, architecture, stock))
    receiver = args.receiver.read_bytes()
    bridge = (args.native_dir / "rmtool-qr-upload.so").read_bytes()
    for data in (receiver, bridge):
        if data[:5] != b"\x7fELF\x02" or int.from_bytes(data[18:20], "little") != 183:
            raise RuntimeError("QR package requires AArch64 ELF payloads")
    if hashlib.sha256(receiver).hexdigest().encode() not in bridge:
        raise RuntimeError("Bridge does not pin this exact receiver")
    if b"/data/rmtool/xovi-standalone/helpers/rmtool-qr-upload" not in bridge:
        raise RuntimeError("Bridge still uses a canary path")
    with tempfile.TemporaryDirectory() as temp:
        extracted = reading.extract_verified_package(args.carrier, carrier, temp)
        files = {item.path: ((extracted / item.path).read_bytes(), item.mode)
                 for item in carrier.files if item.path in qr.REQUIRED_PATHS}
    files.update({
        qr.QMD_PAYLOAD_PATH: ((args.native_dir / "rmtool-qr-upload.qmd").read_bytes(), 0o644),
        qr.BRIDGE_PAYLOAD_PATH: (bridge, 0o644),
        qr.RECEIVER_PAYLOAD_PATH: (receiver, 0o755),
    })
    if set(files) != qr.REQUIRED_PATHS:
        raise RuntimeError("QR package payload set mismatch")
    archive = tap._gzip_member(tap._tar_member(files, apk_checksums=False, include_directories=False))
    release, channel, offline, device = verification
    asset = qr._expected_asset_name(platform, firmware, release)
    entry = dict(firmware=firmware, platform=platform, architecture=architecture, xochitl_sha256=stock,
        release_version=release, channel=channel, offline_verified=offline, device_verified=device,
        package_revision=qr.PACKAGE_REVISION, asset=asset, sha256=hashlib.sha256(archive).hexdigest(),
        size=len(archive), urls=[f"{base}/{asset}" for base in qr.REMOTE_BASE_URLS],
        files=[dict(path=p, mode=m, size=len(d), sha256=hashlib.sha256(d).hexdigest()) for p,(d,m) in sorted(files.items())])
    document = json.loads(qr.BUNDLED_MANIFEST.read_bytes())
    for index, previous in enumerate(document["packages"]):
        if previous["asset"] == asset:
            document["packages"][index] = entry
            break
    else:
        document["packages"].append(entry)
    manifest = (json.dumps(document, indent=2) + "\n").encode()
    qr.parse_manifest(manifest, require_local_match=False)
    packages.write_atomic(args.output_dir / asset, archive)
    packages.write_atomic(qr.BUNDLED_MANIFEST, manifest)
    qr._trusted_catalog.cache_clear()
    package = qr.select_package(qr.parse_manifest(manifest), tap.DeviceIdentity(firmware, platform, architecture, stock))
    qr.load_local_package(package, args.output_dir / asset, str(ROOT / ".rmtool"))
    print(f"Built and cached {asset}: {len(archive)} bytes; not published or deployed")


if __name__ == "__main__":
    main()
