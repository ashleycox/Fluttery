"""Exercise packaging filesystem operations without freezing or building an app."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[1] / 'packaging'


def test_release_path_contains_only_completed_assets(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('fluttery_release_builder', RECIPE / 'build.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(builder.shutil, 'which', lambda command: command)
    output = tmp_path / 'output'
    monkeypatch.setattr(sys, 'argv', [
        'build.py', '--output', str(output), '--linux-format', 'both', '--appimagetool', 'appimagetool',
    ])
    fail = True

    def compress(_bundle, path, *_args):
        path.write_bytes(b'partial or complete data')
        if fail and path.suffix == '.deb':
            raise subprocess.CalledProcessError(1, 'compression')

    monkeypatch.setattr(builder, '_run', lambda *args, **kwargs: None)
    monkeypatch.setattr(builder, '_linux_bundle', compress)
    monkeypatch.setattr(builder, '_debian_bundle', compress)
    with pytest.raises(subprocess.CalledProcessError):
        builder.main()
    assert list(output.iterdir()) == []

    fail = False
    builder.main()
    assert sorted(path.name for path in output.iterdir()) == ['fluttery-linux-x64.AppImage', 'fluttery-linux-x64.deb']


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer sharing requires the native Windows API.')
def test_frozen_app_holds_installer_marker_until_last_reader_closes(tmp_path):
    from fluttery.windows import hold_installation

    marker = tmp_path / '.running'
    with hold_installation(tmp_path):
        assert not marker.exists()
    marker.touch()
    with hold_installation(tmp_path):
        with hold_installation(tmp_path), pytest.raises(PermissionError):
            marker.unlink()
        with pytest.raises(PermissionError):
            marker.unlink()
    marker.unlink()
