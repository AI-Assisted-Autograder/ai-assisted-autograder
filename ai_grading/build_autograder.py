"""Build an assignment package using exported backend configuration and one token."""

import argparse
import json
import math
from pathlib import Path
import shutil
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from local_client import read_assignment_token, validate_backend_url


ROOT = Path(__file__).resolve().parent
EXCLUDED = {'assignment.json', 'README.md', '.gitignore', '__pycache__'}


def build_package(package_dir, assignment_config, backend_url, token_file, output,
                  *, request_namespace='', timeout=150, total_timeout=300):
    package_dir = Path(package_dir).resolve()
    output = Path(output).resolve()
    backend_url = validate_backend_url(backend_url)
    if not backend_url.startswith('https://'):
        raise ValueError('Upload packages require a public HTTPS backend origin.')
    if not 0 < timeout <= 300:
        raise ValueError('Client timeout must be between zero and 300 seconds.')
    if not math.isfinite(total_timeout) or not 0 < total_timeout <= 600:
        raise ValueError('Total AI timeout must be between zero and 600 seconds.')
    UUID(assignment_config['id'])
    token = read_assignment_token(token_file)
    expected = json.loads((package_dir / 'assignment.json').read_text(encoding='utf-8'))
    configured = assignment_config['targets']
    if {target['identifier'] for target in expected['targets']} != {target['identifier'] for target in configured.values()}:
        raise ValueError('Exported backend targets do not match this assignment package.')
    targets = {}
    for target_id, target in configured.items():
        UUID(target_id)
        analysis = target['analysis']
        if not analysis.get('source_globs'):
            raise ValueError('Each configured target must select submitted source files.')
        # Only collection instructions are shipped. Rubrics and the authoritative
        # test plan are read from the database by the backend for every request.
        targets[target_id] = {
            'identifier': target['identifier'],
            'analysis': {
                'mode': analysis.get('mode', 'code_feedback'),
                'source_globs': analysis['source_globs'],
                'context_globs': analysis.get('context_globs', []),
                'evidence_sources': analysis.get('evidence_sources', []),
                'max_source_files': analysis.get('max_source_files'),
            },
        }
    files = {}
    for path in sorted(package_dir.rglob('*')):
        relative = path.relative_to(package_dir)
        if any(part in EXCLUDED or part.startswith('.') for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f'Package source cannot contain symlinks: {relative}')
        if not path.is_file():
            continue
        if path.suffix == '.zip' or any(word in path.name.lower() for word in ('credential', '.token', 'deploy_key')):
            raise ValueError(f'Unexpected private/generated package source: {relative}')
        files[relative.as_posix()] = path.read_bytes()
    if not {'setup.sh', 'run_autograder'}.issubset(files):
        raise ValueError('The package must provide setup.sh and run_autograder.')
    files['setup.sh'] = files['setup.sh'].replace(b'#!/usr/bin/env bash\n',
        b'#!/usr/bin/env bash\nchmod 600 /autograder/source/assignment.token\n', 1)
    for name in ('local_client.py', 'gradescope_feedback.py'):
        files[name] = (ROOT / name).read_bytes()
    files['config.json'] = (json.dumps({'backend_url': backend_url}, indent=2) + '\n').encode()
    files['ai_feedback.json'] = (json.dumps({'assignment_id': assignment_config['id'], 'targets': targets,
                                          'request_namespace': request_namespace, 'timeout': timeout,
                                          'total_timeout': total_timeout}, indent=2) + '\n').encode()
    files['assignment.token'] = (token + '\n').encode('ascii')
    if output.suffix != '.zip' or output.is_relative_to(package_dir):
        raise ValueError('Choose a .zip output outside the source package directory.')
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = output.with_suffix('')
    if runtime.exists() or output.exists():
        raise FileExistsError('Output ZIP and its matching runtime directory must be new paths.')
    runtime.mkdir(mode=0o700)
    try:
        for name, data in files.items():
            path = runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600 if name == 'assignment.token' else
                       0o755 if name == 'run_autograder' or name.endswith('.sh') else 0o644)
        with output.open('xb') as destination:
            output.chmod(0o600)
            with ZipFile(destination, 'w', compression=ZIP_DEFLATED) as archive:
                for path in sorted(runtime.rglob('*')):
                    if path.is_file():
                        member = ZipInfo(path.relative_to(runtime).as_posix())
                        member.create_system = 3
                        member.external_attr = (0o100000 | (path.stat().st_mode & 0o777)) << 16
                        archive.writestr(member, path.read_bytes(), compress_type=ZIP_DEFLATED)
    except Exception:
        # Both outputs were created by this build, and contain a private token.
        output.unlink(missing_ok=True)
        shutil.rmtree(runtime)
        raise
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--assignment-config', type=Path, required=True,
                        help='JSON from GET /internal/assignments/{id}/config')
    parser.add_argument('--backend-url', required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True,
                        help='New ZIP under an ignored directory, such as ai_grading/.local-feedback/')
    parser.add_argument('--request-namespace', default='',
                        help='Change for independent trials or after model/rubric updates; retain for retries')
    parser.add_argument('--timeout', type=float, default=150)
    parser.add_argument('--total-timeout', type=float, default=300,
                        help='Total AI phase budget in seconds, at most 600')
    args = parser.parse_args()
    try:
        assignment = json.loads(args.assignment_config.read_text(encoding='utf-8'))
        runtime = build_package(args.package_dir, assignment, args.backend_url, args.token_file,
                                args.output, request_namespace=args.request_namespace, timeout=args.timeout,
                                total_timeout=args.total_timeout)
    except Exception as exc:
        parser.exit(1, f'Build failed: {exc}\n')
    print(f'Created {args.output}; matching runtime files: {runtime}')
    print('Contains an assignment token. Keep the ZIP and runtime directory private and Git-ignored.')


if __name__ == '__main__':
    main()
