"""Stateful block processing for wow/flutter and the secondary analyzers."""

from collections import deque
from copy import deepcopy
from dataclasses import dataclass, replace
from math import ceil, exp, isfinite, pi, sin

import numpy as np
from scipy import signal

from .flutter_detection import (
    AES_CARRIER,
    AES_WINDOW,
    DRIFT_WINDOW,
    SCRAPE_BAND,
    SCRAPE_CARRIER,
    SCRAPE_MIN_RATE,
    RollingMaxima,
    SlidingRms,
    drift_band_rms,
    reference_weighting,
    repeating_frequency,
    statistical_peak,
)
from .scope import SCOPE_HISTORY_SECONDS, ScopeBuffer
from .settings import DEFAULTS, measurement_settings
from .spectrum import (
    FLOOR_DB,
    amplitude_rms,
    distortion,
    fitted_stereo_levels,
    level_db,
    power_db,
    power_spectrum,
    readonly,
)

ANALYSIS_FRAMES = 4096
ANALYSIS_QUEUE_LIMIT = 4096
SPECTRUM_FRAMES = 8192
LOW_FREQUENCY_SECONDS = 4
MIN_FLUTTER_SPECTRUM_SECONDS = 1
MIN_MEASUREMENT_RATE = 4_000
MAX_MEASUREMENT_RATE = 500_000


def as_frames(samples):
    frames = np.asarray(samples, dtype=np.float32)
    if frames.ndim == 1:
        frames = frames[:, np.newaxis]
    if frames.ndim != 2 or frames.shape[1] not in (1, 2):
        raise ValueError("Audio samples must contain one or two channels.")
    if not np.isfinite(frames).all():
        frames = np.nan_to_num(frames, nan=0.0, posinf=0.0, neginf=0.0)
    return frames


class SampleHistory:
    """Circular array retaining exactly the latest frames, with no shifts."""

    def __init__(self, capacity, channels):
        self.data = np.empty((capacity, channels), dtype=np.float32)
        self.index = 0
        self.count = 0

    def append(self, frames):
        capacity = len(self.data)
        frames = frames[-capacity:]
        count = len(frames)
        split = min(count, capacity - self.index)
        self.data[self.index : self.index + split] = frames[:split]
        self.data[: count - split] = frames[split:]
        self.index = (self.index + count) % capacity
        self.count = min(capacity, self.count + count)

    def latest(self, count=None):
        count = self.count if count is None else min(self.count, count)
        start = (self.index - count) % len(self.data)
        if start + count <= len(self.data):
            return self.data[start : start + count].copy()
        return np.concatenate((self.data[start:], self.data[: self.index]))


class StreamingFilter:
    def __init__(self, numerator, denominator, channels=1):
        self.numerator = numerator
        self.denominator = denominator
        self.state = np.zeros((max(len(numerator), len(denominator)) - 1, channels))

    def __call__(self, samples):
        output, self.state = signal.lfilter(
            self.numerator, self.denominator, samples, axis=0, zi=self.state
        )
        return output

    def seed(self, value):
        self.state[:] = signal.lfilter_zi(self.numerator, self.denominator)[:, np.newaxis] * value


def lowpass(cutoff, sample_rate, channels=1):
    alpha = (2 * pi * cutoff) / (sample_rate + 2 * pi * cutoff)
    return StreamingFilter([alpha], [1.0, alpha - 1], channels)


def empty_meter():
    return {"peak": 0.0, "rms": 0.0, "bufferPeak": 0.0}


def frequency_counter(samples, rate, adaptive_threshold, *, estimate_unsettled=False):
    if len(samples) < 4:
        return 0.0
    # Center the hysteresis on the input's own DC level. This is independent of
    # the wow/flutter DC blocker, which controls whether a speed error is metered.
    samples = samples - np.mean(samples, dtype=np.float64)
    threshold = (
        float(np.clip(amplitude_rms(samples) * 0.1, 0.001, 0.05)) if adaptive_threshold else 0.002
    )
    excursions = np.flatnonzero(np.abs(samples) >= threshold)
    if len(excursions) < 2:
        return 0.0
    signs = samples[excursions] > 0
    changed = np.flatnonzero(signs[1:] != signs[:-1]) + 1
    if len(changed) < 4:
        return 0.0

    if len(changed) > 0.8 * len(samples):
        # Near Nyquist some cycles never cross both thresholds at sampled
        # instants. A concentrated FFT peak resolves them without miscounting.
        spectrum = power_spectrum(samples, rate)
        frequency, _ = spectrum.peak(0.4 * rate, 0.5 * rate)
        main_lobe = 4 * spectrum.bin_width
        tone_power = spectrum.band_power(frequency - main_lobe, frequency + main_lobe)
        if frequency and tone_power >= 0.5 * np.sum(spectrum.power):
            return frequency

    # Each alternating excursion qualifies one crossing. Time its most recent
    # zero rather than the threshold to avoid level-dependent timing shifts.
    zeros = np.flatnonzero(np.signbit(samples[1:]) != np.signbit(samples[:-1])) + 1
    crossings = zeros[np.searchsorted(zeros, excursions[changed], side="right") - 1]
    before = samples[crossings - 1]
    after = samples[crossings]
    times = crossings - 1 - before / (after - before)
    periods = times[2:] - times[:-2]
    average = np.mean(periods)
    if average <= 0:
        return 0.0
    if np.std(periods) > 0.25 * average:
        if not estimate_unsettled:
            return 0.0
        # The median avoids letting one long missed cycle dominate the live
        # estimate. Values beyond Nyquist cannot be measured from this input.
        median = float(np.median(periods))
        return rate / median if median >= 2 else 0.0

    # Fit a common period to all crossings with separate offsets for rising
    # and falling edges. A distorted waveform need not have equal half cycles.
    first, second = times[::2], times[1::2]
    index = np.arange(len(first))
    cycles_first = index - (len(first) - 1) / 2
    cycles_second = index[:len(second)] - (len(second) - 1) / 2
    period = (cycles_first @ first + cycles_second @ second) / (
        cycles_first @ cycles_first + cycles_second @ cycles_second
    )
    return float(rate / period)


@dataclass
class _SpeedCaptureBuffer:
    values: np.ndarray
    valid: np.ndarray
    start: int
    completed: list
    interrupted: bool = False


class MeasurementProcessor:
    """Feed PCM frames of any length and take snapshots without an audio device."""

    def __init__(self, settings=None, sample_rate=48_000):
        self.settings = measurement_settings(DEFAULTS, settings or {})
        self.sample_rate = self._valid_sample_rate(sample_rate)
        self._sequence = 0
        self._measurements = deque(maxlen=ANALYSIS_QUEUE_LIMIT)
        self._speed_capture = None
        self._software_clock = False
        self.reset()

    @staticmethod
    def _valid_sample_rate(rate):
        try:
            rate = float(rate)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("A valid audio sample rate is required.") from None
        if not isfinite(rate) or not 8_000 <= rate <= 384_000:
            raise ValueError("The sample rate must be between 8,000 and 384,000 Hz.")
        return rate

    def reset(self):
        self.channels = self.settings["channels"]
        self.active_target = self._requested_target()
        self.frames_processed = 0
        self.elapsed_seconds = 0.0
        self._dropouts = 0
        self._detector_dropouts = 0
        self._auto_target_frames = 0
        self._calibration_history = deque(maxlen=ceil(65 * self.sample_rate / ANALYSIS_FRAMES))
        self._measurements.clear()
        self._reset_history()
        self._reset_filters()
        self._metrics = self._empty_metrics()

    @property
    def clock_factor(self):
        # A sweep or tone generated for the internal analyser has no physical
        # input clock to calibrate. Keep the saved correction for the device.
        return 1.0 if self._software_clock else self.settings['correction_factor']

    @property
    def uses_software_clock(self):
        return self._software_clock

    @property
    def measurement_rate(self):
        return self.sample_rate * self.clock_factor

    @property
    def clock_unsupported(self):
        # Allow correction at either nominal device-rate limit. Wild values
        # in an older settings file must not create unbounded filter buffers
        # or claim to measure bands the corrected sample clock cannot carry.
        return not MIN_MEASUREMENT_RATE <= self.measurement_rate <= MAX_MEASUREMENT_RATE

    @property
    def _detector_rate(self):
        return self.sample_rate if self.clock_unsupported else self.measurement_rate

    def use_software_clock(self, enabled):
        if enabled == self._software_clock:
            return False
        self._software_clock = enabled
        self._reset_clock()
        return True

    def _reset_clock(self):
        self.active_target = self._requested_target()
        self._auto_target_frames = 0
        self.reset_signal()

    def retain_measurements(self, complete):
        """Keep every queued block during an opted-in full-history observation."""
        limit = None if complete else ANALYSIS_QUEUE_LIMIT
        if limit != self._measurements.maxlen:
            self._measurements = deque(self._measurements, maxlen=limit)

    def begin_speed_capture(self):
        # Keep the pending PCM until the completed detector block says whether
        # its reference was trustworthy. Start at the current input position.
        self._speed_capture = _SpeedCaptureBuffer(np.empty(ANALYSIS_FRAMES, dtype=np.float32),
                                                  np.empty(ANALYSIS_FRAMES, dtype=bool), self._block_count, [])

    def drain_speed_capture(self):
        if self._speed_capture is None:
            return ()
        frames, self._speed_capture.completed = self._speed_capture.completed, []
        return frames

    @property
    def speed_capture_interrupted(self):
        return self._speed_capture is not None and self._speed_capture.interrupted

    def end_speed_capture(self, *, keep_tail=False):
        if keep_tail and self._speed_capture is not None and self._block_count > self._speed_capture.start:
            block = self._raw_block[:self._block_count]
            detected = self._observed_frequency(block, amplitude_rms(block)) * self.clock_factor
            candidate = self._automatic_reference(detected)
            locked = (self._within_reference(detected) and candidate in (None, self.active_target)
                      and np.max(np.abs(block)) < .9995)
            self._complete_speed_block({'locked': locked})
        frames = self.drain_speed_capture()
        self._speed_capture = None
        return frames

    def _complete_speed_block(self, reading):
        capture = self._speed_capture
        if capture is None:
            return
        segment = slice(capture.start, self._block_count)
        if capture.start < self._block_count:
            capture.completed.append((capture.values[segment].copy(), capture.valid[segment] & reading['locked'], reading))
        capture.start = 0

    def _requested_target(self):
        if self.settings["flutter_band"] == "scrape":
            return SCRAPE_CARRIER
        if self.settings["weighting"] == "aes6":
            return AES_CARRIER
        return min(self.settings["target_frequency"], self._detector_rate * 0.49)

    def _reset_history(self):
        self._unsettled_frequency = 0.0
        rate = self._detector_rate
        spectrum_frames = max(SPECTRUM_FRAMES, ceil(SPECTRUM_FRAMES * rate / 48_000))
        self.input_history = SampleHistory(spectrum_frames, 2)
        # A separate ring preserves the analyser FFT size while allowing the
        # scope to find single-shot edges between desktop timer ticks.
        self._scope_input = SampleHistory(ceil(SCOPE_HISTORY_SECONDS * rate), self.channels)
        self._scope_input_epoch = object()
        self._raw_block = np.empty(ANALYSIS_FRAMES, dtype=np.float32)
        self._block_count = 0
        if self._speed_capture is not None:
            self._speed_capture.start = 0
            self._speed_capture.interrupted = True
        self._block_peak = np.zeros(3)
        self._decimation = max(1, int(rate / 1024))
        self._low_rate = rate / self._decimation
        self._low_sos = signal.butter(8, 300, fs=rate, output="sos")
        self._low_filter_state = np.zeros((len(self._low_sos), 2, 4))
        self._low_history = SampleHistory(ceil(LOW_FREQUENCY_SECONDS * self._low_rate), 2)
        self._diagnostic_pending = SampleHistory(ceil(ANALYSIS_FRAMES / self._decimation), 2)
        self.last_unweighted_speed = readonly(np.empty(0, dtype=np.float32))
        self.last_speed_valid = readonly(np.empty(0, dtype=bool))
        self._secondary_frame = -1
        self._secondary_cache = None
        self._spectra = None

    @property
    def _scrape(self):
        return self.settings["flutter_band"] == "scrape"

    @property
    def _aes6(self):
        return not self._scrape and self.settings["weighting"] == "aes6"

    @property
    def _unsupported_band(self):
        # 44.1 kHz is a nominal setting. A small correction at that rate
        # still leaves enough bandwidth for both sidebands and the IQ filter.
        return self._scrape and (self.sample_rate < SCRAPE_MIN_RATE or self._detector_rate < 40_000)

    def _reset_filters(self):
        rate = self._detector_rate
        self._oscillator_phase = 0.0
        self._measured_phase = 0.0
        self._measured_phase_valid = False
        # Reject the doubled-carrier image before taking the phase derivative.
        cutoff = min(320.0, self.active_target / 4)
        settling_time = 5 / (2 * pi * cutoff)
        order = 6
        nyquist_margin = rate / 2 - self.active_target
        if self._scrape and not self._unsupported_band:
            # Keep both FM sidebands of the 12.5 kHz carrier through 5 kHz.
            # A steep IQ low-pass protects the wanted baseband from the
            # doubled-carrier image even at 44.1 kHz.
            cutoff, order = 6500.0, 10
        elif not self._unsupported_band and 0 < nyquist_margin < 8 * cutoff:
            # A high reference puts the folded image close to baseband. Sharpen
            # and narrow the filter only here, preserving the normal passband.
            order = 8
            cutoff = min(cutoff, nyquist_margin / 2)
            settling_time = 5 / (2 * pi * cutoff * sin(pi / (2 * order)))
        self._settling_length = ceil(max(0.04, settling_time) * rate)
        self._settling_samples = self._settling_length
        self._iq_sos = signal.butter(order, cutoff, fs=rate, output="sos")
        self._iq_filter_state = np.zeros((len(self._iq_sos), 2, 2))
        self._dc_filter = StreamingFilter([1, -1], [1, 2 * pi * 0.5 / rate - 1])
        self._split_filter = lowpass(self.settings["split_frequency"], rate)
        self._reference_curve = self._aes6 or self.settings["weighting_profile_name"] == "iec"
        self._weighting_sos = None
        if self._scrape and not self._unsupported_band:
            self._weighting_sos = signal.butter(4, SCRAPE_BAND, btype="bandpass", fs=rate, output="sos")
        elif self._reference_curve and self.settings["weighting"] != "unweighted":
            self._weighting_sos = reference_weighting(rate)
        if self._weighting_sos is not None:
            self._weighting_state = np.zeros((len(self._weighting_sos), 2, 1))
            self._weighting_dc_state = signal.sosfilt_zi(self._weighting_sos)[:, :, np.newaxis]
        self._weighting_needs_seed = True
        center, quality, gain = self.settings["weighting_profile"]
        angular_frequency = 2 * pi * center / rate
        alpha = sin(angular_frequency) / (2 * quality)
        self._weighting_filter = StreamingFilter(
            [alpha * gain, 0, -alpha * gain],
            [1 + alpha, -2 * np.cos(angular_frequency), 1 - alpha],
        )
        rms, attack, release = self.settings["detector_timing"]
        rms_decay = exp(-1 / (rate * rms))
        self._rms_filter = StreamingFilter([1 - rms_decay], [1, -rms_decay], 3)
        self._peak_attack = 1 - exp(-1 / (rate * attack))
        self._peak_release = 1 - exp(-1 / (rate * release))
        self._meter_peak = [0.0, 0.0, 0.0]
        self._meter_rms = np.zeros(3)
        self._one_second = SlidingRms(round(rate), 3)
        self._rolling = RollingMaxima()
        self._valid_history = SampleHistory(ceil(2 * DRIFT_WINDOW * self._low_rate), 2)
        # Scrape FFTs use enough bandwidth to expose the full 5 kHz band.
        self._scrape_decimation = max(1, int(rate / 16_000))
        self._scrape_rate = rate / self._scrape_decimation
        self._scrape_history = SampleHistory(ceil(2 * self._scrape_rate) if self._scrape else 1, 1)
        self._scrape_pending = SampleHistory(ceil(ANALYSIS_FRAMES / self._scrape_decimation) if self._scrape else 1, 1)
        scope_rate = self._scrape_rate if self._scrape else self._low_rate
        self._scope_variation = SampleHistory(ceil(SCOPE_HISTORY_SECONDS * scope_rate), 1)
        self._scope_variation_epoch = object()
        self._variation_valid = 0
        self._scrape_filter = None
        if self._scrape and not self._unsupported_band:
            self._scrape_filter = signal.butter(8, 5800, fs=rate, output="sos")
            self._scrape_filter_state = np.zeros((len(self._scrape_filter), 2))
        self._invalid_frames = 0
        self._diagnostic_pending.count = 0
        self._period_frame = -int(rate)
        self._period = (None, None)
        self._drift_rms = None

    def reset_statistics(self):
        """Start new observation windows while retaining the running signal filters."""
        self._rolling.reset()
        self._reset_ballistics()
        self._valid_history.count = 0
        self._scrape_history.count = 0
        self._period_frame = -int(self._detector_rate)
        self._period = (None, None)
        self._drift_rms = None
        # Earlier completed blocks may still be queued for recording.
        self._metrics = {**self._metrics, **self._empty_statistics()}

    def _reset_ballistics(self):
        self._one_second.reset()
        self._meter_peak = [0.0, 0.0, 0.0]
        self._meter_rms.fill(0)
        self._rms_filter.state.fill(0)

    @staticmethod
    def _empty_statistics():
        return {
            "rmsOneSecond": None,
            "rmsSeconds": 0.0,
            "rollingRmsMax": None,
            "rollingPeakMax": None,
            "aes6Peak": None,
            "aes6Seconds": 0.0,
            "driftRms": None,
            "driftSeconds": 0.0,
            "scrapeRms": None,
            "scrapePeak": None,
            "flutterPeriodHz": None,
            "flutterPeriodSeconds": None,
        }

    def _empty_metrics(self):
        return {
            **empty_meter(),
            **self._empty_statistics(),
            "wow": empty_meter(),
            "flutter": empty_meter(),
            "currentFreq": 0.0,
            "inputLevel": FLOOR_DB,
            "inputAcLevel": FLOOR_DB,
            "timestamp": self.elapsed_seconds,
            "activeTarget": self.active_target,
            "requestedTarget": self.settings["target_frequency"],
            "locked": False,
            "dropouts": self._dropouts,
            "detectorDropouts": self._detector_dropouts,
            "analysisRate": self._detector_rate / ANALYSIS_FRAMES,
            "speedDrift": 0.0,
            "sequence": self._sequence,
            "weighting": self.settings["weighting"],
            "flutterBand": self.settings["flutter_band"],
            "scrapeUnsupported": self._unsupported_band,
            "clockUnsupported": self.clock_unsupported,
        }

    def configure(self, settings=None, **changes):
        updated = measurement_settings(self.settings, {**(settings or {}), **changes})
        modified = {key for key, value in updated.items() if value != self.settings[key]}
        if not modified:
            return
        previous_rate = self.measurement_rate
        self.settings = updated
        if previous_rate != self.measurement_rate:
            self._reset_clock()
            return
        # Editing the saved physical correction while listening to software
        # loopback does not change the active signal or observation clock.
        modified.discard("correction_factor")
        if "show_unsettled_frequency" in modified:
            self._unsettled_frequency = 0.0
            modified.remove("show_unsettled_frequency")
        if not modified:
            return
        if "target_frequency" in modified and self._requested_target() == self.active_target:
            # The interface is accepting an automatic switch already applied
            # by the processor. Do not discard the new filter or partial block.
            self._metrics = {**self._metrics, "requestedTarget": updated["target_frequency"]}
            modified.remove("target_frequency")
            if not modified:
                return
        if modified & {"target_frequency", "flutter_band", "weighting"} or (
            "use_auto_target" in modified and not updated["use_auto_target"]
        ):
            self.active_target = self._requested_target()
            self._auto_target_frames = 0
        self._secondary_cache = None
        if modified & {
            "target_frequency",
            "weighting",
            "use_dc_blocker",
            "use_auto_target",
            "detector_timing",
            "split_frequency",
            "weighting_profile",
            "weighting_profile_name",
            "flutter_band",
        }:
            self.reset_signal()

    def note_gap(self):
        """Discard filter/history state when capture reports missing frames."""
        self._dropouts += 1
        self.reset_signal()

    def reset_signal(self):
        """Begin processing a different source without restarting the clock."""
        self._calibration_history.clear()
        self._reset_history()
        self._reset_filters()
        self._metrics = self._empty_metrics()

    def _demodulate(self, mono):
        if self.clock_unsupported or self._unsupported_band or self.active_target > self.measurement_rate * 0.49:
            return np.zeros((len(mono), 3)), np.zeros(len(mono)), np.zeros(len(mono), dtype=bool)
        step = 2 * pi * self.active_target / self.measurement_rate
        phases = self._oscillator_phase + np.arange(len(mono)) * step
        self._oscillator_phase = (self._oscillator_phase + step * len(mono)) % (2 * pi)
        quadrature = np.column_stack((mono * np.cos(phases), mono * np.sin(phases)))
        quadrature, self._iq_filter_state = signal.sosfilt(
            self._iq_sos, quadrature, axis=0, zi=self._iq_filter_state
        )
        i, q = quadrature.T
        # Use the conjugate local oscillator so a speed increase gives a
        # positive deviation in the scope and on demodulated monitoring.
        phase = np.arctan2(-q, i)
        delta = np.diff(phase, prepend=self._measured_phase)
        self._measured_phase = float(phase[-1])
        delta = (delta + pi) % (2 * pi) - pi
        valid = i * i + q * q >= 1e-6
        preceding_valid = np.concatenate(([self._measured_phase_valid], valid[:-1]))
        self._measured_phase_valid = bool(valid[-1])
        # This is the signed, clock-corrected deviation before weighting or DC
        # removal. Keep it separate from the meter path for archival WAVs.
        raw = delta * (self.measurement_rate / (2 * pi))
        valid &= preceding_valid
        # Discard the discriminator transient while a new IQ filter settles.
        settling = min(self._settling_samples, len(raw))
        valid[:settling] = False
        self._settling_samples -= settling
        raw[~valid] = 0.0
        frequency, wow = self._weight_pass(raw[:, np.newaxis], valid)
        return np.concatenate((frequency, wow, frequency - wow), axis=1), raw, valid

    def _weight_pass(self, frequency, valid):
        seed = self._weighting_needs_seed
        if seed and not np.any(valid):
            return frequency, np.zeros_like(frequency)
        start = np.argmax(valid) if seed else 0
        frequency = frequency[start:]
        if self._weighting_sos is not None:
            if seed:
                self._weighting_state = self._weighting_dc_state * frequency[0, 0]
            weighted, self._weighting_state = signal.sosfilt(
                self._weighting_sos, frequency, axis=0, zi=self._weighting_state,
            )
        else:
            # The published weighting and scrape band-pass already remove
            # steady speed error. Cascading an extra DC blocker with either
            # would alter the specified weighting at low frequencies.
            weighted = frequency
            if self.settings["use_dc_blocker"]:
                if seed:
                    self._dc_filter.seed(weighted[0])
                weighted = self._dc_filter(weighted)
            if self.settings["weighting"] != "unweighted":
                if seed:
                    self._weighting_filter.seed(weighted[0])
                weighted = self._weighting_filter(weighted)
        if seed:
            self._split_filter.seed(weighted[0])
        self._weighting_needs_seed = False
        wow = self._split_filter(weighted)
        if start:
            return np.pad(weighted, ((start, 0), (0, 0))), np.pad(wow, ((start, 0), (0, 0)))
        return weighted, wow

    def _update_detectors(self, percent):
        absolute = np.abs(percent)
        np.maximum(self._block_peak, absolute.max(axis=0), out=self._block_peak)
        self._meter_rms = np.sqrt(np.maximum(self._rms_filter(percent * percent)[-1], 0.0))
        self._one_second.add(percent)
        # An asymmetric peak detector uses the previous output to pick each
        # sample's coefficient. Iterating Python floats avoids NumPy scalar overhead.
        for channel, peak in enumerate(self._meter_peak):
            for value in absolute[:, channel].tolist():
                coefficient = self._peak_attack if value > peak else self._peak_release
                peak += coefficient * (value - peak)
            self._meter_peak[channel] = peak

    def _decimate(self, raw, demodulated, unweighted):
        low, self._low_filter_state = signal.sosfilt(
            self._low_sos, np.column_stack((raw, demodulated, unweighted)), axis=0, zi=self._low_filter_state,
        )
        offset = -self.frames_processed % self._decimation
        reduced = low[offset :: self._decimation]
        self._low_history.append(reduced[:, :2])
        self._diagnostic_pending.append(reduced[:, 2:] * (100 / self.active_target))
        if self._scrape_filter is not None:
            wide, self._scrape_filter_state = signal.sosfilt(
                self._scrape_filter, demodulated, zi=self._scrape_filter_state,
            )
            start = -self.frames_processed % self._scrape_decimation
            self._scrape_pending.append(wide[start :: self._scrape_decimation, np.newaxis])

    def _publish_variation(self, locked, diagnostics):
        # Publish complete blocks only: IQ energy may linger through a drop,
        # and a momentary envelope check cannot detect all clips or bad tones.
        pending = self._scrape_pending if self._scrape else self._diagnostic_pending
        history = self._scope_variation
        if locked:
            if self._scrape:
                frames = pending.latest()
                self._scrape_history.append(frames)
                frames *= 100 / self.active_target
            else:
                frames = diagnostics[:, 1:]
            history.append(frames)
            self._variation_valid = min(history.count, self._variation_valid + len(frames))
        else:
            if self._variation_valid:
                history.count = self._variation_valid = 0
                self._scope_variation_epoch = object()
                self._secondary_cache = None
            history.append(np.full((pending.count, 1), np.nan, dtype=np.float32))
        self._scrape_pending.count = 0

    def _update_statistics(self, locked):
        diagnostics = self._diagnostic_pending.latest() if locked else None
        self._publish_variation(locked, diagnostics)
        self._diagnostic_pending.count = 0
        if not locked:
            # A clipped, unlocked, or interrupted block cannot contribute to
            # a later valid peak. Keep earlier valid rolling maxima intact.
            self._reset_ballistics()
            self._block_peak.fill(0)
            self._valid_history.count = 0
            self._scrape_history.count = 0
            self._period = (None, None)
            self._drift_rms = None
            self._weighting_needs_seed = True

        if locked:
            self._valid_history.append(diagnostics)

        one_second = tuple(float(value) if value is not None else None for value in self._one_second.values())
        rms_max, peak_max = self._rolling.add(
            self.elapsed_seconds, one_second[0], self._meter_peak[0] if locked else None,
        )
        stats = {
            "rmsOneSecond": one_second[0],
            "rmsSeconds": 1.0 if one_second[0] is not None else self._one_second.count / self._detector_rate,
            "rollingRmsMax": rms_max,
            "rollingPeakMax": peak_max,
        }
        if not locked:
            return stats, one_second

        seconds = self._valid_history.count / self._low_rate
        stats["driftSeconds"] = min(DRIFT_WINDOW, seconds)
        if self._aes6:
            stats["aes6Seconds"] = min(AES_WINDOW, seconds)
            window = ceil(AES_WINDOW * self._low_rate)
            if self._valid_history.count >= window:
                weighted, raw = self._valid_history.latest(window).T
                # The standard expresses the deviation as a percentage of
                # average speed over the observation, even if playback is fast
                # or slow. Our discriminator initially scales to the nominal tone.
                speed_ratio = 1 + float(np.mean(raw, dtype=np.float64)) / 100
                if speed_ratio > 0:
                    stats["aes6Peak"] = statistical_peak(weighted) / speed_ratio
        if self._scrape:
            stats["scrapeRms"] = float(self._meter_rms[0])
            stats["scrapePeak"] = float(self._meter_peak[0])

        # Mechanical periodicity and very slow drift need much longer records
        # than the live meters. Refresh their FFTs at one-second intervals.
        if self.frames_processed - self._period_frame >= self._detector_rate:
            self._period_frame = self.frames_processed
            if seconds >= DRIFT_WINDOW:
                raw = self._valid_history.latest()[:, 1]
                self._drift_rms = drift_band_rms(raw, self._low_rate)
            if self._scrape and self._scrape_history.count >= self._scrape_rate:
                # Use percent in both modes. Otherwise a floor meant for a
                # percentage measurement is over 100 times too sensitive.
                scrape = self._scrape_history.latest()[:, 0] * (100 / self.active_target)
                self._period = repeating_frequency(scrape, self._scrape_rate, *SCRAPE_BAND)
            elif not self._scrape and seconds >= 2.0:
                raw = self._valid_history.latest(ceil(30 * self._low_rate))[:, 1]
                self._period = repeating_frequency(raw, self._low_rate, 0.2, 200.0)
        stats.update(driftRms=self._drift_rms, flutterPeriodHz=self._period[0], flutterPeriodSeconds=self._period[1])
        return stats, one_second

    def _observed_frequency(self, raw_block, input_rms):
        adaptive = self.settings["use_adaptive_threshold"]
        raw_freq = frequency_counter(raw_block, self.sample_rate, adaptive)
        if not raw_freq and input_rms > (0.001 if adaptive else 0.002) and np.ptp(raw_block) > 1e-6:
            # A 4096-frame block may span only a fraction of the period of
            # a valid low-frequency custom reference. Avoid scanning the long
            # history when an ordinary-frequency signal failed the lock test.
            zero_crossings = np.count_nonzero(
                np.signbit(raw_block[:-1]) != np.signbit(raw_block[1:])
            )
            if zero_crossings <= 4:
                low_frames = self._low_history.latest(ceil(3.0 * self._low_rate))
                raw_freq = frequency_counter(low_frames[:, 0], self.sample_rate / self._decimation, adaptive)
        return raw_freq

    def _within_reference(self, detected):
        return (not self.clock_unsupported and not self._unsupported_band and detected > 0
                and self.active_target <= self.measurement_rate * 0.49
                and abs(detected - self.active_target) <= 0.1 * self.active_target)

    def _automatic_reference(self, detected):
        if (self.settings["use_auto_target"] and not self._aes6 and self.active_target in (3000, 3150)
                and 2800 < detected < 3350):
            nearest = min((3000.0, 3150.0), key=lambda target: abs(target - detected))
            return nearest if abs(nearest - detected) <= .015 * nearest else 0.0
        return None

    def _finish_block(self):
        raw_block = self._raw_block[:self._block_count]
        input_rms = amplitude_rms(raw_block)
        clipped = bool(np.max(np.abs(raw_block)) >= 0.9995)
        raw_freq = self._observed_frequency(raw_block, input_rms)
        # Store an unusable sample rather than omitting it, so missing data
        # cannot improve the proportion of acceptable calibration readings.
        self._calibration_history.append((self.frames_processed / self.sample_rate, 0.0 if clipped else raw_freq))
        detected = 0.0 if self.clock_unsupported else raw_freq * self.clock_factor
        self._unsettled_frequency = 0.0
        if self.settings["show_unsettled_frequency"] and not raw_freq and not clipped and not self.clock_unsupported:
            # Only the live display sees the estimate. Lock, calibration and
            # saved history continue to use the qualified measurement above.
            self._unsettled_frequency = frequency_counter(
                raw_block, self.sample_rate, self.settings["use_adaptive_threshold"], estimate_unsettled=True,
            ) * self.clock_factor
        input_level = level_db(input_rms)
        # A fixed converter offset can indicate an input fault on the meter,
        # but is not physical signal evidence when looking for the tape end.
        input_ac_level = level_db(np.std(raw_block, dtype=np.float64))
        # Count audible windows with a missed detector result independently of
        # lock transitions. An off-speed tone with a valid reading still counts as detected.
        if input_level > -70 and not detected:
            self._detector_dropouts += 1

        new_target = self.active_target
        candidate = self._automatic_reference(detected)
        reference_ambiguous = candidate == 0.0
        # Confirm a new reference using consecutive clean audio. While it is
        # changing, a valid pitch does not imply a valid speed denominator.
        reference_pending = bool(candidate and candidate != self.active_target and not clipped)
        if reference_pending:
            self._auto_target_frames += self._block_count
            if self._auto_target_frames >= 0.5 * self.measurement_rate:
                new_target = candidate
                self._auto_target_frames = 0
        else:
            self._auto_target_frames = 0

        locked = self._within_reference(detected) and not (reference_ambiguous or reference_pending or clipped)
        if self._metrics["locked"] and not locked:
            self._dropouts += 1
        if not locked:
            # Give the phase discriminator time to settle on a new carrier.
            # Keep this distinct from invalid frames caused by its own warm-up.
            self._settling_samples = self._settling_length
        self._sequence += 1
        statistics, one_second = self._update_statistics(locked and self._invalid_frames == 0)
        window_rms = self.settings["rms_detector"] == "oneSecond"

        def band_meter(index):
            return {
                "peak": self._meter_peak[index],
                "rms": float((one_second[index] or 0.0) if window_rms else self._meter_rms[index]),
                "bufferPeak": float(self._block_peak[index]),
            }

        self._metrics = {
            **self._empty_metrics(),
            **band_meter(0),
            "wow": band_meter(1),
            "flutter": band_meter(2),
            "currentFreq": detected,
            "inputLevel": input_level,
            "inputAcLevel": input_ac_level,
            "activeTarget": new_target,
            "referenceAmbiguous": reference_ambiguous,
            "referencePending": reference_pending,
            "clipped": clipped,
            "invalidFrames": int(self._invalid_frames),
            "locked": locked,
            "speedDrift": 100.0 * (detected - new_target) / new_target if detected else 0.0,
            **statistics,
        }
        self._measurements.append(self._metrics)
        self._complete_speed_block(self._metrics)
        self._block_count = 0
        self._block_peak.fill(0)
        self._invalid_frames = 0
        if new_target != self.active_target:
            self.active_target = new_target
            self._secondary_cache = None
            self._reset_filters()

    def feed(self, samples, sample_rate=None):
        """Accept one or two PCM channels and return the demodulated signal in Hz."""
        if sample_rate is not None:
            rate = self._valid_sample_rate(sample_rate)
            if rate != self.sample_rate:
                self.sample_rate = rate
                self.reset()
        frames = as_frames(samples)
        if not len(frames):
            self.last_unweighted_speed = readonly(np.empty(0, dtype=np.float32))
            self.last_speed_valid = readonly(np.empty(0, dtype=bool))
            return np.empty(0, dtype=np.float32)
        channels = frames.shape[1]
        if self.channels != channels:
            self.channels = channels
            self._reset_history()
            self._reset_filters()
            self._metrics = self._empty_metrics()
        stereo = frames if channels == 2 else np.repeat(frames, 2, axis=1)
        self.input_history.append(stereo)
        self._scope_input.append(frames)
        demodulated = np.empty(len(frames), dtype=np.float32)
        unweighted_speed = np.empty(len(frames), dtype=np.float32)
        valid_speed = np.empty(len(frames), dtype=bool)
        seconds_per_frame = 1 / self._detector_rate
        offset = 0
        while offset < len(frames):
            count = min(ANALYSIS_FRAMES - self._block_count, len(frames) - offset)
            mono = frames[offset : offset + count, 0]
            demodulated_bands, raw_speed, valid = self._demodulate(mono)
            demodulated[offset : offset + count] = demodulated_bands[:, 0]
            unweighted_speed[offset : offset + count] = raw_speed * (100 / self.active_target)
            valid_speed[offset : offset + count] = valid
            if (capture := self._speed_capture) is not None:
                segment = slice(self._block_count, self._block_count + count)
                capture.values[segment] = unweighted_speed[offset : offset + count]
                capture.valid[segment] = valid
            self._invalid_frames += count - np.count_nonzero(valid)
            self._raw_block[self._block_count : self._block_count + count] = mono
            self._update_detectors(demodulated_bands * (100 / self.active_target))
            self._decimate(stereo[offset : offset + count], demodulated_bands[:, 0], raw_speed)
            self.frames_processed += count
            self.elapsed_seconds += count * seconds_per_frame
            self._block_count += count
            offset += count
            if self._block_count == ANALYSIS_FRAMES:
                self._finish_block()

        self.last_unweighted_speed = readonly(unweighted_speed)
        self.last_speed_valid = readonly(valid_speed)
        return demodulated

    def wow_flutter_summary(self):
        """Finish the last partial block when analysing an offline source."""
        if self._block_count:
            self._finish_block()
        return self.metrics()

    def _stereo_tone(self, spectra, reference, *, search_hz=0, samples=None, clipped=False, nominal_clock=False):
        if reference < 6 * spectra[0].bin_width:
            # Resolve low tones independently of the full-band FFT bin width.
            low_audio = self._low_history.latest()[:, :2]
            low_rate = self.sample_rate / self._decimation if nominal_clock else self._low_rate
            spectra = (
                power_spectrum(low_audio[:, 0], low_rate),
                power_spectrum(low_audio[:, 1], low_rate),
            )
            if samples is not None:
                samples = low_audio
            search_hz = min(search_hz, reference / 10)
        tone_frequency = reference
        if search_hz and self.channels == 2:
            # A slightly off-speed record can move the test tone by several
            # bins. Find the dominant tone on either channel, then measure
            # power at that same frequency on both channels for valid ratios.
            low = max(spectra[0].bin_width, reference - search_hz)
            high = reference + search_hz
            peaks = (spectrum.peak(low, high) for spectrum in spectra)
            frequency, _ = max(peaks, key=lambda peak: peak[1])
            tone_frequency = frequency or reference
        levels = fitted_stereo_levels(samples, tone_frequency, spectra[0].bin_width) if samples is not None else None
        raw_left, raw_right = levels or tuple(spectrum.tone_db(tone_frequency) for spectrum in spectra)
        reference_present = min(raw_left, raw_right) >= -80 and all(
            10 ** (level / 10) >= 0.5 * spectrum.power.sum()
            for level, spectrum in zip((raw_left, raw_right), spectra, strict=True)
        )
        trim_left, trim_right = self.settings["channel_calibration"]
        left = raw_left + trim_left
        right = raw_right + trim_right
        difference = left - right
        return {
            "available": self.channels >= 2 and max(raw_left, raw_right) > -95 and not clipped,
            "referencePresent": bool(reference_present) and not clipped,
            "clipped": clipped,
            "targetFrequency": reference,
            "toneFrequency": tone_frequency,
            "leftDb": left,
            "rightDb": right,
            "rawLeftDb": raw_left,
            "rawRightDb": raw_right,
            "balanceDb": difference,
            "trims": {"leftTrimDb": trim_left, "rightTrimDb": trim_right},
            "crosstalkDb": -abs(difference),
            "leftToRightDb": -difference,
            "rightToLeftDb": difference,
        }

    def _secondary(self):
        rate = self._detector_rate
        if (
            self._secondary_cache is not None
            and self.frames_processed - self._secondary_frame < 0.085 * rate
        ):
            return self._secondary_cache
        latest = self.input_history.latest()
        left_spectrum = power_spectrum(latest[:, 0], rate)
        right_spectrum = (
            power_spectrum(latest[:, 1], rate) if self.channels == 2 else left_spectrum
        )
        self._spectra = left_spectrum, right_spectrum
        low = self._low_history.latest()
        resonance = power_spectrum(low[:, 0], self._low_rate, remove_dc=True)
        if self._scrape:
            flutter_rate, flutter_seconds = self._scrape_rate, 2
            flutter_low, flutter_high = SCRAPE_BAND
        else:
            flutter_rate, flutter_seconds = self._low_rate, LOW_FREQUENCY_SECONDS
            flutter_low, flutter_high = 0, 200
        count = min(self._variation_valid, ceil(flutter_seconds * flutter_rate))
        # Shorter blocks give unreliable bins at rotational-wow frequencies.
        # Use contiguous, validated deviation in Hz for both displays.
        usable = count if count >= ceil(MIN_FLUTTER_SPECTRUM_SECONDS * flutter_rate) else 0
        variation_hz = self._scope_variation.latest(usable)[:, 0] * (self.active_target / 100)
        flutter = power_spectrum(variation_hz, flutter_rate)
        flutter_view = flutter.snapshot(low=flutter_low, high=flutter_high)
        resonance_frequency, resonance_level = resonance.resonant_peak(2, 30)
        mono = latest[:, 0]
        left_thd = distortion(mono, left_spectrum, rate)
        right_thd = (distortion(latest[:, 1], right_spectrum, rate)
                     if self.channels == 2 else dict(left_thd))
        spectrum = left_spectrum.snapshot()
        stereo_reference = self.settings["stereo_tone_frequency"]
        # Match the useful search span of two legacy 2048-point FFT bins
        # without sacrificing the narrower, high-resolution power estimate.
        search_hz = 2 * rate / 2048 if stereo_reference is None else 0
        self._secondary_cache = {
            "spectrum": spectrum,
            "stereo_spectrum": {"left": spectrum, "right": right_spectrum.snapshot()},
            "flutter_spectrum": flutter_view,
            "resonance_spectrum": resonance.snapshot(high=30),
            "thd": left_thd,
            "tracking": {"left": left_thd, "right": right_thd},
            "stereo": self._stereo_tone(
                self._spectra, stereo_reference or self.active_target, search_hz=search_hz,
                samples=latest if self.channels == 2 and search_hz else None,
                clipped=bool(np.any(np.abs(latest) >= .9995)),
            ),
            "noise": {
                # AC RMS excludes the converter's static offset from a noise-groove reading.
                "rmsDb": level_db(np.std(mono, dtype=np.float64)) if len(mono) else FLOOR_DB,
                "rumbleDb": power_db(resonance.band_power(2, 30)),
            },
            "resonance": {"frequency": resonance_frequency, "levelDb": resonance_level},
        }
        self._secondary_frame = self.frames_processed
        return self._secondary_cache

    def metrics(self):
        return deepcopy(self._metrics)

    @property
    def sequence(self):
        return self._sequence

    def drain_measurements(self):
        readings = list(self._measurements)
        self._measurements.clear()
        return readings

    def scope_frames(self, seconds, *, modulation=False, after=None):
        """Return contiguous data at a sample-clock index, with an epoch for gaps."""
        if modulation:
            decimation = self._scrape_decimation if self._scrape else self._decimation
            history, epoch = self._scope_variation, self._scope_variation_epoch
            # Recent unclassified samples have not entered the modulation ring.
            end = ceil((self.frames_processed - self._block_count) / decimation)
        else:
            decimation = 1
            history, epoch = self._scope_input, self._scope_input_epoch
            end = self.frames_processed
        # Report the measured sample clock when a correction is known. The
        # capture still counts the same physical samples either way.
        rate = self._detector_rate / decimation
        count = min(history.count, ceil(max(0.0, seconds) * rate))
        if after is not None:
            count = min(history.count, count + max(0, end - after))
        return ScopeBuffer(rate, history.latest(count), end, epoch)

    def snapshot(self):
        scope = self.input_history.latest(ANALYSIS_FRAMES)
        left, right = scope.T
        # Preserve frequency content in the demodulated scope for either band.
        # The longer timebase makes low-frequency wow easier to inspect.
        modulation_rate = self._scrape_rate if self._scrape else self._low_rate
        modulation = self._scope_variation.latest(ceil(2 * modulation_rate))[:, 0]
        rms = level_db(amplitude_rms(left))
        peak = level_db(float(np.max(np.abs(left))) if len(left) else 0.0)
        correlation = 0.0
        if len(left):
            # DC offsets move the XY figure without changing the phase
            # relationship. Correlate deviations from each channel's centre.
            left_ac = left - np.mean(left, dtype=np.float64)
            right_ac = right - np.mean(right, dtype=np.float64)
            denominator = np.linalg.norm(left_ac) * np.linalg.norm(right_ac)
            if denominator:
                correlation = float(np.clip(np.dot(left_ac, right_ac) / denominator, -1, 1))
        status = "Signal present"
        right_rms = level_db(amplitude_rms(right))
        right_peak = level_db(float(np.max(np.abs(right))) if len(right) else 0.0)
        if not len(left) or max(rms, right_rms) < -60:
            status = "No signal"
        elif max(peak, right_peak) > -1:
            status = "Near clipping"
        return {
            "metrics": self.metrics(),
            "unsettled_frequency": self._unsettled_frequency,
            "scope": {
                "left": readonly(left),
                "right": readonly(right),
                "modulation": readonly(modulation),
                "modulationSampleRate": modulation_rate,
                "rmsDb": rms,
                "peakDb": peak,
                "rightRmsDb": right_rms,
                "rightPeakDb": right_peak,
                "correlation": correlation,
                "status": status,
            },
            **self._secondary(),
            "sample_rate": self.sample_rate,
        }

    def clock_readings(self, after=0.0):
        return [frequency for timestamp, frequency in self._calibration_history if timestamp > after]

    def channel_readings(self, reference):
        clipped = self._secondary()["stereo"]["clipped"]
        readings = self._stereo_tone(self._spectra, reference, clipped=clipped)
        ratio = self.sample_rate / self._detector_rate
        if readings["referencePresent"] or clipped or ratio == 1:
            return readings
        # An incorrect saved clock correction should not prevent independent
        # channel trim calibration if the tone is found on the nominal axis.
        spectra = tuple(replace(spectrum, frequencies=spectrum.frequencies * ratio) for spectrum in self._spectra)
        return self._stereo_tone(spectra, reference, nominal_clock=True)
