"""Freeze a native Fluttery build and prepare its stable release asset name."""

from __future__ import annotations

import argparse
import base64
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / 'packaging'
ICON = ROOT / 'fluttery' / 'assets' / 'icon.png'
LINUX_DESKTOP_ENTRY = (
    '[Desktop Entry]\nType=Application\nName=Fluttery\nExec={command} %f\nIcon=fluttery\n'
    'Categories=AudioVideo;Utility;\nTerminal=false\n'
)


def _run(*args, env=None):
    subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True)


def _mac_bundle(app, framework, identity, output):
    if framework is not None:
        destination = app / 'Contents/Frameworks/Sparkle.framework'
        shutil.copytree(framework, destination, symlinks=True)
        # Sign from the inside out so the embedded framework and its helpers use the
        # same Developer ID as the application. The bundle retains its own entitlements.
        nested = sorted((path for path in destination.rglob('*') if not path.is_symlink()
                         and path.suffix in ('.xpc', '.app') and path.is_dir()),
                        key=lambda path: len(path.parts), reverse=True)
        helpers = sorted({path.resolve() for path in destination.glob('Versions/*/Autoupdate') if path.is_file()})
        for item in (*helpers, *nested, destination):
            _run('codesign', '--force', '--timestamp', '--options', 'runtime',
                 '--preserve-metadata=entitlements', '--sign', identity, item)
    if identity:
        _run('codesign', '--force', '--timestamp', '--options', 'runtime', '--sign', identity,
             '--entitlements', RECIPE / 'macos-entitlements.plist', app)
        _run('codesign', '--verify', '--deep', '--strict', app)
    _run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', app, output)


def _windows_bundle(bundle, output, release_version, architecture, work, icon):
    from windows_setup import installer_defines

    makensis = shutil.which('makensis')
    if not makensis:
        raise OSError('NSIS is required. Add makensis to PATH.')
    _run(makensis, *installer_defines(bundle, output, release_version, architecture, work, icon),
         RECIPE / 'installer.nsi')


def _linux_bundle(bundle, output, work, tool, architecture):
    appdir = work / 'Fluttery.AppDir'
    install_dir = appdir / 'usr/lib/fluttery'
    shutil.copytree(bundle, install_dir, symlinks=True)
    icon = appdir / 'fluttery.png'
    shutil.copy2(ICON, icon)
    (appdir / 'Fluttery.desktop').write_text(
        LINUX_DESKTOP_ENTRY.format(command='Fluttery'), encoding='utf-8',
    )
    launcher = appdir / 'AppRun'
    launcher.write_text('#!/bin/sh\nset -eu\nroot=$(dirname "$(readlink -f "$0")")\n'
                        'exec "$root/usr/lib/fluttery/Fluttery" "$@"\n', encoding='utf-8')
    launcher.chmod(0o755)
    env = {**os.environ, 'ARCH': {'x64': 'x86_64', 'arm64': 'aarch64'}[architecture]}
    _run(tool, appdir, output, env=env)


def _debian_bundle(bundle, output, work, tool, architecture, release_version):
    from PIL import Image

    package = work / 'Fluttery.deb-root'
    control = package / 'DEBIAN'
    launcher = package / 'usr/bin/fluttery'
    desktop = package / 'usr/share/applications/fluttery.desktop'
    icon = package / 'usr/share/icons/hicolor/256x256/apps/fluttery.png'
    session_type = package / 'usr/share/mime/packages/fluttery.xml'
    copyright_file = package / 'usr/share/doc/fluttery/copyright'
    for destination in (control / 'control', launcher, desktop, icon, session_type, copyright_file):
        destination.parent.mkdir(parents=True, exist_ok=True)
    # PyInstaller's Linux onedir output shares library targets via symlinks.
    shutil.copytree(bundle, package / 'usr/lib/fluttery', symlinks=True)
    launcher.write_text('#!/bin/sh\nexec /usr/lib/fluttery/Fluttery "$@"\n', encoding='utf-8')
    launcher.chmod(0o755)
    desktop.write_text(LINUX_DESKTOP_ENTRY.format(command='fluttery') + 'MimeType=application/x-fluttery;\n',
                       encoding='utf-8')
    with Image.open(ICON) as source:
        source.resize((256, 256), Image.Resampling.LANCZOS).save(icon)
    session_type.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
        '  <mime-type type="application/x-fluttery">\n'
        '    <comment>Fluttery session</comment>\n'
        '    <glob pattern="*.fluttery"/>\n'
        '  </mime-type>\n'
        '</mime-info>\n', encoding='utf-8',
    )
    copyright_file.write_text(
        'Fluttery by Ashley Cox and contributors.\n'
        'The Fluttery application code is distributed under GPL-3.0-only.\n\n'
        + (ROOT / 'LICENSE').read_text(encoding='utf-8'), encoding='utf-8',
    )
    installed_size = 0
    for entry in package.rglob('*'):
        if entry.is_symlink():
            continue
        if entry.is_dir():
            entry.chmod(0o755)
        elif entry.is_file():
            info = entry.stat()
            entry.chmod(0o755 if info.st_mode & 0o111 else 0o644)
            installed_size += (info.st_size + 1023) // 1024
    package.chmod(0o755)
    # The dependency floor matches the Ubuntu 24.04 environment used to freeze the app.
    debian_arch = {'x64': 'amd64', 'arm64': 'arm64'}[architecture]
    control_file = control / 'control'
    control_file.write_text(
        'Package: fluttery\n'
        f'Version: {release_version}\n'
        f'Architecture: {debian_arch}\n'
        'Section: sound\n'
        'Priority: optional\n'
        'Maintainer: Ashley Cox <info@theblindmansworkshop.com>\n'
        f'Installed-Size: {installed_size}\n'
        'Depends: libc6 (>= 2.39), libgtk-3-0t64 | libgtk-3-0, libportaudio2,\n'
        ' libgl1, libglu1-mesa, libnotify4, libsdl2-2.0-0, libxtst6,\n'
        ' libwebpdemux2, libcurl4t64 | libcurl4, libpcre2-32-0\n'
        'Recommends: desktop-file-utils, shared-mime-info\n'
        'Homepage: https://github.com/ashleycox/Fluttery\n'
        'Description: audio measurement and turntable calibration\n'
        ' Measure wow and flutter, inspect signals, generate test tones, and export reports.\n',
        encoding='utf-8',
    )
    control_file.chmod(0o644)
    _run(tool, '--root-owner-group', '--build', package, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--sparkle-framework', type=Path, help='Path to the official Sparkle.framework on macOS')
    parser.add_argument('--appimagetool', type=Path, help='Path to the appimagetool executable on Linux')
    parser.add_argument('--linux-format', choices=('appimage', 'deb', 'both'),
                        help='Linux package format (default: appimage)')
    args = parser.parse_args()
    system = platform.system()
    architecture = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    system_name = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(system)
    if not system_name or not architecture:
        parser.error('Build on a supported x64 or ARM64 host using a native Python interpreter.')
    identity = os.environ.get('FLUTTERY_MAC_SIGNING_IDENTITY', '')
    key = os.environ.get('FLUTTERY_SPARKLE_PUBLIC_KEY', '')
    if args.sparkle_framework and (system != 'Darwin' or not identity or not key):
        parser.error('Sparkle requires macOS, a signing identity and the Sparkle public key.')
    if args.sparkle_framework and not (args.sparkle_framework / 'Sparkle').is_file():
        parser.error('Supply the Sparkle.framework bundle directory.')
    if key and not args.sparkle_framework:
        parser.error('Supply the Sparkle framework when setting a Sparkle key.')
    if key:
        try:
            valid_key = len(base64.b64decode(key, validate=True)) == 32
        except ValueError:
            valid_key = False
        if not valid_key:
            parser.error('The Sparkle public key must be a base64-encoded Ed25519 public key.')
    if args.linux_format and system != 'Linux':
        parser.error('--linux-format is only available on Linux.')
    targets = {'Darwin': ('zip',), 'Windows': ('exe',), 'Linux': ('AppImage',)}[system]
    if args.linux_format == 'deb':
        targets = ('deb',)
    elif args.linux_format == 'both':
        targets = ('AppImage', 'deb')
    if 'AppImage' in targets and not args.appimagetool:
        parser.error('Provide --appimagetool to build a Linux AppImage.')
    dpkg_deb = shutil.which('dpkg-deb') if 'deb' in targets else None
    if 'deb' in targets and not dpkg_deb:
        parser.error('Add dpkg-deb to PATH to build a Debian package.')
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    release_version = version('fluttery')
    assets = {extension: output / f'fluttery-{system_name}-{architecture}.{extension}' for extension in targets}
    for asset in assets.values():
        if asset.exists() or asset.is_symlink():
            parser.error(f'Output already exists: {asset}')

    # Stage on the destination volume so a failed compressor leaves no asset
    # behind and a completed file can be published with a single rename.
    with (tempfile.TemporaryDirectory(prefix='fluttery-package-') as directory,
          tempfile.TemporaryDirectory(dir=output, prefix='.fluttery-release-') as staging):
        from PIL import Image

        work = Path(directory)
        icon = work / {'Darwin': 'Fluttery.icns', 'Windows': 'Fluttery.ico', 'Linux': 'fluttery.png'}[system]
        with Image.open(ICON) as image:
            image.save(icon)
        env = {**os.environ, 'FLUTTERY_BUILD_VERSION': release_version,
               'FLUTTERY_BUILD_ARCH': architecture, 'FLUTTERY_BUILD_ICON': str(icon)}
        _run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--distpath', work / 'dist',
             '--workpath', work / 'work', RECIPE / 'fluttery.spec', env=env)
        bundle = work / 'dist' / 'Fluttery'
        staged = {extension: Path(staging) / asset.name for extension, asset in assets.items()}
        if system == 'Darwin':
            _mac_bundle(bundle.with_suffix('.app'), args.sparkle_framework, identity, staged['zip'])
        elif system == 'Windows':
            _windows_bundle(bundle, staged['exe'], release_version, architecture, work, icon)
        else:
            if 'AppImage' in staged:
                _linux_bundle(bundle, staged['AppImage'], work, args.appimagetool.resolve(), architecture)
            if 'deb' in staged:
                _debian_bundle(bundle, staged['deb'], work, dpkg_deb, architecture, release_version)
        for asset in assets.values():
            if asset.exists() or asset.is_symlink():
                parser.error(f'Output already exists: {asset}')
        for extension, asset in assets.items():
            staged[extension].replace(asset)
    for asset in assets.values():
        print(asset)


if __name__ == '__main__':
    main()
