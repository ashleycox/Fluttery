"""Build Intel and Apple silicon Fluttery bundles from a single Mac."""

from __future__ import annotations

import argparse
import json
import platform
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    'x64': ('x86_64', 'macosx_10_15_x86_64'),
    'arm64': ('arm64', 'macosx_12_0_arm64'),
}


def _normalise_path(value):
    # Keep a venv's interpreter entry point intact. Resolving its symlink would
    # bypass pyvenv.cfg and makes Rosetta use the wrong environment on some hosts.
    return value.expanduser().absolute()


def _paths(args, target):
    selected = args.x64_python if target == 'x64' else args.arm64_python
    return _normalise_path(selected or args.python)


def _wheel_requirements():
    with (ROOT / 'pyproject.toml').open('rb') as stream:
        project = tomllib.load(stream)
    return [*project['build-system']['requires'], *project['project']['dependencies'],
            *project['project']['optional-dependencies']['build']]


def _target_commands(target, interpreter, scratch, output, framework, python_minor):
    machine, wheel_platform = TARGETS[target]
    host = ('/usr/bin/arch', f'-{machine}', interpreter)
    packages = scratch / 'wheelhouse'
    venv = scratch / 'venv'
    abi = python_minor.replace('.', '')
    local = ('/usr/bin/arch', f'-{machine}', venv / 'bin/python')
    first = (*host, '-m', 'venv', venv)
    download = (*local, '-m', 'pip', 'download', '--only-binary=:all:', '--platform', wheel_platform,
                '--implementation', 'cp', '--python-version', python_minor, '--abi', f'cp{abi}',
                '--dest', packages, *_wheel_requirements())
    install = (*local, '-m', 'pip', 'install', '--no-index', '--find-links', packages, '.[build]')
    build = (*local, ROOT / 'packaging/build.py', '--output', output)
    if framework is not None:
        build += ('--sparkle-framework', framework)
    return first, download, install, build


def _run(command, **options):
    return subprocess.run(tuple(map(str, command)), cwd=ROOT, check=True, **options)


def _interpreter_version(target, interpreter):
    machine, _ = TARGETS[target]
    result = _run((
        '/usr/bin/arch', f'-{machine}', interpreter, '-c',
        'import json,platform,sys; print(json.dumps([platform.machine(),sys.version_info[:2]]))',
    ), capture_output=True, text=True)
    reported, (major, minor) = json.loads(result.stdout)
    if reported != machine or (major, minor) < (3, 12):
        raise ValueError(f'{target} requires a {machine} Python 3.12 or newer.')
    return f'{major}.{minor}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=(*TARGETS, 'both'), default='both')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='Universal2 Python interpreter')
    parser.add_argument('--x64-python', type=Path, help='Override Python for Intel')
    parser.add_argument('--arm64-python', type=Path, help='Override Python for Apple silicon')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sparkle-framework', type=Path, help='Official universal2 Sparkle.framework')
    parser.add_argument('--plan', action='store_true', help='Print the commands without installing or building')
    args = parser.parse_args(argv)
    targets = TARGETS if args.arch == 'both' else (args.arch,)
    output = _normalise_path(args.output)
    framework = _normalise_path(args.sparkle_framework) if args.sparkle_framework else None
    if not args.plan and platform.system() != 'Darwin':
        parser.error('The macOS bundle is assembled on a Mac. Use an Apple silicon Mac with Rosetta for both targets.')
    for target in targets:
        interpreter = _paths(args, target)
        print(f'{target}: fluttery-macos-{target}.zip', flush=True)
        if args.plan:
            # A representative 3.12 path can be printed on any host; the real
            # commands get their wheel tags from the selected interpreter.
            commands = _target_commands(target, interpreter, Path(f'<temporary {target} folder>'), output, framework, '3.12')
            for command in commands:
                print(shlex.join(tuple(map(str, command))))
            continue
        try:
            python_minor = _interpreter_version(target, interpreter)
            with tempfile.TemporaryDirectory(prefix=f'fluttery-macos-{target}-') as scratch:
                for command in _target_commands(target, interpreter, Path(scratch), output, framework, python_minor):
                    _run(command)
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            parser.error(f'{target}: {error}')


if __name__ == '__main__':
    main()
