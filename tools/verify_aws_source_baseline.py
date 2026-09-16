"""Offline check that recovered application files still match the AWS baseline."""

from hashlib import sha256
import json
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads((root / 'docs/aws-source-baseline.json').read_text())
    differences = []
    for entry in baseline['files']:
        path = root / entry['path']
        if not path.is_file() or sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            differences.append(entry['path'])
    if differences:
        print('Files differ from the recorded AWS source baseline:')
        print('\n'.join(differences))
        return 1
    print(f"All {len(baseline['files'])} source/config/terms files match the recorded AWS baseline.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
