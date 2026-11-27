# PyInstaller spec. The wrapper supplies platform metadata and a generated icon.
import os
import platform
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH).parent
system = platform.system()
version = os.environ['FLUTTERY_BUILD_VERSION']
arch = os.environ['FLUTTERY_BUILD_ARCH']
icon = os.environ.get('FLUTTERY_BUILD_ICON')
identity = os.environ.get('FLUTTERY_MAC_SIGNING_IDENTITY') or None
entitlements = str(root / 'packaging' / 'macos-entitlements.plist') if identity else None
manual = root / 'fluttery' / 'manual'
picture_platform = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}[system]
manual_data = [(str(path), 'fluttery/manual') for path in manual.glob('*.md')]
for folder in ('shared', picture_platform):
    pictures = manual / 'images' / folder
    if pictures.is_dir():
        manual_data.append((str(pictures), f'fluttery/manual/images/{folder}'))
analysis = Analysis(
    [str(root / 'fluttery' / '__main__.py')],
    pathex=[str(root)],
    datas=[(str(root / 'fluttery' / 'assets'), 'fluttery/assets'), *manual_data,
           (str(root / 'LICENSE'), '.'), *copy_metadata('fluttery', recursive=True)],
    hiddenimports=['matplotlib.backends.backend_agg', 'matplotlib.backends.backend_pdf', 'matplotlib.backends.backend_svg'],
    excludes=['matplotlib.backends.backend_tkagg', 'matplotlib.backends.backend_qtagg', 'tkinter', 'pytest'],
)
bundle = PYZ(analysis.pure)
executable = EXE(
    bundle, analysis.scripts, [], exclude_binaries=True, name='Fluttery', console=(system == 'Linux'),
    icon=icon, upx=False, codesign_identity=identity, entitlements_file=entitlements,
    target_arch={'x64': 'x86_64', 'arm64': 'arm64'}.get(arch) if system == 'Darwin' else None,
)
files = COLLECT(executable, analysis.binaries, analysis.datas, strip=False, upx=False, name='Fluttery')
if system == 'Darwin':
    info = {
        'CFBundleDisplayName': 'Fluttery',
        'CFBundleShortVersionString': version,
        'CFBundleVersion': version,
        # The ARM64 SciPy wheel tags omit the patch level. Its Mach-O files require 12.3.
        'LSMinimumSystemVersion': '12.3' if arch == 'arm64' else '10.15',
        'NSMicrophoneUsageDescription': 'Fluttery needs audio input to measure your equipment.',
        'CFBundleDocumentTypes': [{
            'CFBundleTypeExtensions': ['fluttery'], 'CFBundleTypeName': 'Fluttery Session',
            'CFBundleTypeRole': 'Editor', 'LSHandlerRank': 'Owner',
        }],
        'NSHighResolutionCapable': True,
    }
    public_key = os.environ.get('FLUTTERY_SPARKLE_PUBLIC_KEY')
    if public_key:
        info.update(
            SUFeedURL=f'https://github.com/ashleycox/Fluttery/releases/latest/download/appcast-macos-{arch}.xml',
            SUPublicEDKey=public_key,
            SUEnableAutomaticChecks=True,
        )
    app = BUNDLE(files, name='Fluttery.app', bundle_identifier='com.ashleycox.fluttery',
                 icon=icon, version=version, info_plist=info, codesign_identity=identity,
                 entitlements_file=entitlements)
