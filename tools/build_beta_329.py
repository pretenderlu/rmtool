"""Build offline-verified 3.29.0.152 candidates from exact official firmware."""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RELEASE = '3.29.0.152'
FIRMWARE = '20261009073202'
TARGETS = {
    'ferrari': ('aarch64', '5cbaee13e52938794297461430f7c2bbe136569df868353f565ad7022978a4b4', '7aa5d67da6850d7021ade5a2232edeb5f34550236eca6f7a102f37f962dd4bd2'),
    'chiappa': ('aarch64', '9a988a3c06b6ec6290d30901d220d7eb9929522438514827667ab47a2bddb603', '09ba8581a50d96c19b360606ee0288b8863faec620036c8eda3b2a0b5cc2d2e2'),
    'tatsu': ('aarch64', '241e33ac800d79d837b3467033a7e68f7667e949f93396999a80027369c483a3', '1bf16b6e7d5355e48479fa832871e98834307da652a06245b5fb86d0bbd7a26c'),
    'rm1': ('armv7l', '8ea9c8cf6b203e7feca3c30bbf854d16688006103ee035ae20c418c47761af34', '2eb775c3ced943c20ccf6271bb107e745db10aece2dc21e0e4bd8450d98fd23c'),
    'rm2': ('armv7l', 'fb1b115bf2fc38794663bbdc750b531582d94f176109de2a13bbdf7c313ce19c', '50a5ed69ab9c60d9f3b1d04b19311562194f6cfee769b17a6ea069d255e47760'),
}
FEATURES = ('tap-page-turn', 'native-chinese', 'pinyin-input', 'reading-enhancements', 'note-enhancements')
HASHTAB = 'exthome/qt-resource-rebuilder/hashtab'


def load(path):
    spec = importlib.util.spec_from_file_location(path.parent.name.replace('-', '_') + '_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(args):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode(errors='replace'))
    return result.stdout


def ts_messages(path):
    messages = {}
    for context in ET.parse(path).getroot().findall('context'):
        for message in context.findall('message'):
            translation = message.find('translation')
            if translation is not None and translation.get('type') in ('obsolete', 'vanished'):
                continue
            key = (context.findtext('name', ''), message.findtext('source', ''),
                   message.findtext('comment', ''), message.get('numerus') == 'yes')
            if key in messages:
                raise RuntimeError(f'Duplicate translation identity: {key}')
            messages[key] = translation
    return messages


def validate_catalog(stock_paths, merged_ts, qt, work, qml_ts):
    localized = ts_messages(merged_ts)
    required = set()
    for source in stock_paths:
        target = work / (source.stem + '.ts')
        run([qt / 'lconvert.exe', '-locations', 'none', source, '-o', target])
        required.update(ts_messages(target))
    required.update(ts_messages(qml_ts))
    missing = required - localized.keys()
    if missing:
        raise RuntimeError(f'Untranslated exact identities: {sorted(missing)}')
    for key, translation in localized.items():
        if translation is None or translation.get('type') == 'unfinished':
            raise RuntimeError(f'Unfinished translation: {key}')
        forms = translation.findall('numerusform') if key[3] else [translation]
        if not forms:
            raise RuntimeError(f'Missing plural form: {key}')
        for form in forms:
            text = ''.join(form.itertext())
            if not text.strip() or collections.Counter(re.findall(r'%(?:L?\d+|n)', key[1])) != collections.Counter(re.findall(r'%(?:L?\d+|n)', text)):
                raise RuntimeError(f'Invalid translation placeholders/text: {key}')
            if re.findall(r'</?\w+[^>]*>', key[1]) != re.findall(r'</?\w+[^>]*>', text):
                raise RuntimeError(f'Translation markup mismatch: {key}')
    return len(required)


def build_arm_translator(zig, work):
    # Qt's 32-bit qsizetype is int; leave already-published ARM payloads intact.
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        for name in ('hook.c', 'xovi.c', 'xovi.h', 'test_hook.c'):
            text = (ROOT / 'native-chinese/xovi-src' / name).read_text()
            text = text.replace('_ZN7QStringC1EPK5QCharx', '_ZN7QStringC1EPK5QChari')
            text = text.replace('typedef long long i64;', '')
            text = re.sub(r'\bi64\b', 'int', text)
            (source / name).write_text(text)
        executable = source / ('test_hook.exe' if os.name == 'nt' else 'test_hook')
        run([zig, 'cc', '-std=c11', '-DRMTOOL_RUNTIME_CATALOG', source / 'test_hook.c', '-o', executable])
        home = '/home/root/.local/share/rmtool/xovi-standalone'
        data = '/data/rmtool/xovi-standalone'
        for configured in (home, data, '', '/untrusted'):
            selected = home if configured == home else data
            subprocess.run([str(executable), selected + '/native-chinese/reMarkable_zh_CN.qm'],
                           env=dict(os.environ, XOVI_ROOT=configured), check=True, timeout=30)
        destination = work / 'native-chinese-translator-armv7-qt32.so'
        run([zig, 'cc', '-target', 'arm-linux-gnueabihf.2.4', '-std=c11', '-Oz', '-shared',
             '-fPIC', '-fno-stack-protector', '-s', '-DRMTOOL_RUNTIME_CATALOG',
             source / 'hook.c', source / 'xovi.c', '-o', destination])
        payload = destination.read_bytes()
        if b'I_ZN7QStringC1EPK5QChari\0' not in payload or b'_ZN7QStringC1EPK5QCharx' in payload:
            raise RuntimeError('Incorrect ARM QString ABI in generated translator')
        return payload


def abi_check(root, architecture, work):
    from capstone import Cs, CS_ARCH_ARM, CS_ARCH_ARM64, CS_MODE_ARM
    from elftools.elf.elffile import ELFFile
    process = '_ZN22QGuiApplicationPrivate15processKeyEventEPN29QWindowSystemInterfacePrivate8KeyEventE'
    imports = {
        'Core': ('_ZN11QTranslator4loadERK7QLocaleRK7QStringS5_S5_S5_',
                 '_ZN11QTranslator4loadERK7QStringS2_S2_S2_',
                 '_ZN7QStringC1EPK5QChar' + ('x' if architecture == 'aarch64' else 'i'), '_ZNK7QLocale8languageEv'),
        'Gui': (process, '_ZN17QInputMethodEvent15setCommitStringERK7QStringii'),
    }
    # Reviewed against the actual 3.29 binaries, not the old register allocation.
    evidence = (
        [('mov', 'x19, x0'), ('ldr', 'w2, [x19, #0x48]'),
         ('add', 'x7, x19, #0x50'), ('ldr', 'w20, [x19, #0x6c]')]
        if architecture == 'aarch64' else
        [('mov', 'r4, r0'), ('ldr', 'r2, [r4, #0x28]'),
         ('add', 'r8, r4, #0x2c'), ('ldr', 'r3, [r4, #0x3c]')]
    )
    result = {}
    for name, required in imports.items():
        path = root / f'usr/lib/libQt6{name}.so.6.10.3'
        with path.open('rb') as stream:
            elf = ELFFile(stream)
            expected_machine = 'EM_AARCH64' if architecture == 'aarch64' else 'EM_ARM'
            if elf['e_machine'] != expected_machine:
                raise RuntimeError(f'Wrong Qt architecture: {path}')
            table = elf.get_section_by_name('.dynsym')
            for symbol in required:
                matches = table.get_symbol_by_name(symbol) or []
                if len(matches) != 1 or matches[0]['st_shndx'] == 'SHN_UNDEF':
                    raise RuntimeError(f'Missing Qt export: {symbol}')
            if name == 'Gui':
                symbol = table.get_symbol_by_name(process)[0]
                section = elf.get_section(symbol['st_shndx'])
                start = symbol['st_value'] - section['sh_addr']
                dis = Cs(CS_ARCH_ARM64 if architecture == 'aarch64' else CS_ARCH_ARM, CS_MODE_ARM)
                instructions = [(i.mnemonic, i.op_str) for i in dis.disasm(section.data()[start:start+symbol['st_size']], symbol['st_value'])]
                if not all(item in instructions for item in evidence):
                    raise RuntimeError(f'Unverified IME field layout: {path}')
                (work / 'ime-abi.json').write_text(json.dumps(instructions, indent=2) + '\n')
        result[name] = dict(sha256=digest(path.read_bytes()), size=path.stat().st_size)
    return result


def highlight_abi_check(path, platform):
    from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
    from elftools.elf.elffile import ELFFile
    if platform not in ('chiappa', 'ferrari') or digest(path.read_bytes()) != TARGETS[platform][2]:
        raise RuntimeError('Unverified highlighter firmware identity')
    source = (ROOT / 'reading-enhancements/native/highlight_snap_329.h').read_text()
    pattern_text = source.split('TARGET_PATTERN[] = {', 1)[1].split('};', 1)[0]
    pattern = bytes(int(value, 16) for value in re.findall(r'0x([0-9a-f]{2})', pattern_text))
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name('.text')
        data = section.data()
        if elf['e_machine'] != 'EM_AARCH64' or data.count(pattern) != 1:
            raise RuntimeError('Missing unique executable highlighter signature')
        offset = data.index(pattern)
        address = section['sh_addr'] + offset
        instructions = list(Cs(CS_ARCH_ARM64, CS_MODE_ARM).disasm(data[offset:offset+0x220], address))
    # These five displaced instructions have no PC-relative operands.
    expected_prologue = [('paciasp', ''), ('stp', 'x29, x30, [sp, #-0x50]!'),
                         ('mov', 'x29, sp'), ('stp', 'x19, x20, [sp, #0x10]'), ('mov', 'x20, x1')]
    if [(i.mnemonic, i.op_str) for i in instructions[:5]] != expected_prologue:
        raise RuntimeError('Highlighter trampoline prologue changed')
    normalized = []
    for ins in instructions:
        if ins.mnemonic == 'bl':
            normalized.append('bl')
        elif ins.mnemonic == 'b' or ins.mnemonic.startswith('b.') or ins.mnemonic in ('cbz', 'cbnz', 'tbz', 'tbnz'):
            prefix, _, target = ins.op_str.rpartition('#')
            normalized.append(ins.mnemonic + ' ' + prefix + '#' + hex(int(target, 16)-address))
        else:
            normalized.append(ins.mnemonic + ' ' + ins.op_str)
    # Full reviewed expansion loop: wrapper +0, glyphs +8, UTF-16 +0x30,
    # glyph stride 0x38, range stride 0x20, start/end indices +0/+4.
    if digest('\n'.join(normalized).encode()) != 'eca4c7bf1f4fa94f45e72847f5bcb581addd14d02a45eb1c72b56164c0db9fa7':
        raise RuntimeError('Highlighter expansion layout changed')
    return dict(address=hex(address), xochitl_sha256=TARGETS[platform][2],
                instructions=[[hex(i.address), i.mnemonic, i.op_str] for i in instructions])


def validate_tap_navigation(qrex, gestures):
    directory = qrex / 'qml/device/view/documentview'
    scene = (directory / 'DeviceSceneView.qml').read_text(encoding='utf-8')
    document = (directory / 'DocumentView.qml').read_text(encoding='utf-8')
    stock = (directory / 'SceneViewGestures.qml').read_text(encoding='utf-8')
    for direction, method in (('left', 'moveBackward'), ('right', 'moveForward')):
        signal = direction + 'PageRequested'
        handler = 'on' + signal[0].upper() + signal[1:]
        if (f'view.{method}()' in gestures
                or gestures.count(f'view.{signal}()') != stock.count(f'view.{signal}()') + 1
                or f'view.{signal}()' not in stock
                or not re.search(rf'\bsignal\s+{signal}\b', scene)
                or not re.search(rf'{handler}:\s*\{{[^}}]*\broot\.{method}\(\)', document)
                or not re.search(rf'\bfunction\s+{method}\(\)', document)):
            raise RuntimeError(f'Unverified tap navigation signal: {signal}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--firmware-cache', type=Path, required=True)
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--qt-bin', type=Path, required=True)
    parser.add_argument('--qmd-tool', type=Path, required=True)
    parser.add_argument('--qmldiff', type=Path, required=True)
    parser.add_argument('--python-libs', type=Path, required=True)
    parser.add_argument('--zig', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.python_libs))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    arm_translator = build_arm_translator(args.zig, args.output_dir)
    highlight = args.output_dir / 'rmtool-highlight-snap-3.29.so'
    run([sys.executable, ROOT / 'reading-enhancements/native/build_highlight_snap.py',
         '--zig', args.zig, '--beta-329', '--output', highlight])
    base_builder = load(ROOT / 'native-chinese/build_172.py')
    records = {feature: [] for feature in FEATURES}
    report = {}
    carriers = []
    for platform, (architecture, swu_hash, xochitl_hash) in TARGETS.items():
        cache = args.firmware_cache / platform
        root = cache / 'extracted/selected-rootfs'
        swu = cache / f'remarkable-production-image-{RELEASE}-{platform}-public.swu'
        if digest(swu.read_bytes()) != swu_hash or digest((root / 'usr/bin/xochitl').read_bytes()) != xochitl_hash or (root / 'etc/version').read_text().strip() != FIRMWARE:
            raise RuntimeError(f'Firmware identity mismatch: {platform}')
        resource = args.resources / platform
        identity = json.loads((resource / 'identity.json').read_text())
        if identity['xochitl_sha256'] != xochitl_hash or identity['hashtab_sha256'] != digest((resource / 'hashtab').read_bytes()):
            raise RuntimeError(f'Resource provenance mismatch: {platform}')
        work = args.output_dir / platform
        work.mkdir(exist_ok=True)
        report[platform] = dict(identity=identity, abi=abi_check(root, architecture, work))
        if platform in ('chiappa', 'ferrari'):
            report[platform]['highlight_abi'] = highlight_abi_check(root / 'usr/bin/xochitl', platform)
        static_ts = work / 'static-qml.ts'
        run([args.qt_bin / 'lupdate.exe', resource / 'qrex-out', '-no-obsolete', '-locations', 'none', '-ts', static_ts])
        payloads = {}
        available = FEATURES if platform in ('ferrari', 'chiappa') else FEATURES[:3]
        for feature in available:
            if feature == 'reading-enhancements':
                # Bootstrap while the unpublished beta's payload whitelist changes.
                catalog = json.loads((ROOT / feature / 'manifest.json').read_text(encoding='utf-8'))
                catalog['packages'] = [p for p in catalog['packages'] if p['release_version'] == '3.28.0.172']
                import _reading_enhancements as reading
                packages = reading.parse_manifest(json.dumps(catalog).encode(), require_local_match=False)
            else:
                packages = base_builder._catalog(feature)
            previous = [p for p in packages if p.platform == platform and p.release_version == '3.28.0.172']
            if len(previous) != 1:
                raise RuntimeError(f'Missing unique predecessor: {feature}/{platform}')
            previous = previous[0]
            path, _ = base_builder._find_archive(previous, (ROOT / '.rmtool/cache', ROOT / 'build'))
            files = base_builder._extract_files(path, previous)
            files[HASHTAB] = ((resource / 'hashtab').read_bytes(), 0o644)
            qmd_path = f'exthome/qt-resource-rebuilder/{feature}.qmd'
            if feature == 'native-chinese':
                qmd_source = ROOT / 'native-chinese/qmd/native-chinese-3.29.qmd'
            elif feature == 'pinyin-input':
                qmd_source = ROOT / 'pinyin-input/qmd/pinyin-input.qmd'
            elif feature == 'tap-page-turn':
                qmd_source = ROOT / 'tap-page-turn/qmd-src/tap-page-turn-3.28.qmd'
            else:
                qmd_source = ROOT / feature / f'qmd-src/{feature}-3.28.qmd'
            with tempfile.TemporaryDirectory() as temporary:
                temp = Path(temporary)
                if feature == 'reading-enhancements':
                    builder = load(ROOT / feature / 'build_assets.py')
                    qmd_source = builder._source_for_target(qmd_source, FIRMWARE, RELEASE,
                                                           platform, architecture, xochitl_hash, temp / 'src')
                    files['extensions.d/rmtool-highlight-snap.so'] = (highlight.read_bytes(), 0o644)
                elif feature == 'tap-page-turn':
                    builder = load(ROOT / 'reading-enhancements/build_assets.py')
                    text = builder._tap_source_for_release(qmd_source.read_text(encoding='utf-8'), RELEASE)
                    qmd_source = temp / 'tap-page-turn-3.29.qmd'
                    qmd_source.write_text(text, encoding='utf-8', newline='\n')
                if feature in ('reading-enhancements', 'note-enhancements'):
                    builder = load(ROOT / feature / 'build_assets.py')
                    compiled = builder._compile_and_validate(
                        qmd_tool=args.qmd_tool, qmldiff=args.qmldiff, source=qmd_source,
                        target=dict(identity, id=platform + '-' + RELEASE, platform=platform,
                                    release_version=RELEASE), work=temp / 'validate')
                else:
                    compiled = run([args.qmd_tool, 'hash', '-hashtab', resource / 'hashtab', qmd_source])
                files[qmd_path] = (compiled, 0o644)
                if feature == 'native-chinese':
                    if architecture == 'armv7l':
                        files['extensions.d/native-chinese-translator.so'] = (arm_translator, 0o644)
                    old_qm = temp / 'previous.qm'
                    old_qm.write_bytes(files['native-chinese/reMarkable_zh_CN.qm'][0])
                    merged = temp / 'merged.ts'
                    run([args.qt_bin / 'lconvert.exe', '-sort-contexts', '-locations', 'none', old_qm,
                         ROOT / 'translations/reMarkable_zh_CN_3_29_0_152_supplement.ts', '-o', merged])
                    stock = root / 'usr/share/remarkable/xochitl/translations'
                    report[platform]['stock_and_qml_keys'] = validate_catalog([stock / f'reMarkable_{locale}.qm' for locale in ('en', 'fr', 'de', 'es')], merged, args.qt_bin, temp, static_ts)
                    qm = work / f'reMarkable_zh_CN-{RELEASE}-{platform}.qm'
                    run([args.qt_bin / 'lrelease.exe', '-nounfinished', merged, '-qm', qm])
                    first = qm.read_bytes()
                    run([args.qt_bin / 'lrelease.exe', '-nounfinished', merged, '-qm', temp / 'repeat.qm'])
                    if first != (temp / 'repeat.qm').read_bytes():
                        raise RuntimeError('Non-deterministic Chinese catalog')
                    files['native-chinese/reMarkable_zh_CN.qm'] = (first, 0o644)
                    carriers.append(dict(platform=platform, release_version=RELEASE, channel='beta',
                                         asset=qm.name, size=len(first), sha256=digest(first), xochitl_sha256=xochitl_hash,
                                         stock_french_sha256=digest((stock / 'reMarkable_fr.qm').read_bytes())))
            payloads[feature] = files
            module = __import__('_' + feature.replace('-', '_'))
            entry = dict(firmware=FIRMWARE, release_version=RELEASE, channel='beta', platform=platform,
                         architecture=architecture, xochitl_sha256=xochitl_hash)
            if feature == 'tap-page-turn':
                asset = f'rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}.tar.gz'
            else:
                entry.update(offline_verified=True, device_verified=False)
                if feature in ('reading-enhancements', 'note-enhancements'):
                    asset = module._expected_asset_name(platform, FIRMWARE, RELEASE)
                    entry['package_revision'] = module.PACKAGE_REVISION
                elif feature == 'native-chinese':
                    asset = f'rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}' + ('-runtime-catalog' if architecture == 'armv7l' else '') + '.tar.gz'
                else:
                    asset = f'rmtool-{feature}-{platform}-{FIRMWARE}-{RELEASE}.tar.gz'
                origins = (module.COS_URL, module.GITHUB_URL) if feature in ('native-chinese', 'pinyin-input') else module.REMOTE_BASE_URLS
                entry['urls'] = [f'{origin}/{asset}' for origin in origins]
            archive = base_builder.pack(files)
            entry.update(asset=asset, size=len(archive), sha256=digest(archive), files=[
                dict(path=p, size=len(data), sha256=digest(data), mode=mode) for p, (data, mode) in sorted(files.items())])
            destination = args.output_dir / feature / asset
            destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(archive)
            base_builder._extract_files(destination, base_builder._package(entry))
            records[feature].append(entry)
            print(f'{platform}/{feature}: {len(archive)} bytes', flush=True)
        groups = [('native-chinese', 'pinyin-input', 'tap-page-turn')]
        if platform in ('ferrari', 'chiappa'):
            groups.append(('native-chinese', 'pinyin-input', 'reading-enhancements', 'note-enhancements'))
        with tempfile.TemporaryDirectory() as temporary:
            for number, group in enumerate(groups):
                for reverse in (False, True):
                    ordered = group[::-1] if reverse else group
                    qmds = [(feature, payloads[feature][f'exthome/qt-resource-rebuilder/{feature}.qmd'][0]) for feature in ordered]
                    replay_work = Path(temporary) / f'{number}-{reverse}'
                    base_builder.replay(qmds, identity, args, replay_work)
                    gestures = (replay_work / 'replay/qml/device/view/documentview/SceneViewGestures.qml').read_text(encoding='utf-8')
                    validate_tap_navigation(resource / 'qrex-out', gestures)
        report[platform]['combined_qmd_orders'] = len(groups) * 2
        report[platform]['tap_navigation_signals_verified'] = True
    for feature, entries in records.items():
        (args.output_dir / feature / 'manifest.candidate.json').write_text(json.dumps(dict(schema=1, packages=entries), indent=2) + '\n')
    (args.output_dir / 'carriers.json').write_text(json.dumps(carriers, indent=2) + '\n')
    (args.output_dir / 'validation.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
