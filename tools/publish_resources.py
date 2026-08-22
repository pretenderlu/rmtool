"""Validate fixed GitHub resource releases and publish them to Tencent COS."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "pretenderlu/rmtool"
COS_BUCKET = "rmtool-localization-1254761827"
COS_REGION = "ap-shanghai"
COS_PUBLIC_BASE_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com"
)
MAX_DOWNLOAD_BYTES = 70 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){2,3}$")
ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}\.tar\.gz$")
LOCALIZATION_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.qm$")


@dataclass(frozen=True)
class Resource:
    name: str
    tag: str
    repository_manifest: Path
    object_prefix: str
    asset_prefix: str = ""
    extra_keys: frozenset[str] = frozenset()
    url_bases: tuple[str, str] | None = None

    @property
    def release_dir_name(self) -> str:
        return self.name


RESOURCES = {
    resource.name: resource
    for resource in (
        Resource(
            "localization",
            "localization-assets",
            ROOT / "translations" / "manifest.json",
            "",
        ),
        Resource(
            "tap-page-turn",
            "tap-page-turn-assets",
            ROOT / "tap-page-turn" / "manifest.json",
            "tap-page-turn",
            "rmtool-tap-page-turn-",
        ),
        Resource(
            "fast-mono-reading",
            "fast-mono-reading-assets",
            ROOT / "fast-mono-reading" / "manifest.json",
            "fast-mono-reading",
            "rmtool-fast-mono-reading-",
            frozenset({"offline_verified", "device_verified", "package_revision"}),
        ),
        Resource(
            "native-chinese",
            "native-chinese-assets",
            ROOT / "native-chinese" / "manifest.json",
            "native-chinese",
            "rmtool-native-chinese-",
            frozenset({"offline_verified", "device_verified", "urls"}),
            (
                f"{COS_PUBLIC_BASE_URL}/native-chinese",
                "https://github.com/pretenderlu/rmtool/releases/download/native-chinese-assets",
            ),
        ),
        Resource(
            "pinyin-input",
            "pinyin-input-assets",
            ROOT / "pinyin-input" / "manifest.json",
            "pinyin-input",
            "rmtool-pinyin-input-",
            frozenset({"offline_verified", "device_verified", "urls"}),
            (
                f"{COS_PUBLIC_BASE_URL}/pinyin-input",
                "https://github.com/pretenderlu/rmtool/releases/download/pinyin-input-assets",
            ),
        ),
        Resource(
            "reading-enhancements",
            "reading-enhancements-assets",
            ROOT / "reading-enhancements" / "manifest.json",
            "reading-enhancements",
            "rmtool-reading-enhancements-",
            frozenset(
                {
                    "offline_verified",
                    "device_verified",
                    "package_revision",
                    "urls",
                }
            ),
            (
                f"{COS_PUBLIC_BASE_URL}/reading-enhancements",
                "https://github.com/pretenderlu/rmtool/releases/download/reading-enhancements-assets",
            ),
        ),
    )
}


@dataclass(frozen=True)
class Bundle:
    resource: Resource
    release_dir: Path
    assets: tuple[str, ...]

    def object_key(self, name: str) -> str:
        return f"{self.resource.object_prefix}/{name}".lstrip("/")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json(payload: bytes, label: str):
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid JSON in {label}.") from exc


def _is_safe_relative_path(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in ("", ".", "..") for part in path.parts)
    )


def _require_exact_release_files(release_dir: Path, assets: dict[str, tuple[int, str]]):
    entries = list(release_dir.iterdir())
    if any(not path.is_file() for path in entries):
        raise RuntimeError("Release directory contains a non-file entry.")
    downloaded = {path.name for path in entries if path.name != "manifest.json"}
    if downloaded != set(assets):
        raise RuntimeError("Release payload set does not match its manifest.")
    for name, (expected_size, expected_digest) in sorted(assets.items()):
        payload = (release_dir / name).read_bytes()
        if len(payload) != expected_size or _sha256(payload) != expected_digest:
            raise RuntimeError(f"Release asset validation failed for {name}.")


def _validate_localization(resource: Resource, release_dir: Path) -> Bundle:
    repository_manifest = resource.repository_manifest.read_bytes()
    if (release_dir / "manifest.json").read_bytes() != repository_manifest:
        raise RuntimeError(
            "The localization-assets release manifest does not match "
            "translations/manifest.json byte-for-byte."
        )
    document = _read_json(repository_manifest, "localization manifest")
    firmwares = document.get("firmwares") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema") != 1
        or not isinstance(firmwares, dict)
        or not firmwares
    ):
        raise RuntimeError("The localization manifest has no firmware entries.")

    firmware_re = re.compile(r"^[0-9]{14}$")
    release_re = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
    platform_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
    max_payload_bytes = 16 * 1024 * 1024
    assets: dict[str, tuple[int, str]] = {}
    for firmware_version, firmware in firmwares.items():
        if not firmware_re.fullmatch(firmware_version) or not isinstance(firmware, dict):
            raise RuntimeError("Invalid firmware entry in localization manifest.")
        variants = firmware.get("variants", [])
        if not isinstance(variants, list):
            raise RuntimeError("Invalid hardware variants in localization manifest.")
        packages = [(firmware, False), *((item, True) for item in variants)]
        platform_releases = []
        stock_digests = []
        localized_digests = set()
        for package, require_platform in packages:
            if not isinstance(package, dict):
                raise RuntimeError("Invalid package entry in localization manifest.")
            name = package.get("asset")
            size = package.get("size")
            digest = package.get("sha256")
            stock_digest = package.get("stock_french_sha256")
            release_version = package.get("release_version")
            channel = package.get("channel")
            platform = package.get("platform", "")
            if (
                not isinstance(name, str)
                or not LOCALIZATION_ASSET_RE.fullmatch(name)
                or type(size) is not int
                or size <= 0
                or size > max_payload_bytes
                or not isinstance(digest, str)
                or not SHA_RE.fullmatch(digest)
                or not isinstance(stock_digest, str)
                or not SHA_RE.fullmatch(stock_digest)
                or not isinstance(release_version, str)
                or not release_re.fullmatch(release_version)
                or channel not in ("stable", "beta")
                or not isinstance(platform, str)
                or (platform and not platform_re.fullmatch(platform))
                or (require_platform and not platform)
            ):
                raise RuntimeError(f"Invalid localization asset metadata: {name!r}")
            metadata = (size, digest)
            if name in assets and assets[name] != metadata:
                raise RuntimeError(f"Conflicting size or SHA-256 metadata for {name}.")
            assets[name] = metadata
            platform_releases.append((platform.casefold(), release_version))
            stock_digests.append(stock_digest)
            localized_digests.add(digest)
        if variants and not firmware.get("platform"):
            raise RuntimeError("A variant manifest requires a base platform.")
        if len(platform_releases) != len(set(platform_releases)):
            raise RuntimeError("Duplicate hardware platform and release in firmware entry.")
        if len(stock_digests) != len(set(stock_digests)):
            raise RuntimeError("Duplicate stock carrier digest in firmware entry.")
        if any(digest in localized_digests for digest in stock_digests):
            raise RuntimeError("Stock and localized digests conflict.")

    _require_exact_release_files(release_dir, assets)
    return Bundle(resource, release_dir, tuple(sorted(assets)))


def _validate_feature(resource: Resource, release_dir: Path) -> Bundle:
    repository_manifest = resource.repository_manifest.read_bytes()
    if (release_dir / "manifest.json").read_bytes() != repository_manifest:
        raise RuntimeError(
            f"The {resource.name} release manifest does not match "
            f"{resource.repository_manifest.relative_to(ROOT)} byte-for-byte."
        )
    document = _read_json(repository_manifest, f"{resource.name} manifest")
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "packages"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("packages"), list)
        or not document["packages"]
    ):
        raise RuntimeError(f"Invalid {resource.name} manifest schema.")

    base_keys = {
        "firmware",
        "release_version",
        "channel",
        "platform",
        "architecture",
        "xochitl_sha256",
        "asset",
        "sha256",
        "size",
        "files",
    }
    file_keys = {"path", "sha256", "size", "mode"}
    assets: dict[str, tuple[int, str]] = {}
    identities = set()
    for package in document["packages"]:
        if not isinstance(package, dict) or set(package) != base_keys | resource.extra_keys:
            raise RuntimeError(f"Invalid {resource.name} package fields.")
        identity = (
            package.get("firmware"),
            package.get("platform"),
            package.get("xochitl_sha256"),
        )
        name = package.get("asset")
        size = package.get("size")
        digest = package.get("sha256")
        if (
            not isinstance(identity[0], str)
            or not re.fullmatch(r"[0-9]{14}", identity[0])
            or not isinstance(package.get("release_version"), str)
            or not VERSION_RE.fullmatch(package["release_version"])
            or package.get("channel") not in ("stable", "beta")
            or not isinstance(identity[1], str)
            or not TOKEN_RE.fullmatch(identity[1])
            or package.get("architecture") not in ("aarch64", "armv7l")
            or not isinstance(identity[2], str)
            or not SHA_RE.fullmatch(identity[2])
            or not isinstance(name, str)
            or not ASSET_RE.fullmatch(name)
            or not name.startswith(resource.asset_prefix)
            or type(size) is not int
            or size <= 0
            or size > 64 * 1024 * 1024
            or not isinstance(digest, str)
            or not SHA_RE.fullmatch(digest)
            or identity in identities
            or name in assets
        ):
            raise RuntimeError(f"Invalid or duplicate {resource.name} package: {name!r}")
        identities.add(identity)
        if resource.name in {
            "fast-mono-reading",
            "native-chinese",
            "pinyin-input",
            "reading-enhancements",
        } and (
            type(package.get("offline_verified")) is not bool
            or type(package.get("device_verified")) is not bool
        ):
            raise RuntimeError(f"Invalid {resource.name} verification metadata.")
        if resource.name in {"fast-mono-reading", "reading-enhancements"} and (
            type(package.get("package_revision")) is not int
            or package["package_revision"] <= 0
        ):
            raise RuntimeError(f"Invalid {resource.name} package revision.")
        # The runtime parsers accept the two mirrors in any order (GitHub is
        # now the default route), so validate them as a set of exact URLs.
        if resource.url_bases is not None and set(package.get("urls") or ()) != {
            f"{base}/{name}" for base in resource.url_bases
        }:
            raise RuntimeError(f"Invalid {resource.name} download URLs.")

        files = package.get("files")
        if not isinstance(files, list) or not files:
            raise RuntimeError(f"Invalid file list for {name}.")
        file_paths = set()
        for item in files:
            if not isinstance(item, dict) or set(item) != file_keys:
                raise RuntimeError(f"Invalid file metadata for {name}.")
            path = item.get("path")
            if (
                not _is_safe_relative_path(path)
                or path in file_paths
                or not isinstance(item.get("sha256"), str)
                or not SHA_RE.fullmatch(item["sha256"])
                or type(item.get("size")) is not int
                or item["size"] <= 0
                or type(item.get("mode")) is not int
                or not 0 <= item["mode"] <= 0o7777
            ):
                raise RuntimeError(f"Invalid nested file metadata for {name}.")
            file_paths.add(path)
        assets[name] = (size, digest)

    _require_exact_release_files(release_dir, assets)
    return Bundle(resource, release_dir, tuple(sorted(assets)))


def validate_releases(release_root: Path, resource_names) -> tuple[Bundle, ...]:
    bundles = []
    for name in resource_names:
        resource = RESOURCES[name]
        release_dir = release_root / resource.release_dir_name
        if not release_dir.is_dir():
            raise RuntimeError(f"Missing release directory: {release_dir}")
        bundle = (
            _validate_localization(resource, release_dir)
            if name == "localization"
            else _validate_feature(resource, release_dir)
        )
        bundles.append(bundle)
        print(f"Validated {len(bundle.assets)} {name} payloads.")
    return tuple(bundles)


def _request_bytes(url: str, *, limit: int, headers=None, timeout=60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "rmtool-publisher", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > limit:
            raise RuntimeError(f"Download exceeds the size limit: {url}")
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise RuntimeError(f"Download exceeds the size limit: {url}")
    return payload


def _github_release_assets(resource: Resource):
    tag = urllib.parse.quote(resource.tag, safe="")
    url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
    payload = _retry(
        f"GitHub release lookup for {resource.tag}",
        lambda: _request_bytes(url, limit=2 * 1024 * 1024),
    )
    document = _read_json(payload, resource.tag)
    assets = document.get("assets") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("tag_name") != resource.tag
        or not isinstance(assets, list)
    ):
        raise RuntimeError(f"Invalid GitHub release metadata for {resource.tag}.")
    result = []
    names = set()
    allowed_re = LOCALIZATION_ASSET_RE if resource.name == "localization" else ASSET_RE
    for item in assets:
        name = item.get("name") if isinstance(item, dict) else None
        url = item.get("browser_download_url") if isinstance(item, dict) else None
        size = item.get("size") if isinstance(item, dict) else None
        if name == "manifest.json":
            valid_name = True
        else:
            valid_name = isinstance(name, str) and bool(allowed_re.fullmatch(name))
        parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
        if (
            not valid_name
            or name in names
            or type(size) is not int
            or size <= 0
            or size > MAX_DOWNLOAD_BYTES
            or parsed is None
            or parsed.scheme != "https"
            or parsed.netloc != "github.com"
        ):
            raise RuntimeError(f"Unsafe GitHub release asset metadata in {resource.tag}.")
        names.add(name)
        result.append((name, url, size))
    if "manifest.json" not in names:
        raise RuntimeError(f"Missing manifest.json in {resource.tag}.")
    return result


def download_releases(download_root: Path) -> None:
    download_root.mkdir(parents=True, exist_ok=True)
    for resource in RESOURCES.values():
        release_dir = download_root / resource.release_dir_name
        release_dir.mkdir()
        for name, url, expected_size in _github_release_assets(resource):
            payload = _retry(
                f"GitHub download for {name}",
                lambda url=url: _request_bytes(
                    url, limit=MAX_DOWNLOAD_BYTES, timeout=120
                ),
            )
            if len(payload) != expected_size:
                raise RuntimeError(f"GitHub release asset size changed for {name}.")
            (release_dir / name).write_bytes(payload)
        print(f"Downloaded fixed release: {resource.tag}")


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"Missing credential file: {path}")
    values = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid .env line {line_number}.")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in {
            "TENCENT_CLOUD_SECRET_ID",
            "TENCENT_CLOUD_SECRET_KEY",
            "TENCENT_CLOUD_TOKEN",
        } or key in values:
            raise RuntimeError(f"Invalid or duplicate .env key on line {line_number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            raise RuntimeError(f"Empty .env value on line {line_number}.")
        values[key] = value
    for key in ("TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
        if key not in values:
            raise RuntimeError(f"Missing {key} in .env.")
    return values


def _public_url(key: str) -> str:
    quoted = urllib.parse.quote(key, safe="/")
    return f"{COS_PUBLIC_BASE_URL}/{quoted}?verify={secrets.token_hex(8)}"


def _public_matches(path: Path, key: str) -> bool:
    try:
        actual = _request_bytes(_public_url(key), limit=MAX_DOWNLOAD_BYTES, timeout=30)
    except Exception:
        return False
    expected = path.read_bytes()
    return len(actual) == len(expected) and _sha256(actual) == _sha256(expected)


def _retry(label: str, operation, attempts=3):
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            if attempt + 1 == attempts:
                raise RuntimeError(f"{label} failed after {attempts} attempts.") from exc
            time.sleep(2**attempt)


def _verify_public(path: Path, key: str) -> None:
    expected = path.read_bytes()

    def check():
        actual = _request_bytes(_public_url(key), limit=MAX_DOWNLOAD_BYTES, timeout=30)
        if actual != expected:
            raise RuntimeError("public object bytes differ")

    _retry(f"Public readback for {key}", check, attempts=5)
    print(f"Verified public object: {key}")


def make_cos_client(credentials):
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as exc:
        raise RuntimeError(
            "Tencent COS SDK is missing; install cos-python-sdk-v5==1.9.44."
        ) from exc
    config = CosConfig(
        Region=COS_REGION,
        SecretId=credentials["TENCENT_CLOUD_SECRET_ID"],
        SecretKey=credentials["TENCENT_CLOUD_SECRET_KEY"],
        Token=credentials.get("TENCENT_CLOUD_TOKEN"),
        Scheme="https",
    )
    return CosS3Client(config)


def publish_bundles(bundles: tuple[Bundle, ...], client) -> None:
    payload_objects = []
    for bundle in bundles:
        for name in bundle.assets:
            path = bundle.release_dir / name
            key = bundle.object_key(name)
            payload_objects.append((path, key))
            if _public_matches(path, key):
                print(f"Unchanged payload: {key}")
                continue

            def upload(path=path, key=key):
                client.upload_file(
                    Bucket=COS_BUCKET,
                    Key=key,
                    LocalFilePath=str(path),
                    PartSize=2,
                    MAXThread=3,
                    EnableMD5=True,
                )

            _retry(f"Payload upload for {key}", upload)
            print(f"Uploaded payload: {key}")

    for path, key in payload_objects:
        _verify_public(path, key)

    manifest_objects = []
    for bundle in bundles:
        path = bundle.release_dir / "manifest.json"
        key = bundle.object_key("manifest.json")
        manifest_objects.append((path, key))
        _retry(
            f"Manifest upload for {key}",
            lambda path=path, key=key: client.put_object(
                Bucket=COS_BUCKET,
                Key=key,
                Body=path.read_bytes(),
                ContentType="application/json",
            ),
        )
        print(f"Uploaded manifest last: {key}")

    for path, key in manifest_objects:
        _verify_public(path, key)


def _resource_names(values) -> tuple[str, ...]:
    return tuple(values or RESOURCES)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="validate downloaded fixed releases")
    verify.add_argument("--release-root", type=Path, required=True)
    verify.add_argument("--resource", action="append", choices=RESOURCES)
    publish = subparsers.add_parser("publish", help="download, validate, and publish all releases")
    publish.add_argument("--download-dir", type=Path, required=True)
    publish.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            validate_releases(args.release_root, _resource_names(args.resource))
        else:
            credentials = load_env(args.env_file)
            client = make_cos_client(credentials)
            download_releases(args.download_dir)
            bundles = validate_releases(args.download_dir, tuple(RESOURCES))
            publish_bundles(bundles, client)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
