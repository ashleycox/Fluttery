"""Shared NSIS inputs for the native and cross-compiled Windows installers."""

import os
from pathlib import Path

RECIPE = Path(__file__).resolve().parent


def installer_defines(bundle, output, version, architecture, work, icon=None):
    # Uninstall only files from this payload, then remove its empty directories.
    # A user can save sessions beside the program or anywhere in the chosen tree.
    manifest = work / 'uninstall-payload.nsh'
    files, directories = [], []
    for path in sorted(bundle.rglob('*')):
        relative = path.relative_to(bundle)
        target = '\\'.join(relative.parts).replace('$', '$$')
        if path.is_dir():
            directories.append((len(relative.parts), f'    RMDir "$INSTDIR\\{target}"'))
        elif relative.as_posix() != 'LICENSE':
            files.append(f'    Delete "$INSTDIR\\{target}"')
    commands = [*files, *(command for _, command in sorted(directories, reverse=True))]
    # A BOM lets NSIS interpret Unicode filenames on Windows and on Unix hosts.
    manifest.write_text('\n'.join(commands) + '\n', encoding='utf-8-sig')
    options = {
        'Output': output, 'Bundle': bundle, 'Version': version, 'TargetArch': architecture,
        'LicenseFile': RECIPE.parent / 'LICENSE', 'PayloadManifest': manifest,
    }
    if icon is not None:
        options['Icon'] = icon
    prefix = '/D' if os.name == 'nt' else '-D'
    return tuple(f'{prefix}{key}={value}' for key, value in options.items())
