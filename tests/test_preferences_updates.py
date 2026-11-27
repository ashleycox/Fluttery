import hashlib
import io
import json
import stat

import pytest

from fluttery import updates
from fluttery.preferences import (
    BUILTIN_PROFILES,
    load_settings,
    make_profile,
    normalize_settings,
    save_settings,
)


def test_custom_profiles_keep_legacy_measurement_settings(tmp_path):
    custom = make_profile('Tape service', {
        'targetFreq': 3150, 'duration': 30, 'channels': 2,
        'detectorTiming': {'preset': 'custom', 'rmsTime': 0.54},
        'weightingProfile': {'preset': 'custom', 'centreFrequency': 6},
    })
    settings = normalize_settings({
        'measurementProfiles': [BUILTIN_PROFILES[0], custom, custom, {'id': 'broken'}],
        'channelCalibration': {'leftTrimDb': -2, 'rightTrimDb': 2, 'referenceFrequency': 1000},
        'exportSettings': {'sessionData': {'fft': False}, 'filenameTemplate': 'bench_{equipment}'},
        'workspaceLayout': 'tabs',
    })
    path = tmp_path / 'preferences.json'
    save_settings(settings, path)
    assert load_settings(path) == settings
    assert settings['measurementProfiles'] == [custom]
    assert settings['measurementProfiles'][0]['settings']['duration'] == '30'
    assert settings['measurementProfiles'][0]['settings']['detectorTiming']['rmsTime'] == 0.54
    assert settings['exportSettings']['sessionData']['fft'] is False
    assert settings['exportSettings']['sessionData']['drift'] is True
    assert settings['channelCalibration']['referenceFrequency'] == 1000
    assert settings['workspaceLayout'] == 'tabs'


def test_invalid_preferences_fall_back_and_interrupted_write_preserves_previous_settings(tmp_path):
    path = tmp_path / 'preferences.json'
    path.write_text('{invalid json', encoding='utf-8')
    assert load_settings(path)['duration'] == 'continuous'
    save_settings({'technician': 'Original'}, path)
    with pytest.raises(ValueError):
        save_settings({'bad': float('nan')}, path)
    assert json.loads(path.read_text(encoding='utf-8')) == {'technician': 'Original'}
    assert list(tmp_path.iterdir()) == [path]


def test_sweep_presets_round_trip_valid_rules_and_wav_settings(tmp_path):
    from fluttery.audio.sweep import SweepRule, SweepSettings

    sweep = SweepSettings(start_frequency=20, end_frequency=1000, rules=(
        SweepRule(200, waveform='triangle', level=0.5, level_curve='log', frequency_curve='linear'),
    ))
    first = {'name': '  Bench sweep ', 'sweep': sweep.to_dict(),
             'wav': {'sample_rate': '96000', 'channels': '2', 'bit_depth': 32}}
    valid = normalize_settings({'sweepPresets': [first, {**first, 'name': 'BENCH SWEEP'},
                                                 {'name': 'Broken', 'sweep': {'duration': -1}}]})
    assert valid['sweepPresets'] == [{'name': 'Bench sweep', 'sweep': sweep.to_dict(),
                                     'wav': {'sample_rate': 96000, 'channels': 2, 'bit_depth': 32}}]
    path = tmp_path / 'preferences.json'
    save_settings(valid, path)
    assert load_settings(path)['sweepPresets'] == valid['sweepPresets']



def test_first_launch_imports_legacy_preferences_without_changing_them(tmp_path):
    previous = tmp_path / 'config.json'
    original = {'weightingStandard': 'jis', 'targetFreq': 3150, 'inputDevice': 'legacy microphone id'}
    previous.write_text(json.dumps(original), encoding='utf-8')
    current = tmp_path / 'preferences.json'
    settings = load_settings(current)
    assert settings['weightingStandard'] == 'jis'
    assert settings['targetFreq'] == 3150
    assert settings['inputDevice'] is None
    assert json.loads(previous.read_text(encoding='utf-8')) == original
    assert not current.exists()


def test_legacy_linux_settings_and_american_weighting_spelling(tmp_path):
    directory = tmp_path / 'fluttery'
    directory.mkdir()
    (directory / 'config.json').write_text(json.dumps({
        'weightingProfile': {'centerFrequency': 7.5, 'q': 1.3},
        'monitoringMode': 'mono', 'channels': '2', 'testRecordSpeed': '45',
        'exportSettings': {'scaleFactor': '1.25'},
    }), encoding='utf-8')
    settings = load_settings(tmp_path / 'Fluttery' / 'preferences.json')
    assert settings['weightingProfile'] == {'preset': 'custom', 'centreFrequency': 7.5, 'q': 1.3, 'gain': 1}
    assert settings['autoStartInput'] is True
    assert settings['monitoringMode'] == 'mono_sum'
    assert normalize_settings({'monitoringMode': 'mono'})['monitoringMode'] == 'mono'
    assert (settings['channels'], settings['testRecordSpeed'], settings['exportSettings']['scaleFactor']) == (2, 45, 1.25)


def release_payload(asset_name, digest, tag='v1.1.0'):
    asset_url = f'{updates.REPOSITORY}/releases/download/{tag}/{asset_name}'
    return {'tag_name': tag, 'assets': [{'name': asset_name, 'size': 64,
                                         'digest': digest, 'browser_download_url': asset_url}]}


def test_update_requires_a_newer_stable_release_and_matching_asset():
    payload = release_payload('fluttery-linux-arm64.AppImage', f'sha256:{"a" * 64}')
    assert updates.parse_release(payload, '1.1.0', machine='aarch64') is None
    assert updates.parse_release({**payload, 'prerelease': True}, '1.0.0', machine='aarch64') is None
    assert updates.parse_release(payload, '1.0.0', machine='x86_64').asset is None
    release = updates.parse_release(payload, '1.0.0', machine='aarch64')
    assert release.version == '1.1.0'
    assert release.asset.size == 64
    payload['assets'][0]['browser_download_url'] = 'https://example.com/download'
    assert updates.parse_release(payload, '1.0.0', machine='aarch64').asset is None


def appimage_bytes(contents, machine=62):
    return b'\x7fELF\x02\x01\x01\x00AI\x02' + bytes(7) + machine.to_bytes(2, 'little') + contents


requires_appimage_api = pytest.mark.skipif(
    not all(hasattr(updates.os, name) for name in ('fchmod', 'O_NOFOLLOW', 'O_NONBLOCK')),
    reason='AppImage updates require POSIX link handling and file modes.',
)


@requires_appimage_api
def test_current_appimage_requires_the_mounted_fluttery_executable(monkeypatch, tmp_path):
    mount = tmp_path / 'mounted'
    executable = mount / 'usr/lib/fluttery/Fluttery'
    existing = tmp_path / 'fluttery.AppImage'
    existing.write_bytes(appimage_bytes(b'original'))
    existing.chmod(0o751)
    monkeypatch.setattr(updates.platform, 'system', lambda: 'Linux')
    monkeypatch.setenv('APPDIR', str(mount))
    monkeypatch.setenv('APPIMAGE', str(existing))

    monkeypatch.setattr(updates.sys, 'frozen', False, raising=False)
    assert updates.current_appimage() is None
    monkeypatch.setattr(updates.sys, 'frozen', True)
    monkeypatch.setattr(updates.sys, 'executable', str(tmp_path / 'another-install' / 'Fluttery'))
    assert updates.current_appimage() is None
    monkeypatch.setattr(updates.sys, 'executable', str(executable))
    assert updates.current_appimage() == existing

    link = tmp_path / 'symlink.AppImage'
    link.symlink_to(existing)
    monkeypatch.setenv('APPIMAGE', str(link))
    assert updates.current_appimage() is None
    monkeypatch.setenv('APPIMAGE', str(existing))
    existing.write_bytes(b'not an AppImage')
    assert updates.current_appimage() is None


@requires_appimage_api
def test_linux_file_replaced_only_after_complete_verified_download(monkeypatch, tmp_path):
    binary = appimage_bytes(b'verified update')
    original = appimage_bytes(b'original')
    existing = tmp_path / 'fluttery.AppImage'
    existing.write_bytes(b'original')
    existing.chmod(0o751)
    asset = updates.Asset('https://example.com/app', len(binary), hashlib.sha256(binary).hexdigest())
    with pytest.raises(OSError, match='supported type 2 AppImage'):
        updates.install_appimage(asset, existing)
    assert existing.read_bytes() == b'original'

    existing.write_bytes(original)
    monkeypatch.setattr(updates, '_open', lambda _url: io.BytesIO(binary))
    incorrect = updates.Asset(asset.url, asset.size, '0' * 64)
    with pytest.raises(ValueError, match='integrity'):
        updates.install_appimage(incorrect, existing)
    assert existing.read_bytes() == original

    wrong_architecture = appimage_bytes(b'other architecture', machine=183)
    monkeypatch.setattr(updates, '_open', lambda _url: io.BytesIO(wrong_architecture))
    incompatible = updates.Asset(asset.url, len(wrong_architecture), hashlib.sha256(wrong_architecture).hexdigest())
    with pytest.raises(ValueError, match='processor architecture'):
        updates.install_appimage(incompatible, existing)
    assert existing.read_bytes() == original

    class ReplacingDownload(io.BytesIO):
        def __exit__(self, *exc):
            existing.write_bytes(b'file changed while downloading')
            return super().__exit__(*exc)

    monkeypatch.setattr(updates, '_open', lambda _url: ReplacingDownload(binary))
    with pytest.raises(OSError, match='changed while the update was downloading'):
        updates.install_appimage(asset, existing)
    assert existing.read_bytes() == b'file changed while downloading'

    class FragmentedDownload(io.BytesIO):
        def read(self, size):
            return super().read(min(size, 7))

    existing.write_bytes(original)
    monkeypatch.setattr(updates, '_open', lambda _url: FragmentedDownload(binary))
    updates.install_appimage(asset, existing)
    assert existing.read_bytes() == binary
    assert stat.S_IMODE(existing.stat().st_mode) == 0o751
    assert list(tmp_path.iterdir()) == [existing]
