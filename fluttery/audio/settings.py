"""Engine settings and compatibility with saved measurement profiles."""

from math import isfinite

ALIASES = {
    "targetFreq": "target_frequency",
    "target_freq": "target_frequency",
    "weightingStandard": "weighting",
    "weighting_standard": "weighting",
    "useDCBlocker": "use_dc_blocker",
    "useAdaptiveThreshold": "use_adaptive_threshold",
    "useAutoTarget": "use_auto_target",
    "showUnsettledFrequency": "show_unsettled_frequency",
    "correctionFactor": "correction_factor",
    "detectorTiming": "detector_timing",
    "weightingProfile": "weighting_profile",
    "splitFrequency": "split_frequency",
    "flutterBand": "flutter_band",
    "rmsDetector": "rms_detector",
    "meterScale": "meter_scale",
    "channelCalibration": "channel_calibration",
    "channel_count": "channels",
    "stereoToneFrequency": "stereo_tone_frequency",
}

DEFAULTS = {
    "target_frequency": 3000.0,
    "weighting": "iec",
    "use_dc_blocker": True,
    "use_adaptive_threshold": True,
    "use_auto_target": True,
    "show_unsettled_frequency": False,
    "correction_factor": 1.0,
    "detector_timing": (0.300, 0.010, 2.000),
    "weighting_profile": (4.0, 1.0, 1.0),
    "weighting_profile_name": "iec",
    "split_frequency": 6.0,
    "flutter_band": "rotational",
    "rms_detector": "exponential",
    "meter_scale": 0.0,
    "channel_calibration": (0.0, 0.0),
    "stereo_tone_frequency": None,
    "channels": 1,
}

DETECTOR_PRESETS = {
    "nak": (0.300, 0.010, 2.000),
    "fast": (0.100, 0.005, 0.500),
    "slow": (1.000, 0.050, 3.000),
}

WEIGHTING_PRESETS = {
    "iec": (4.0, 1.0, 1.0),
    "broad": (4.0, 0.707, 1.0),
    "narrow": (4.0, 1.414, 1.0),
}


def bounded(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(high, max(low, number)) if isfinite(number) else default


def nested_value(value, *keys, default):
    return next((value[key] for key in keys if key in value), default)


def detector_timing(value):
    if not isinstance(value, dict):
        return DEFAULTS["detector_timing"]
    preset = value.get("preset")
    if isinstance(preset, str) and preset in DETECTOR_PRESETS:
        return DETECTOR_PRESETS[preset]
    return (
        bounded(nested_value(value, "rms_time", "rmsTime", default=0.300), 0.05, 5, 0.300),
        bounded(
            nested_value(value, "peak_attack_time", "peakAttackTime", default=0.010), 0.001, 0.5, 0.010
        ),
        bounded(
            nested_value(value, "peak_release_time", "peakReleaseTime", default=2.000), 0.05, 5, 2.000
        ),
    )


def weighting_profile(value):
    if not isinstance(value, dict):
        return DEFAULTS["weighting_profile"]
    preset = value.get("preset")
    if isinstance(preset, str) and preset in WEIGHTING_PRESETS:
        return WEIGHTING_PRESETS[preset]
    return (
        bounded(
            nested_value(
                value,
                "centreFrequency",
                "centerFrequency",
                "centre_frequency",
                "center_frequency",
                default=4,
            ),
            0.5,
            30,
            4.0,
        ),
        bounded(value.get("q"), 0.1, 10, 1.0),
        bounded(value.get("gain"), 0.1, 10, 1.0),
    )


def channel_calibration(value):
    if not isinstance(value, dict):
        return DEFAULTS["channel_calibration"]
    return (
        bounded(nested_value(value, "leftTrimDb", "left_trim_db", default=0), -24, 24, 0.0),
        bounded(nested_value(value, "rightTrimDb", "right_trim_db", default=0), -24, 24, 0.0),
    )


def measurement_settings(current, settings):
    normalized = current.copy()
    for key, value in settings.items():
        key = ALIASES.get(key, key)
        if key == "target_frequency":
            normalized[key] = bounded(value, 1, 24_000, 3000.0)
        elif key == "weighting":
            normalized[key] = value if value in ("iec", "jis", "unweighted", "aes6") else "iec"
        elif key in ("use_dc_blocker", "use_adaptive_threshold", "use_auto_target", "show_unsettled_frequency"):
            normalized[key] = bool(value)
        elif key == "correction_factor":
            normalized[key] = bounded(value, 0.000001, 100, 1.0)
        elif key == "split_frequency":
            normalized[key] = bounded(value, 0.5, 30, 6.0)
        elif key == "detector_timing":
            normalized[key] = detector_timing(value)
        elif key == "weighting_profile":
            normalized[key] = weighting_profile(value)
            preset = value.get("preset", "custom") if isinstance(value, dict) else "iec"
            if not isinstance(preset, str) or preset not in WEIGHTING_PRESETS:
                preset = "custom"
            normalized["weighting_profile_name"] = preset
        elif key == "flutter_band":
            normalized[key] = "scrape" if value == "scrape" else "rotational"
        elif key == "rms_detector":
            normalized[key] = "oneSecond" if value == "oneSecond" else "exponential"
        elif key == "meter_scale":
            scale = bounded(value, 0, 4, 0)
            normalized[key] = scale if scale in (0, 0.1, 0.4, 1.0, 4.0) else 0.0
        elif key == "channel_calibration":
            normalized[key] = channel_calibration(value)
        elif key == "stereo_tone_frequency":
            normalized[key] = bounded(value, 1, 24_000, None)
        elif key == "channels":
            normalized[key] = 2 if value in (2, "2") else 1
    return normalized
