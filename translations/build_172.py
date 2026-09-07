"""Reproduce the exact .172 catalogs; preserve all published catalog bytes."""
import argparse
import hashlib
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    'ferrari': (196626, '49cf09fc23ef3fcacb956d426915e3f80b85a02fa7e597a8b5fc8013a2bdb931'),
    'chiappa': (192400, '2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1'),
    'tatsu': (192400, '2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1'),
    'legacy': (205621, '0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485'),
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--qt-bin', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for platform, expected in EXPECTED.items():
        sources = [ROOT / 'reMarkable_zh_CN.ts']
        if platform in {'ferrari', 'legacy'}:
            sources.append(ROOT / 'reMarkable_zh_CN_ferrari_supplement.ts')
        sources.append(ROOT / 'reMarkable_zh_CN_3_28_0_164_supplement.ts')
        if platform in {'ferrari', 'legacy'}:
            sources.append(ROOT / 'reMarkable_zh_CN_3_28_0_164_ferrari_supplement.ts')
        supplement_platform = (
            'ferrari' if platform == 'legacy'
            else 'chiappa' if platform == 'tatsu'
            else platform
        )
        sources.append(ROOT / f'reMarkable_zh_CN_3_28_0_166_{supplement_platform}_supplement.ts')
        if platform == 'legacy':
            sources.append(ROOT / 'reMarkable_zh_CN_legacy_supplement.ts')
        builds = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                ts = Path(temporary) / 'merged.ts'
                qm = Path(temporary) / 'compiled.qm'
                subprocess.run([str(args.qt_bin / 'lconvert.exe'), '-sort-contexts', '-locations', 'none',
                                *map(str, sources), '-o', str(ts)], check=True)
                subprocess.run([str(args.qt_bin / 'lrelease.exe'), '-nounfinished', str(ts), '-qm', str(qm)], check=True)
                builds.append(qm.read_bytes())
        if builds[0] != builds[1]:
            raise RuntimeError('Non-deterministic QM build')
        data = builds[0]
        actual = len(data), hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise RuntimeError(f'{platform}: catalog identity changed: {actual}')
        output_platform = platform if platform != 'legacy' else 'rm1-rm2'
        output = args.output_dir / f'reMarkable_zh_CN-3.28.0.172-{output_platform}.qm'
        output.write_bytes(data)
        print(platform, *actual)


if __name__ == '__main__':
    main()
