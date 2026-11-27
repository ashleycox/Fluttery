"""Audio device streaming, generation and the public measurement service."""

from dataclasses import asdict, dataclass
from math import ceil
from threading import Condition, Event, Lock, RLock, Thread
from time import monotonic

import numpy as np

from .generator import GeneratorSettings, MonitorResampler, SignalGenerator, monitor_route
from .processor import (
    MAX_MEASUREMENT_RATE,
    MIN_MEASUREMENT_RATE,
    MeasurementProcessor,
    SampleHistory,
    as_frames,
)
from .settings import bounded
from .sweep import SweepRenderer, SweepSettings

OUTPUT_BUFFER_SECONDS = 0.040
DEVICE_CHECK_SECONDS = 0.5
CALLBACK_TIMEOUT_SECONDS = 2.0
SWEEP_STOP_SECONDS = 0.005
# The level meters can observe five seconds after one second of real filter
# history. Display analysis never uses the preceding samples as observations.
LIVE_HISTORY_SECONDS = 6


class FrameQueue:
    """Bounded FIFO between the capture/playback callbacks and the worker."""

    def __init__(self, capacity, channels):
        self.data = np.empty((capacity, channels), dtype=np.float32)
        self._read_at = 0
        self._write_at = 0
        self.count = 0
        self.frames_written = 0
        self.frames_read = 0
        self.lock = Lock()

    def _segments(self, offset, count):
        end = min(offset + count, len(self.data))
        return self.data[offset:end], self.data[: offset + count - end]

    def write(self, source, *, wait=True):
        if not self.lock.acquire(blocking=wait):
            return 0
        try:
            count = min(len(source), len(self.data) - self.count)
            first, second = self._segments(self._write_at, count)
            first[:] = source[: len(first)]
            second[:] = source[len(first) : count]
            self._write_at = (self._write_at + count) % len(self.data)
            self.count += count
            self.frames_written += count
            return count
        finally:
            self.lock.release()

    def read_into(self, target, *, wait=True):
        if not self.lock.acquire(blocking=wait):
            return 0
        try:
            count = min(len(target), self.count)
            first, second = self._segments(self._read_at, count)
            target[: len(first)] = first
            target[len(first) : count] = second
            self._read_at = (self._read_at + count) % len(self.data)
            self.count -= count
            self.frames_read += count
            return count
        finally:
            self.lock.release()

    def clear(self):
        with self.lock:
            self._read_at = self._write_at
            self.count = 0
            self.frames_read = self.frames_written

    def mute(self):
        """Silence pending samples without underfeeding the playback callback."""
        with self.lock:
            for segment in self._segments(self._read_at, self.count):
                segment.fill(0)


class _SweepRoute:
    """Render a finite sweep on the input or output sample clock."""

    def __init__(self, settings, rate):
        self.renderer = SweepRenderer(settings, rate)
        self.rate = rate
        self.frame_limit = self.renderer.total_frames
        self.fade_start = None

    @property
    def position(self):
        return self.renderer.position

    @property
    def exhausted(self):
        return self.position >= self.frame_limit

    def stop(self):
        self.fade_start = self.position
        # A stop before the first block should never start an audible signal.
        fade = round(self.rate * SWEEP_STOP_SECONDS) if self.position else 0
        self.frame_limit = min(self.frame_limit, self.position + fade)

    def render(self, count, channels):
        start = self.position
        frames = min(count, self.frame_limit - start)
        signal = self.renderer.render(frames)
        if self.fade_start is not None and frames:
            fade_length = self.frame_limit - self.fade_start
            fade = 1 - (start - self.fade_start + np.arange(1, frames + 1)) / fade_length
            signal *= fade
        play = np.zeros((count, channels), dtype=np.float32)
        play[:frames] = signal[:, None]
        return play


@dataclass(slots=True)
class _SweepRun:
    identifier: int
    settings: SweepSettings
    output: _SweepRoute | None
    loopback: _SweepRoute | None
    output_latency: float = 0.0
    status: str = "playing"
    elapsed: float = 0.0
    output_start: int | None = None
    output_drained_at: float | None = None

    @property
    def active(self):
        return self.status in ("playing", "stopping")

    def stop(self):
        if self.status != "playing":
            return False
        self.status = "stopping"
        for route in (self.output, self.loopback):
            if route is not None:
                route.stop()
        return True

    def advance(self, output_queue, now):
        if not self.active:
            return
        # Output rendering runs ahead of playback. Wait for the final signal
        # frame to pass through the callback, then allow for device latency.
        output = self.output
        played = 0
        output_done = output is None
        if output is not None:
            if self.output_start is not None:
                played = max(0, min(output_queue.frames_read - self.output_start, output.position))
            if output.exhausted and played == output.position:
                if self.output_drained_at is None:
                    self.output_drained_at = now
                output_done = not played or now >= self.output_drained_at + self.output_latency
        elapsed = (played / output.rate if output is not None else
                   self.loopback.position / self.loopback.rate if self.loopback is not None else 0.0)
        self.elapsed = min(self.settings.duration, max(self.elapsed, elapsed))
        if output_done and (self.loopback is None or self.loopback.exhausted):
            self.status = "stopped" if self.status == "stopping" else "completed"
            if self.status == "completed":
                self.elapsed = self.settings.duration


class AudioEngine:
    """Own device streams and run signal processing away from the GUI thread."""

    def __init__(self, settings=None):
        self.processor = MeasurementProcessor(settings)
        self._state = Condition(RLock())
        self._lifecycle = RLock()
        self._generator = GeneratorSettings()
        self._sweep = None
        self._sweep_identifier = 0
        self._monitor_enabled = False
        self._interface_loopback_interlock = False
        self._monitor_level = 0.25
        self._monitor_mode = "stereo"
        self._loopback = False
        self._software_generator = SignalGenerator()
        self._device_generator = SignalGenerator()
        self._generation = 0
        self._pcm_history = None
        self._session_audio = None
        self._pcm_rate = None
        self._pcm_sequence = 0
        self._bench_capture = None
        self._bench_underflows = 0
        self._raw_speed_capture = None
        self._input_stream = None
        self._output_stream = None
        self._input_queue = None
        self._output_queue = None
        self._halt = Event()
        self._wake = Event()
        self._stop_input = Event()
        self._worker = None
        self._start_signature = None
        self._input_overflows = 0
        self._output_underflows = 0
        self._discarded_frames = 0
        self._error = ""
        self._input_warning = ""
        self._output_warning = ""
        self._portaudio_error = OSError
        self._last_capture_at = 0.0
        self._last_playback_at = 0.0
        self._clear_cadence()

    @staticmethod
    def _backend():
        try:
            import sounddevice
        except (ImportError, OSError) as error:
            raise RuntimeError(f"Audio support is unavailable. {error}") from error
        return sounddevice

    @property
    def running(self):
        return self._worker is not None and not self._halt.is_set()

    @property
    def input_enabled(self):
        return self.running and self._input_stream is not None

    @property
    def output_enabled(self):
        return self.running and self._output_stream is not None

    @property
    def has_device_streams(self):
        # A faulted worker can leave aborted streams for the lifecycle owner
        # to close. Keep their hardware reservation until that cleanup runs.
        with self._state:
            return self._input_stream is not None or self._output_stream is not None

    @property
    def input_device_index(self):
        return getattr(self._input_stream, 'device', None)

    @property
    def output_device_index(self):
        return getattr(self._output_stream, 'device', None)

    def devices(self):
        with self._lifecycle:
            backend = self._backend()
            try:
                host_apis = backend.query_hostapis()
                devices = backend.query_devices()
            except backend.PortAudioError as error:
                raise RuntimeError(f"Could not list audio devices. {error}") from error
            return [
                {
                    "id": index,
                    "name": device["name"],
                    "host_api": host_apis[device["hostapi"]]["name"],
                    "input_channels": int(device["max_input_channels"]),
                    "output_channels": int(device["max_output_channels"]),
                    "sample_rate": float(device["default_samplerate"]),
                }
                for index, device in enumerate(devices)
            ]

    def check_route(self, direction, device, rate, channels):
        """Validate a historical audio route without opening or playing a stream."""
        with self._lifecycle:
            backend = self._backend()
            try:
                info = backend.query_devices(device, direction)
                count = channels if direction == 'input' else min(2, info['max_output_channels'])
                check = backend.check_input_settings if direction == 'input' else backend.check_output_settings
                check(device=device, channels=count, samplerate=rate or info['default_samplerate'], dtype='float32')
            except backend.PortAudioError as error:
                raise RuntimeError(f'The {direction} device does not support the recorded sample rate or channel count. {error}') from error

    def configure(self, settings=None, **changes):
        with self._state:
            rate = self.processor.measurement_rate
            self.processor.configure(settings, **changes)
            if self.processor.measurement_rate != rate:
                self._generation += 1
                self._state.notify_all()

    def reset(self):
        with self._state:
            self.processor.reset()
            self._pcm_history = None
            self._clear_cadence()
            self._generation += 1
            self._state.notify_all()

    def set_generator(self, enabled=None, **changes):
        with self._state:
            if enabled and (self._interface_loopback_interlock
                            or self._bench_capture is not None and self._bench_capture.active):
                return
            if enabled and self._sweep is not None:
                self._sweep.stop()
            self._generator = self._generator.updated(enabled, **changes)
        self._wake.set()

    def start_sweep(self, settings):
        """Play a sweep on the existing output and optionally on internal loopback."""
        with self._lifecycle, self._state:
            if self._interface_loopback_interlock:
                raise RuntimeError("Disconnect the interface loopback cable before playing a sweep.")
            if self._bench_capture is not None and self._bench_capture.active:
                raise RuntimeError("Finish the response capture before playing a sweep.")
            if self._sweep is not None and self._sweep.active:
                raise RuntimeError("Stop the current sweep before starting another.")
            if not self.running or not (self.output_enabled or self._loopback):
                raise RuntimeError("Start audio output or enable internal loopback before playing a sweep.")
            stream = self._output_stream
            # Each route is rendered at its negotiated rate. Validate both
            # before changing the transport so errors leave the tone intact.
            output = _SweepRoute(settings, stream.samplerate) if stream is not None else None
            loopback = _SweepRoute(settings, self.processor.sample_rate) if self._loopback else None
            latency = bounded(getattr(stream, "latency", None), 0, CALLBACK_TIMEOUT_SECONDS, 0)
            self._sweep_identifier += 1
            identifier = self._sweep_identifier
            self._sweep = _SweepRun(identifier, settings, output, loopback, latency)
            # Keep the fixed-tone renderers so their current level can fall
            # to zero before the new sweep starts on each destination.
            self._generator = self._generator.updated(False)
        self._wake.set()
        return identifier

    def reset_measurement_statistics(self):
        with self._state:
            self.processor.reset_statistics()

    def _keep_pcm(self, samples, rate):
        if self._pcm_history is None or self._pcm_rate != rate or self._pcm_history.data.shape[1] != samples.shape[1]:
            self._pcm_history = SampleHistory(ceil(rate * LIVE_HISTORY_SECONDS), samples.shape[1])
            self._pcm_epoch = object()
            self._pcm_rate = rate
        self._pcm_history.append(samples)
        self._pcm_sequence += len(samples)
        if self._session_audio is not None:
            self._session_audio.append(samples, rate)

    def start_session_audio(self, capture):
        with self._state:
            if self._session_audio is not None:
                raise RuntimeError('A session audio capture is already active.')
            self._session_audio = capture

    def finish_session_audio(self, capture):
        with self._state:
            if self._session_audio is capture:
                self._session_audio = None
        return capture.finish()

    def recent_pcm(self, seconds=1):
        """Copy recent device PCM. Multiple tools can read it independently."""
        return self.input_observation(seconds)[:3]

    def input_observation(self, seconds):
        """Return device PCM, its exclusive frame position and continuity token."""
        with self._state:
            rate = self._pcm_rate or self.processor.sample_rate
            if self._pcm_history is None:
                empty = np.empty((0, self.processor.channels), dtype=np.float32)
                return rate, empty, self._pcm_sequence, None
            count = ceil(max(0, min(float(seconds), LIVE_HISTORY_SECONDS)) * rate)
            return rate, self._pcm_history.latest(count), self._pcm_sequence, self._pcm_epoch

    def scope_frames(self, seconds, *, modulation=False, after=None):
        """Read the effective measurement source, including internal loopback."""
        with self._state:
            return self.processor.scope_frames(seconds, modulation=modulation, after=after)

    def start_bench_capture(self, duration, stimulus=None, *, interface_loopback=False):
        from .response import BenchCapture

        # Generate the reference outside the audio worker lock. Confirm the
        # streams still match afterward before arming the capture.
        with self._state:
            if self._interface_loopback_interlock != interface_loopback:
                raise ValueError('Prepare the interface loopback before calibrating.' if interface_loopback
                                 else 'Disconnect the interface loopback cable before running another test.')
            source, destination = self._input_stream, self._output_stream
            if source is None or (stimulus is not None and destination is None):
                raise ValueError('Start the selected physical audio devices before measuring.')
            if self._bench_capture is not None and self._bench_capture.active:
                raise ValueError('An audio response capture is already in progress.')
            rate = source.samplerate
            output_rate = destination.samplerate if stimulus is not None else None
            channels = self.processor.channels
        reference = stimulus.render(output_rate) if stimulus is not None else None
        capture = BenchCapture(rate, output_rate, duration, reference, channels=channels)
        with self._state:
            if self._interface_loopback_interlock != interface_loopback:
                raise ValueError('The interface loopback state changed before capture could start.')
            if source is not self._input_stream or destination is not self._output_stream:
                raise ValueError('The audio route changed before capture could start.')
            if self._bench_capture is not None and self._bench_capture.active:
                raise ValueError('An audio response capture is already in progress.')
            if self._generator.enabled or (self._sweep is not None and self._sweep.active):
                raise ValueError('Stop the generator or sweep before starting a response capture.')
            self._bench_capture = capture
            if self._output_queue is not None:
                self._output_queue.mute()
            self._bench_underflows = self._output_underflows
        self._wake.set()
        return capture

    def finish_bench_capture(self, capture, *, cancel=False):
        with self._state:
            if self._bench_capture is capture:
                self._bench_capture = None
                if (cancel or capture.error) and self._output_queue is not None:
                    self._output_queue.mute()
            if cancel and capture.active:
                capture.cancel()
        self._wake.set()

    def begin_raw_speed_capture(self, target, *, tape_end=None):
        from .recordings import RawSpeedCapture

        with self._state:
            if self._raw_speed_capture is not None:
                raise ValueError('A speed waveform is already being saved.')
            if not self.running:
                raise ValueError('Start live input before recording a speed waveform.')
            software = self._using_internal_source() or (self._loopback and not self.input_enabled)
            rate = self.processor.sample_rate
            if not software:
                rate *= self.processor.settings['correction_factor']
            if not MIN_MEASUREMENT_RATE <= rate <= MAX_MEASUREMENT_RATE:
                raise ValueError('The corrected sample rate is outside the supported measurement range.')
            capture = RawSpeedCapture(target, rate, tape_end)
            if self.processor.use_software_clock(software):
                self._generation += 1
                self._state.notify_all()
            self.processor.begin_speed_capture()
            self._raw_speed_capture = capture
            return capture

    def finish_raw_speed_capture(self, capture, *, cancel=False):
        with self._state:
            if self._raw_speed_capture is capture:
                self._raw_speed_capture = None
                if self.processor.speed_capture_interrupted and not cancel:
                    capture.finish(error='The audio reference or route changed during the speed recording.')
                for values, valid, _ in self.processor.end_speed_capture(keep_tail=not (cancel or capture.error)):
                    if cancel:
                        break
                    capture.append(values, valid)
        capture.finish(cancel=cancel)

    def _publish_raw_speed(self):
        capture = self._raw_speed_capture
        if capture is None:
            return
        if self.processor.speed_capture_interrupted:
            capture.finish(error='The audio reference or route changed during the speed recording.')
            self._raw_speed_capture = None
            self.processor.end_speed_capture()
            return
        for values, valid, reading in self.processor.drain_speed_capture():
            capture.append(values, valid)
            if capture.observe_end(reading):
                self._raw_speed_capture = None
                self.processor.end_speed_capture()
                break

    def _interrupt_captures(self, message, *, speed=True):
        if self._session_audio is not None:
            self._session_audio.mark_gap(message)
        if self._bench_capture is not None:
            self._bench_capture.cancel(message)
            self._bench_capture = None
            if self._output_queue is not None:
                self._output_queue.mute()
        if speed and self._raw_speed_capture is not None:
            self._raw_speed_capture.finish(error=message)
            self._raw_speed_capture = None
            self.processor.end_speed_capture()

    def stop_sweep(self):
        """Request a short fade and drain any signal already queued for playback."""
        with self._state:
            changed = self._sweep is not None and self._sweep.stop()
        self._wake.set()
        return changed

    def set_monitor(self, enabled, level=None, mode=None):
        with self._state:
            self._monitor_enabled = bool(enabled)
            self._monitor_level = bounded(level, 0, 1, self._monitor_level)
            if mode in ("stereo", "left", "right", "mono", "mono_sum", "difference", "demodulated"):
                self._monitor_mode = mode
        self._wake.set()

    def set_interface_loopback_interlock(self, engaged):
        """Mute ordinary output until the operator confirms physical disconnection."""
        with self._state:
            engaged = bool(engaged)
            if self._interface_loopback_interlock == engaged:
                return
            self._interface_loopback_interlock = engaged
            if self._bench_capture is not None:
                self._bench_capture.cancel('The interface loopback interlock changed during the measurement.')
                self._bench_capture = None
            if self._output_queue is not None:
                # Remove monitoring audio already buffered for the callback.
                # The hardware buffer drains before the operator connects.
                self._output_queue.clear()
        self._wake.set()

    def set_loopback(self, enabled):
        with self._state:
            if self._loopback == bool(enabled):
                return
            self._loopback = bool(enabled)
            if not self._loopback:
                self._software_generator = SignalGenerator()
                if self._sweep is not None and self._sweep.active:
                    self._sweep.loopback = None
                    if self._sweep.output is None:
                        self._sweep.status = "stopped"
            if not self.running:
                self.processor.reset_signal()
            self._generation += 1
            self._state.notify_all()
        self._wake.set()

    def feed(self, samples, sample_rate=None):
        """Submit PCM directly for offline use and return the resulting snapshot."""
        with self._state:
            previous_rate = self.processor.sample_rate
            frames = as_frames(samples)
            clock_changed = self.processor.use_software_clock(False)
            self.processor.feed(frames, sample_rate)
            self._publish_raw_speed()
            if len(frames):
                self._keep_pcm(frames, self.processor.sample_rate)
            if clock_changed or self.processor.sample_rate != previous_rate:
                self._generation += 1
            self._state.notify_all()
        return self.snapshot()

    def render_generator(self, frames, sample_rate=None):
        """Render current generator settings without opening an output device."""
        with self._state:
            rate = (
                self.processor.sample_rate
                if sample_rate is None
                else self.processor._valid_sample_rate(sample_rate)
            )
            return self._software_generator.render(frames, rate, self._generator)

    def _capture(self, samples, _count, _time, status):
        self._last_capture_at = monotonic()
        if status.input_overflow:
            self._input_overflows += 1
        copied = self._input_queue.write(samples, wait=False)
        self._discarded_frames += len(samples) - copied

    def _playback(self, target, _count, _time, status):
        self._last_playback_at = monotonic()
        copied = self._output_queue.read_into(target, wait=False)
        target[copied:].fill(0)
        if status.output_underflow or copied < len(target):
            self._output_underflows += 1

    def _open_capture(self, backend, device, rate, channels):
        info = backend.query_devices(device, "input")
        if channels > info["max_input_channels"]:
            raise RuntimeError(f"{info['name']} cannot provide {channels} input channels.")
        rate = rate or info["default_samplerate"]
        self._input_queue = FrameQueue(ceil(rate * 0.35), channels)
        return backend.InputStream(
            device=device,
            channels=channels,
            samplerate=rate,
            dtype="float32",
            callback=self._capture,
        )

    def _open_playback(self, backend, device, rate, *, allow_default_rate=False):
        info = backend.query_devices(device, "output")
        channels = min(2, info["max_output_channels"])
        if channels == 0:
            raise RuntimeError(f"{info['name']} has no output channels.")
        rate = rate or info["default_samplerate"]
        try:
            backend.check_output_settings(
                device=device, channels=channels, samplerate=rate, dtype="float32"
            )
        except backend.PortAudioError:
            if not allow_default_rate or rate == info["default_samplerate"]:
                raise
            rate = info["default_samplerate"]
        self._output_queue = FrameQueue(ceil(rate * 0.10), channels)
        stream = backend.OutputStream(
            device=device,
            channels=channels,
            samplerate=rate,
            dtype="float32",
            callback=self._playback,
        )
        self._output_queue.write(
            np.zeros((ceil(stream.samplerate * OUTPUT_BUFFER_SECONDS), channels), dtype=np.float32)
        )
        return stream

    def start(
        self,
        input_device=None,
        output_device=None,
        sample_rate=None,
        channels=None,
        *,
        input_enabled=True,
        output_enabled=True,
    ):
        """Start or restart the requested route and report whether it changed."""
        with self._lifecycle:
            with self._state:
                channels = (
                    2
                    if channels in (2, "2")
                    else (self.processor.settings["channels"] if channels is None else 1)
                )
            signature = input_device, output_device, sample_rate, channels, input_enabled, output_enabled
            if (
                self.running
                and self._start_signature == signature
                and self.input_enabled == input_enabled
                and self.output_enabled == output_enabled
            ):
                return False
            self.stop()
            self._error = ""
            self._input_warning = ""
            self._output_warning = ""
            self._halt.clear()
            self._wake.clear()
            self._input_overflows = 0
            self._output_underflows = 0
            self._discarded_frames = 0
            try:
                backend = self._backend() if input_enabled or output_enabled else None
                self._portaudio_error = getattr(backend, "PortAudioError", OSError)
                if input_enabled:
                    self._input_stream = self._open_capture(backend, input_device, sample_rate, channels)
                input_rate = self._input_stream.samplerate if self._input_stream is not None else None
                if output_enabled:
                    try:
                        self._output_stream = self._open_playback(
                            backend, output_device, sample_rate or input_rate, allow_default_rate=sample_rate is None,
                        )
                    except Exception as error:
                        if not input_enabled or output_device is not None:
                            raise
                        self._output_warning = f"No default audio output is available. {error}"
                        self._output_queue = None
                output_rate = self._output_stream.samplerate if self._output_stream is not None else None
                processing_rate = input_rate or output_rate or sample_rate or self.processor.sample_rate
                with self._state:
                    self.processor.configure(channels=channels)
                    self.processor.sample_rate = self.processor._valid_sample_rate(processing_rate)
                    self.processor.reset()
                    self._clear_cadence()
                    self._generation += 1
                    self._software_generator = SignalGenerator()
                    self._device_generator = SignalGenerator()
                    self._state.notify_all()
                self._last_capture_at = self._last_playback_at = monotonic()
                if self._input_stream is not None:
                    self._input_stream.start()
                if self._output_stream is not None:
                    self._output_stream.start()
                self._start_signature = signature
                self._worker = Thread(
                    target=self._work,
                    args=(input_rate, output_rate, channels),
                    name="Fluttery audio",
                    daemon=True,
                )
                self._worker.start()
                return True
            except Exception as error:
                self.stop()
                self._error = f"Could not start audio. {error}"
                raise RuntimeError(self._error) from error

    def stop(self):
        with self._lifecycle:
            self._halt.set()
            self._wake.set()
            if self._worker is not None:
                # A thread that failed to start has no joinable handle.
                if self._worker.ident is not None:
                    self._worker.join()
                self._worker = None
            for stream in (self._input_stream, self._output_stream):
                if error := self._close_stream(stream):
                    self._error = error
            self._input_stream = self._output_stream = None
            self._input_queue = self._output_queue = None
            self._stop_input.clear()
            self._start_signature = None
            with self._state:
                if self._sweep is not None and self._sweep.active:
                    self._sweep.status = "interrupted"
                self._interrupt_captures('The audio route stopped during capture.')
                self._pcm_history = None
                self.processor.reset_signal()
                self._generation += 1
                self._state.notify_all()

    def stop_capture(self):
        """Detach physical input without reopening a working playback route."""
        with self._lifecycle:
            if not self.input_enabled:
                return False
            with self._state:
                keep_running = self.output_enabled or self._loopback
            if not keep_running:
                self.stop()
                return True
            with self._state:
                # The processing worker owns the device reference and its
                # local monitor queue. Let it detach at a block boundary.
                self._stop_input.set()
                self._wake.set()
                self._state.wait_for(lambda: self._input_stream is None or not self.running,
                                     timeout=CALLBACK_TIMEOUT_SECONDS)
                if self._input_stream is not None:
                    raise RuntimeError(self._error or "The input device has not finished closing.")
            return True

    def close(self):
        self.stop()

    def _close_stream(self, stream):
        if stream is not None:
            try:
                try:
                    stream.abort()
                finally:
                    stream.close()
            except (OSError, RuntimeError, self._portaudio_error) as error:
                return f"Could not close an audio device. {error}"
        return ""

    def _device_responds(self, stream, last_callback_at, now):
        try:
            return stream.active and now - last_callback_at <= CALLBACK_TIMEOUT_SECONDS
        except (OSError, RuntimeError, self._portaudio_error):
            return False

    def _using_internal_source(self):
        sweep = self._sweep
        sweep_signal = sweep is not None and sweep.active and sweep.loopback is not None and not sweep.loopback.exhausted
        return self._loopback and (sweep_signal or self._generator.enabled or self._software_generator.amplitude > 0)

    def _check_streams(self, now, input_rate, output_rate):
        """Keep a healthy stream running when the other device disappears."""
        requested = self._stop_input.is_set()
        if self._input_stream is not None and (
            requested or not self._device_responds(self._input_stream, self._last_capture_at, now)
        ):
            close_error = self._close_stream(self._input_stream)
            input_rate = None
            with self._state:
                self._input_stream = self._input_queue = None
                if requested:
                    self._input_warning = close_error
                    reason = "Live input was stopped during capture."
                else:
                    self._input_warning = "The input device was disconnected or stopped responding."
                    if close_error:
                        self._input_warning += f" {close_error}"
                    reason = self._input_warning
                self._pcm_history = None
                software = self.processor.uses_software_clock
                self._interrupt_captures(reason, speed=not software)
                if requested and self._start_signature is not None:
                    input_device, output_device, rate, channels, _, output_enabled = self._start_signature
                    self._start_signature = input_device, output_device, rate, channels, False, output_enabled
                else:
                    self._start_signature = None
                if not software:
                    self.processor.note_gap()
                self._generation += 1
                self._state.notify_all()
        if requested:
            self._stop_input.clear()
        if self._output_stream is not None and not self._device_responds(
            self._output_stream, self._last_playback_at, now
        ):
            close_error = self._close_stream(self._output_stream)
            output_rate = None
            with self._state:
                self._output_stream = self._output_queue = None
                self._output_warning = "The output device was disconnected or stopped responding."
                if close_error:
                    self._output_warning += f" {close_error}"
                if self._bench_capture is not None and self._bench_capture.reference is not None:
                    self._bench_capture.cancel(self._output_warning)
                    self._bench_capture = None
                self._start_signature = None
                if self._sweep is not None and self._sweep.active:
                    self._sweep.output = None
                    if self._sweep.loopback is None or self._sweep.loopback.exhausted:
                        self._sweep.status = "interrupted"
        if input_rate is None and output_rate is None and not self._loopback:
            warning = self._input_warning or self._output_warning
            if warning:
                raise RuntimeError(warning)
        return input_rate, output_rate

    def _clear_cadence(self):
        self._last_analysis_at = None
        self._average_analysis_ms = 0.0
        self._max_analysis_ms = 0.0
        self._analysis_messages = 0

    def _record_cadence(self, complete_blocks):
        if not complete_blocks:
            return
        now = monotonic()
        if self._last_analysis_at is not None:
            interval = (now - self._last_analysis_at) * 1000 / complete_blocks
            self._average_analysis_ms = (
                self._average_analysis_ms * 0.9 + interval * 0.1
                if self._average_analysis_ms
                else interval
            )
            self._max_analysis_ms = max(interval, self._max_analysis_ms)
        self._last_analysis_at = now
        self._analysis_messages += complete_blocks

    def _work(self, input_rate, output_rate, channels):
        try:
            self._stream_audio(input_rate, output_rate, channels)
        except (ArithmeticError, OSError, RuntimeError, ValueError, self._portaudio_error) as error:
            with self._state:
                self._error = f"Audio stopped. {error}"
                self._halt.set()
                if self._sweep is not None and self._sweep.active:
                    self._sweep.status = "interrupted"
                self._interrupt_captures(self._error)
                if self._output_queue is not None:
                    self._output_queue.mute()
                self._pcm_history = None
                self.processor.reset_signal()
                self._state.notify_all()
            for stream in (self._input_stream, self._output_stream):
                if stream is not None:
                    try:
                        stream.abort()
                    except (OSError, RuntimeError, self._portaudio_error):
                        # Normal lifecycle teardown still owns the handles.
                        continue

    def _stream_audio(self, input_rate, output_rate, channels):
        rate = self.processor.sample_rate
        input_frames = np.empty((max(256, ceil(rate * 0.020)), channels), dtype=np.float32)
        monitor_queue = FrameQueue(ceil(output_rate * 0.08), 2) if input_rate and output_rate else None
        resampler = MonitorResampler(input_rate, output_rate) if monitor_queue is not None else None
        previous_monitoring = False
        signal_errors = self._input_overflows, self._discarded_frames
        next_synthetic = monotonic()
        next_device_check = next_synthetic + DEVICE_CHECK_SECONDS
        while not self._halt.is_set():
            now = monotonic()
            if now >= next_device_check or self._stop_input.is_set():
                input_rate, output_rate = self._check_streams(now, input_rate, output_rate)
                if input_rate is None or output_rate is None:
                    monitor_queue = None
                next_device_check = now + DEVICE_CHECK_SECONDS
            count = self._input_queue.read_into(input_frames) if self._input_queue is not None else 0
            capture = input_frames[:count]
            current_errors = self._input_overflows, self._discarded_frames
            with self._state:
                loopback = self._loopback
                generator = self._generator
                sweep = self._sweep
                monitoring = self._monitor_enabled and not self._interface_loopback_interlock
                mode = self._monitor_mode
                gain = self._monitor_level
                internal_source = self._using_internal_source()
                if self.processor.use_software_clock(internal_source or (loopback and not input_rate)):
                    self._generation += 1
                    self._state.notify_all()
                    next_synthetic = now
                if current_errors != signal_errors:
                    if self._input_queue is not None:
                        self._input_queue.clear()
                    capture = input_frames[:0]
                    if not internal_source:
                        count = 0
                        self.processor.note_gap()
                        self._generation += 1
                        self._state.notify_all()
                    signal_errors = current_errors
                    self._interrupt_captures('Audio frames were dropped during capture. Check the audio device and try again.',
                                             speed=not internal_source)
                    self._pcm_history = None
                    if monitor_queue is not None:
                        monitor_queue.clear()
                        resampler = MonitorResampler(input_rate, output_rate)
                if len(capture):
                    self._keep_pcm(capture, input_rate)
                    if self._bench_capture is not None and self._bench_capture.active:
                        if (self._bench_capture.reference is not None
                                and self._bench_underflows < self._output_underflows):
                            self._bench_capture.cancel('Playback frames were dropped during the response measurement.')
                            self._bench_capture = None
                            if self._output_queue is not None:
                                self._output_queue.mute()
                        else:
                            self._bench_capture.append(capture)
                if self._bench_capture is not None:
                    monitoring = False
                # Software loopback runs on its own clock. Device capture may
                # still run for monitoring, but its callback cadence is independent.
                analysis_input = capture
                if internal_source or (loopback and not input_rate):
                    count = 0
                    if now >= next_synthetic:
                        count = len(input_frames)
                        next_synthetic = max(next_synthetic + count / rate, now)
                        sweep_input = sweep.loopback if sweep is not None and sweep.active else None
                        if sweep_input is not None and not sweep_input.exhausted and not self._software_generator.amplitude:
                            analysis_input = sweep_input.render(count, channels)
                        else:
                            generated = self._software_generator.render(count, rate, generator)
                            analysis_input = (
                                generated if channels == 2 else generated.mean(axis=1, keepdims=True)
                            )
                demodulated = None
                if count:
                    old_sequence = self.processor.sequence
                    demodulated = self.processor.feed(analysis_input)
                    self._publish_raw_speed()
                    self._record_cadence(self.processor.sequence - old_sequence)
                    self._state.notify_all()
            if monitor_queue is not None:
                if monitoring != previous_monitoring:
                    monitor_queue.clear()
                    resampler = MonitorResampler(input_rate, output_rate)
                    previous_monitoring = monitoring
                has_monitor_input = (
                    demodulated is not None if mode == "demodulated" else len(capture) > 0
                )
                if monitoring and has_monitor_input:
                    monitor = monitor_route(capture, demodulated, mode)
                    monitor = resampler.process(monitor) * gain
                    if monitor_queue.count + len(monitor) > len(monitor_queue.data):
                        monitor_queue.clear()
                    monitor_queue.write(monitor)
            with self._state:
                self._fill_output(output_rate, monitor_queue if monitoring else None)
                if self._sweep is not None:
                    self._sweep.advance(self._output_queue, monotonic())
            if not count:
                self._wake.wait(0.004)
                self._wake.clear()

    def _fill_output(self, output_rate, monitor_queue):
        queue = self._output_queue
        if queue is None:
            return
        needed = ceil(output_rate * OUTPUT_BUFFER_SECONDS) - queue.count
        if needed <= 0:
            return
        sweep = self._sweep
        sweep_output = sweep.output if sweep is not None and sweep.active else None
        if self._bench_capture is not None:
            mono = self._bench_capture.output(needed)
            play = np.repeat(mono[:, np.newaxis], 2, axis=1)
        elif self._interface_loopback_interlock:
            play = np.zeros((needed, 2), dtype=np.float32)
        elif sweep_output is not None and not sweep_output.exhausted and not self._device_generator.amplitude:
            if sweep.output_start is None:
                sweep.output_start = queue.frames_written
            play = sweep_output.render(needed, 2)
        else:
            play = self._device_generator.render(needed, output_rate, self._generator)
        # Recheck under the state lock. The worker can render monitoring
        # samples between lock acquisitions while the UI engages the guard.
        if monitor_queue is not None and self._bench_capture is None and not self._interface_loopback_interlock:
            monitor = np.zeros_like(play)
            monitor_queue.read_into(monitor)
            play += monitor
        np.clip(play, -1, 1, out=play)
        if queue.data.shape[1] == 1:
            play = play.mean(axis=1, keepdims=True)
        queue.write(play)

    def drain_measurements(self):
        """Return all completed analysis blocks since the previous drain."""
        with self._state:
            return self.processor.drain_measurements()

    def retain_measurements(self, complete):
        """Keep every measured block when the session is recording full history."""
        with self._state:
            self.processor.retain_measurements(complete)

    def snapshot(self):
        with self._state:
            result = self.processor.snapshot()
            scope = result["scope"]
            status = self._error or ("Running" if self.running else "Stopped")
            input_rate = self._input_stream.samplerate if self.input_enabled else None
            output_rate = self._output_stream.samplerate if self.output_enabled else None
            sweep = self._sweep
            sweep_active = sweep is not None and sweep.active
            cursor = (sweep.output or sweep.loopback) if sweep is not None else None
            frequency, level = cursor.renderer.values_at(sweep.elapsed) if cursor is not None else (None, None)
            result["health"] = {
                "running": self.running,
                "inputOverflow": self._input_overflows,
                "outputUnderflow": self._output_underflows,
                "discardedFrames": self._discarded_frames,
                "status": status,
                "error": self._error,
                "inputWarning": self._input_warning,
                "outputWarning": self._output_warning,
                "inputSampleRate": input_rate,
                "outputSampleRate": output_rate,
                "analysisMessages": self._analysis_messages,
                "detectorDropouts": result["metrics"]["detectorDropouts"],
                "averageAnalysisIntervalMs": self._average_analysis_ms,
                "maxAnalysisIntervalMs": self._max_analysis_ms,
                "channels": self.processor.channels,
                "signalStatus": scope["status"],
                "inputActive": self.input_enabled,
                "outputActive": self.output_enabled,
                "inputRunning": self.input_enabled,
                "outputRunning": self.output_enabled,
                "generatorActive": self._generator.enabled or sweep_active,
                "loopbackActive": self.running and self._using_internal_source(),
            }
            result["generator"] = {
                **asdict(self._generator),
                "effective_frequency": self._generator.effective_frequency(
                    output_rate or self.processor.sample_rate
                ),
                "loopback_frequency": self._generator.effective_frequency(self.processor.sample_rate),
            }
            result["sweep"] = {
                "id": sweep.identifier if sweep is not None else 0,
                "status": sweep.status if sweep is not None else "idle",
                "active": sweep_active,
                "elapsed": sweep.elapsed if sweep is not None else 0.0,
                "duration": sweep.settings.duration if sweep is not None else 0.0,
                "start_frequency": sweep.settings.start_frequency if sweep is not None else None,
                "end_frequency": sweep.settings.end_frequency if sweep is not None else None,
                "frequency": frequency,
                "level": level,
                "output": sweep is not None and sweep.output is not None,
                "loopback": sweep is not None and sweep.loopback is not None,
            }
            result["monitor"] = {
                "enabled": (self._monitor_enabled and not self._interface_loopback_interlock
                            and self._bench_capture is None),
                "level": self._monitor_level,
                "mode": self._monitor_mode,
            }
            result["loopback"] = self._loopback
            return result

    def _wait_for_audio(self, duration, sample=None, interval=0.15):
        end = monotonic() + duration + 1.5
        generation = self._generation
        start = self.processor.frames_processed / self.processor.sample_rate
        elapsed = 0.0
        next_sample = min(interval, duration)
        while elapsed < duration:
            remaining = end - monotonic()
            if remaining <= 0:
                raise RuntimeError("No continuous audio was received during calibration.")
            self._state.wait(remaining)
            if generation != self._generation:
                raise RuntimeError("The audio configuration changed during calibration.")
            if self._error:
                raise RuntimeError(self._error)
            elapsed = self.processor.frames_processed / self.processor.sample_rate - start
            if sample is not None and elapsed >= next_sample:
                sample()
                next_sample = min(elapsed + interval, duration)
        return start

    def calibrate_clock(self, reference_frequency, duration=None):
        reference = bounded(reference_frequency, 1, 24_000, None)
        if reference is None:
            raise ValueError("Enter a valid reference frequency.")
        with self._state:
            if duration is not None:
                start = self._wait_for_audio(bounded(duration, 0.1, 60, 3.0))
            else:
                start = self.processor.frames_processed / self.processor.sample_rate - 3
            readings = np.asarray(self.processor.clock_readings(start))
            reference_readings = readings[np.abs(readings - reference) < reference * 0.10]
            if len(reference_readings) < 2 or len(reference_readings) < 0.8 * len(readings):
                raise RuntimeError("Clock calibration needs a steady reference tone on the left input.")
            measured = float(np.median(reference_readings))
            if np.std(reference_readings) > 0.002 * measured:
                raise RuntimeError("The calibration tone frequency was unstable.")
            return reference / measured

    def calibrate_channels(self, reference_frequency=1000, duration=None):
        reference = bounded(reference_frequency, 1, 24_000, None)
        if reference is None:
            raise ValueError("Enter a valid reference frequency.")
        with self._state:
            readings = []
            if duration is not None:
                self._wait_for_audio(
                    bounded(duration, 0.1, 60, 3.0),
                    lambda: readings.append(self.processor.channel_readings(reference)),
                    interval=max(0.15, 3 / reference),
                )
            else:
                readings.append(self.processor.channel_readings(reference))
            levels = [
                (tone["rawLeftDb"], tone["rawRightDb"])
                for tone in readings
                if tone["available"] and tone["referencePresent"]
            ]
            if not levels or len(levels) < 0.8 * len(readings):
                raise RuntimeError("Channel calibration needs the same steady tone on both inputs.")
            pairs = np.asarray(levels)
            differences = pairs[:, 0] - pairs[:, 1]
            if np.std(differences) > 0.75:
                raise RuntimeError("The channel balance changed during calibration.")
            left, right = map(float, np.median(pairs, axis=0))
            difference = float(np.median(differences))
            return {
                "leftTrimDb": max(-24.0, min(24.0, -difference / 2)),
                "rightTrimDb": max(-24.0, min(24.0, difference / 2)),
                "imbalanceDb": difference,
                "referenceFrequency": reference,
                "rawLeftDb": left,
                "rawRightDb": right,
            }

    def channel_readings(self, reference_frequency):
        """Read calibrated and uncalibrated stereo levels at a reference tone."""
        reference = bounded(reference_frequency, 1, 24_000, None)
        if reference is None:
            raise ValueError("Enter a valid reference frequency.")
        with self._state:
            return self.processor.channel_readings(reference)
