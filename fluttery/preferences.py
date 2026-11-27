"""Persistent preferences and measurement profile validation."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from platformdirs import user_config_path

from .staging import atomic_output

TAB_VALUES = ('measure', 'scope', 'turntable', 'thd', 'generate', 'results', 'settings')
TAB_LABELS = ('Measure', 'Oscilloscope', 'Turntable', 'Distortion', 'Generator', 'Results', 'Settings')
TURNTABLE_VALUES = ('rpm', 'crosstalk', 'balance', 'tracking', 'noise', 'resonance')
TURNTABLE_LABELS = ('Speed (RPM)', 'Crosstalk', 'Channel balance', 'Tracking distortion', 'Noise floor', 'Resonance')
RESULT_VALUES = ('fft', 'graph', 'drift', 'spectrogram', 'table', 'metadata', 'measurements', 'thd', 'stats')
RESULT_LABELS = ('Flutter spectrum', 'Wow and flutter', 'Speed drift', 'Spectrogram', 'Sample table', 'Session metadata', 'Measurements', 'THD+N results', 'Session statistics')
DETECTOR_PRESETS = {
    'nak': ('NAK T-100', 0.300, 0.010, 2.000),
    'fast': ('Fast setup', 0.100, 0.005, 0.500),
    'slow': ('Slow / steady', 1.000, 0.050, 3.000),
}
WEIGHTING_PRESETS = {
    'iec': ('IEC/DIN 45507', 4.0, 1.0, 1.0),
    'broad': ('Broad service', 4.0, 0.707, 1.0),
    'narrow': ('Narrow diagnostic', 4.0, 1.414, 1.0),
}
DETECTOR_FIELDS = ('rmsTime', 'peakAttackTime', 'peakReleaseTime')
WEIGHTING_FIELDS = ('centreFrequency', 'q', 'gain')
SAMPLE_RATES = (0, 44100, 48000, 88200, 96000, 176400, 192000)
SPECTROGRAM_RANGES = (40, 60, 80, 100, 120)
MAX_SWEEP_PRESETS = 100
DEFAULT_SWEEP_WAV = {'sample_rate': 48000, 'channels': 1, 'bit_depth': 24}
DEFAULT_PLOT_IMAGE = {
    'format': 'png', 'width': 1600, 'height': 900, 'dpi': 144, 'background': 'light', 'full_view': False,
    'show_title': True, 'show_legend': None, 'show_grid': None, 'show_cursors': True,
}


def preset_settings(presets, fields, name):
    return {'preset': name, **dict(zip(fields, presets[name][1:], strict=True))}


DEFAULT_EXPORT = {
    'includeMetadata': True,
    'includeDataTables': True,
    'includeAccessibility': True,
    'sessionData': dict.fromkeys(('fft', 'graph', 'drift', 'spectrogram', 'stats'), True),
    'turntableData': dict.fromkeys((*TURNTABLE_VALUES, 'thd', 'alignment', 'frequency-response',
                                   'bench-imd', 'bench-distortion'), True),
    'paperSize': 'A4',
    'scaleFactor': 1.0,
    'filenameTemplate': 'fluttery_report_{date}',
    'logoDataURL': None,
}
DEFAULTS = {
    'inputDevice': None,
    'outputDevice': None,
    'sampleRate': 0,
    'autoStartInput': True,
    'channels': 1,
    'monitoring': False,
    'monitoringMode': 'stereo',
    'monitoringVolume': 1.0,
    'targetFreq': 3000,
    'duration': 'continuous',
    'showUnsettledFrequency': False,
    'weightingStandard': 'iec',
    'flutterBand': 'rotational',
    'rmsDetector': 'exponential',
    'meterScale': 0.0,
    'tapeAutoStop': False,
    'tapeSilenceSeconds': 6.0,
    'tapeSilenceDb': -45.0,
    'detectorTiming': preset_settings(DETECTOR_PRESETS, DETECTOR_FIELDS, 'nak'),
    'weightingProfile': preset_settings(WEIGHTING_PRESETS, WEIGHTING_FIELDS, 'iec'),
    'splitFrequency': 6.0,
    'graphSource': 'detectorPeak',
    'testRecordSpeed': 33,
    'testRecordCustomRpm': 33.333333333333336,
    'turntable78Reference': 78.26,
    'enableQC': False,
    'qcLimit': 0.1,
    'useDCBlocker': True,
    'useAutoTarget': True,
    'useAdaptiveThreshold': True,
    'meterDamping': 0.1,
    'correctionFactor': 1.0,
    'channelCalibration': {'referenceFrequency': 3150, 'leftTrimDb': 0.0, 'rightTrimDb': 0.0},
    'voltageCalibrations': [],
    'responseCalibrations': [],
    'benchLevelOptions': {},
    'responseTestDefaults': {},
    'alignmentOptions': {},
    'headAzimuthOffsetUs': 0.0,
    'maxSessionSamples': 1000,
    'keepCompleteHistory': False,
    'keepSignalLossDetails': False,
    'includeInputAudio': False,
    'interfaceLoopbackPendingDisconnect': False,
    'workspaceLayout': 'windows',
    'technician': '',
    'theme': 'modern',
    'colorMode': 'auto',
    'textScale': 100,
    'enableUISounds': True,
    'soundOnFocus': True,
    'useAccessibility': True,
    'showHelpTags': True,
    'announceStatus': True,
    'defaultStartTab': 'measure',
    'defaultTurntableView': 'rpm',
    'defaultResultsView': 'fft',
    'measurementProfiles': [],
    'sweepPresets': [],
    'selectedMeasurementProfileId': 'iec-service',
    'exportSettings': DEFAULT_EXPORT,
    'debugLoggingEnabled': False,
    'checkForUpdates': True,
    'setupWizardSeen': False,
    'plotGrid': True,
    'plotGridDetail': 'major',
    'plotCanvas': 'system',
    'plotTraceStyle': 'standard',
    'plotPalette': 'theme',
    'plotLegend': 'auto',
    'plotPointMarkers': False,
    'plotImageExport': DEFAULT_PLOT_IMAGE,
    'spectrogramPalette': 'viridis',
    'spectrogramRange': 100,
}
PROFILE_KEYS = (
    'targetFreq', 'duration', 'weightingStandard', 'enableQC', 'qcLimit', 'detectorTiming',
    'splitFrequency', 'graphSource', 'weightingProfile', 'channels', 'testRecordSpeed', 'testRecordCustomRpm',
    'turntable78Reference',
    'useDCBlocker', 'useAutoTarget', 'useAdaptiveThreshold', 'flutterBand', 'rmsDetector', 'meterScale',
    'tapeAutoStop', 'tapeSilenceSeconds', 'tapeSilenceDb',
)


def number(value, minimum, maximum, fallback):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return min(maximum, max(minimum, value)) if math.isfinite(value) else fallback


def choice(value, values, fallback):
    return value if value in values else fallback


def numerical_choice(value, values, fallback):
    # Older settings and profile imports accepted numeric strings from input controls.
    parsed = number(value, -math.inf, math.inf, None) if isinstance(value, str) else value
    return choice(parsed, values, fallback)


def mapping(value):
    return value if isinstance(value, dict) else {}


def string(value, fallback='', limit=250):
    return value.strip()[:limit] if isinstance(value, str) else fallback


def normalize_preset(data, presets, fields, ranges):
    values = mapping(data)
    first = next(iter(presets))
    fallback = 'custom' if any(key in values for key in fields) else first
    name = choice(values.get('preset'), (*presets, 'custom'), fallback)
    if name != 'custom':
        return preset_settings(presets, fields, name)
    defaults = presets[first][1:]
    return {'preset': 'custom', **{
        key: number(values.get(key), bounds[0], bounds[1], default)
        for key, bounds, default in zip(fields, ranges, defaults, strict=True)
    }}


def normalize_export(data):
    values = mapping(data)
    result = deepcopy(DEFAULT_EXPORT)
    for key in ('includeMetadata', 'includeDataTables', 'includeAccessibility'):
        if isinstance(values.get(key), bool):
            result[key] = values[key]
    for group in ('sessionData', 'turntableData'):
        supplied = mapping(values.get(group))
        result[group].update({key: supplied[key] for key in result[group] if isinstance(supplied.get(key), bool)})
    result['paperSize'] = choice(values.get('paperSize'), ('A1', 'A2', 'A3', 'A4', 'A5', 'LETTER', 'LEGAL'), 'A4')
    result['scaleFactor'] = numerical_choice(values.get('scaleFactor'), (0.75, 1.0, 1.25, 1.5), 1.0)
    result['filenameTemplate'] = string(values.get('filenameTemplate'), result['filenameTemplate']) or result['filenameTemplate']
    logo = values.get('logoDataURL')
    if isinstance(logo, str) and len(logo) <= 28_000_000 and logo.startswith('data:image/'):
        result['logoDataURL'] = logo
    return result


def preferences_path():
    return user_config_path('Fluttery', appauthor=False, roaming=True) / 'preferences.json'


def normalize_sweep_presets(presets):
    if not isinstance(presets, list) or not presets:
        return []
    from .audio.sweep import SweepSettings

    seen = set()
    result = []
    for item in presets[:MAX_SWEEP_PRESETS]:
        item = mapping(item)
        name = string(item.get('name'), limit=80)
        key = name.casefold()
        if not name or key in seen or not isinstance(item.get('sweep'), dict):
            continue
        try:
            sweep = SweepSettings.from_dict(item['sweep'])
        except (TypeError, ValueError, OverflowError):
            continue  # One malformed record should never hide the other saved presets.
        wav = mapping(item.get('wav'))
        rate = number(wav.get('sample_rate'), -math.inf, math.inf, None)
        result.append({'name': name, 'sweep': sweep.to_dict(), 'wav': {
            'sample_rate': int(rate) if rate is not None and 8000 <= rate <= 192000 and rate.is_integer()
            else DEFAULT_SWEEP_WAV['sample_rate'],
            'channels': int(numerical_choice(wav.get('channels'), (1, 2), DEFAULT_SWEEP_WAV['channels'])),
            'bit_depth': int(numerical_choice(wav.get('bit_depth'), (16, 24, 32), DEFAULT_SWEEP_WAV['bit_depth'])),
        }})
        seen.add(key)
    return result


def normalize_plot_image(data):
    if not isinstance(data, dict):
        return DEFAULT_PLOT_IMAGE.copy()
    from .plot_export import MAX_IMAGE_PIXELS, MAX_IMAGE_SIZE, MIN_IMAGE_HEIGHT, MIN_IMAGE_WIDTH

    values = data
    result = DEFAULT_PLOT_IMAGE.copy()
    for key in ('full_view', 'show_title', 'show_legend', 'show_grid', 'show_cursors'):
        if isinstance(values.get(key), bool):
            result[key] = values[key]
    result['format'] = choice(values.get('format'), ('png', 'svg', 'pdf'), result['format'])
    result['background'] = choice(values.get('background'), ('light', 'dark', 'display', 'transparent'), result['background'])
    result['dpi'] = int(number(values.get('dpi'), 72, 600, result['dpi']))
    width = int(number(values.get('width'), MIN_IMAGE_WIDTH, MAX_IMAGE_SIZE, result['width']))
    height = int(number(values.get('height'), MIN_IMAGE_HEIGHT, MAX_IMAGE_SIZE, result['height']))
    if width * height <= MAX_IMAGE_PIXELS:
        result.update(width=width, height=height)
    return result


def normalize_settings(data):
    values = mapping(data)
    result = deepcopy(DEFAULTS)
    for key, default in DEFAULTS.items():
        if isinstance(default, bool) and isinstance(values.get(key), bool):
            result[key] = values[key]
    enums = {
        'monitoringMode': ('stereo', 'left', 'right', 'mono', 'mono_sum', 'difference'),
        'weightingStandard': ('iec', 'jis', 'unweighted', 'aes6'),
        'flutterBand': ('rotational', 'scrape'),
        'rmsDetector': ('exponential', 'oneSecond'),
        'duration': ('continuous', '10', '30', '60'),
        'graphSource': ('detectorPeak', 'rawBlockPeak'),
        'theme': ('modern', 'retro', 'neumorphism', 'neubrutalism'),
        'colorMode': ('auto', 'light', 'dark'),
        'workspaceLayout': ('windows', 'tabs'),
        'defaultStartTab': TAB_VALUES,
        'defaultTurntableView': TURNTABLE_VALUES,
        'defaultResultsView': RESULT_VALUES,
        'plotTraceStyle': ('fine', 'standard', 'bold'),
        'plotPalette': ('theme', 'blueOrange', 'highContrast'),
        'plotCanvas': ('system', 'light', 'dark'),
        'plotGridDetail': ('major', 'minor'),
        'plotLegend': ('auto', 'always', 'hidden'),
        'spectrogramPalette': ('viridis', 'magma', 'grayscale'),
    }
    for key, options in enums.items():
        result[key] = choice(values.get(key), options, DEFAULTS[key])
    result['channels'] = int(numerical_choice(values.get('channels'), (1, 2), DEFAULTS['channels']))
    # A supported rate negotiated by a device can be outside the common
    # choices. Keep its exact nominal rate for session replay.
    rate = number(values.get('sampleRate'), -math.inf, math.inf, 0.0)
    result['sampleRate'] = int(rate) if 8_000 <= rate <= 384_000 and rate.is_integer() else 0
    result['testRecordSpeed'] = numerical_choice(values.get('testRecordSpeed'), (50 / 3, 33, 45, 78, 0), 33)
    result['turntable78Reference'] = numerical_choice(values.get('turntable78Reference'), (78.26, 78.0), 78.26)
    result['spectrogramRange'] = int(numerical_choice(values.get('spectrogramRange'), SPECTROGRAM_RANGES, 100))
    result['meterScale'] = numerical_choice(values.get('meterScale'), (0, 0.1, 0.4, 1, 4), 0)
    numerical_ranges = {
        'targetFreq': (1, 24000), 'splitFrequency': (0.5, 30), 'qcLimit': (0.001, 100),
        'monitoringVolume': (0, 1), 'meterDamping': (0.0, 5), 'correctionFactor': (0.000001, 100),
        'maxSessionSamples': (100, 10000), 'textScale': (80, 150),
        'tapeSilenceSeconds': (1, 120), 'tapeSilenceDb': (-100, -6),
        'headAzimuthOffsetUs': (-500000, 500000), 'testRecordCustomRpm': (1, 300),
    }
    for key, (minimum, maximum) in numerical_ranges.items():
        result[key] = number(values.get(key), minimum, maximum, DEFAULTS[key])
    result['maxSessionSamples'] = int(result['maxSessionSamples'])
    result['textScale'] = int(result['textScale'])
    result['technician'] = string(values.get('technician'))
    result['detectorTiming'] = normalize_preset(
        values.get('detectorTiming'), DETECTOR_PRESETS, DETECTOR_FIELDS, ((0.05, 5), (0.001, 0.5), (0.05, 5)),
    )
    weighting = mapping(values.get('weightingProfile'))
    if 'centreFrequency' not in weighting and 'centerFrequency' in weighting:
        weighting = {**weighting, 'centreFrequency': weighting['centerFrequency']}
    result['weightingProfile'] = normalize_preset(
        weighting, WEIGHTING_PRESETS, WEIGHTING_FIELDS, ((0.5, 30), (0.1, 10), (0.1, 10)),
    )
    trims = mapping(values.get('channelCalibration'))
    result['channelCalibration'] = {
        'referenceFrequency': number(trims.get('referenceFrequency'), 1, 24000, 3150),
        'leftTrimDb': number(trims.get('leftTrimDb'), -24, 24, 0.0),
        'rightTrimDb': number(trims.get('rightTrimDb'), -24, 24, 0.0),
    }
    for key in ('inputDevice', 'outputDevice'):
        value = values.get(key)
        if isinstance(value, str) and value:
            result[key] = value[:500]
    result['exportSettings'] = normalize_export(values.get('exportSettings'))
    result['plotImageExport'] = normalize_plot_image(values.get('plotImageExport'))
    result['sweepPresets'] = normalize_sweep_presets(values.get('sweepPresets'))
    if values.get('responseCalibrations'):
        from .audio.response import normalize_response_calibrations

        result['responseCalibrations'] = normalize_response_calibrations(values['responseCalibrations'])
    if values.get('voltageCalibrations'):
        from .audio.levels import normalize_voltage_calibrations

        result['voltageCalibrations'] = normalize_voltage_calibrations(values['voltageCalibrations'])
    if values.get('benchLevelOptions'):
        from .audio.levels import normalize_bench_level_options

        result['benchLevelOptions'] = normalize_bench_level_options(values['benchLevelOptions'])
    if values.get('responseTestDefaults'):
        from .audio.response import normalize_response_test_defaults

        result['responseTestDefaults'] = normalize_response_test_defaults(values['responseTestDefaults'])
    if values.get('alignmentOptions'):
        from .audio.alignment import normalize_alignment_options

        result['alignmentOptions'] = normalize_alignment_options(values['alignmentOptions'])
    result['selectedMeasurementProfileId'] = string(values.get('selectedMeasurementProfileId'), 'iec-service', 80)
    profiles = values.get('measurementProfiles')
    if isinstance(profiles, list):
        # Strip nested profile lists before normalizing profile settings, including malformed files.
        ids = {item['id'] for item in BUILTIN_PROFILES}
        for item in profiles[:500]:
            profile = mapping(item)
            identifier = string(profile.get('id'), limit=80)
            name = string(profile.get('name'), limit=80)
            if identifier and name and identifier not in ids:
                ids.add(identifier)
                result['measurementProfiles'].append(make_profile(
                    name, mapping(profile.get('settings')), identifier, string(profile.get('description'), limit=180),
                ))
    return result


def make_profile(name, settings, identifier=None, description=''):
    subset = {key: settings[key] for key in PROFILE_KEYS if key in settings}
    duration = subset.get('duration')
    if duration in (10, 30, 60):
        # Electron also accepted numeric durations in saved measurement profiles.
        subset['duration'] = str(int(duration))
    normalized = normalize_settings(subset)
    return {
        'id': identifier or uuid4().hex, 'name': name.strip()[:80], 'description': description,
        'settings': {key: normalized[key] for key in PROFILE_KEYS},
    }


BUILTIN_PROFILES = [
    make_profile('IEC service check', {'targetFreq': 3150, 'duration': '30', 'enableQC': True}, 'iec-service',
                 '30-second IEC/DIN peak measurement with a 3,150 Hz reference.'),
    make_profile('JIS RMS check', {
        'duration': '30', 'weightingStandard': 'jis', 'enableQC': True, 'qcLimit': 0.15,
    }, 'jis-rms', '30-second JIS RMS measurement with a 3,000 Hz reference.'),
    make_profile('Quick QC', {
        'duration': '10', 'enableQC': True, 'qcLimit': 0.15, 'detectorTiming': {'preset': 'fast'},
    }, 'quick-qc', '10-second pass/fail measurement with a fast peak detector.'),
    make_profile('Turntable stereo setup', {'targetFreq': 3150, 'duration': '30', 'channels': 2}, 'turntable-stereo',
                 'Measure speed, channel separation, balance and tracking with a stereo 3,150 Hz reference.'),
    make_profile('AES6 weighted peak', {'targetFreq': 3150, 'duration': '30', 'weightingStandard': 'aes6',
                                       'useAutoTarget': False, 'rmsDetector': 'oneSecond'}, 'aes6-weighted',
                 '30-second run with a 3,150 Hz reference, two-sigma weighted peak and a one-second RMS window.'),
    make_profile('Tape scrape flutter', {'targetFreq': 12500, 'duration': '30', 'weightingStandard': 'unweighted',
                                        'flutterBand': 'scrape', 'useAutoTarget': False}, 'scrape-flutter',
                 '30-second scrape flutter measurement with a 12,500 Hz reference. Use a sample rate of 44.1 kHz or higher.'),
]


def load_settings(path: Path | None = None):
    path = path or preferences_path()
    legacy = False
    try:
        try:
            values = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            # The legacy app wrote flat preferences. Read them only for the first
            # launch. On a case-sensitive desktop Electron can use lowercase fluttery.
            siblings = (path.with_name('config.json'), path.parent.parent / 'fluttery' / 'config.json')
            legacy_path = next((candidate for candidate in siblings if candidate.is_file()), None)
            if legacy_path is None:
                return deepcopy(DEFAULTS)
            if legacy_path.stat().st_size > 30_000_000:
                return deepcopy(DEFAULTS)
            values = json.loads(legacy_path.read_text(encoding='utf-8'))
            legacy = True
    except (OSError, UnicodeError, ValueError, RecursionError):
        return deepcopy(DEFAULTS)
    if legacy:
        values = {key: value for key, value in mapping(values).items() if key not in ('inputDevice', 'outputDevice')}
        if values.get('monitoringMode') == 'mono':
            values['monitoringMode'] = 'mono_sum'
    return normalize_settings(values)


def save_settings(settings, path: Path | None = None):
    path = path or preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_output(path, mode='w') as stream:
        json.dump(settings, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write('\n')
