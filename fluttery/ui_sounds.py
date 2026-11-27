"""Built-in mechanical interface sounds played through the system audio output."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from math import gcd
from threading import Condition, Thread

import numpy as np
from scipy import signal

RATE = 48_000
IDLE_SECONDS = 0.6
# Keep the excitation noise of each cue stable when another cue is removed.
EFFECT_SEEDS = {"switch": 179, "dial": 252, "button": 325, "counter_reset": 471, "start": 544, "stop": 617, "focus": 690}
EFFECTS = tuple(EFFECT_SEEDS)


@dataclass(frozen=True)
class Material:
    # Modal ratios, amplitudes and decay multipliers describe the radiating case.
    modes: tuple[tuple[float, float, float], ...]
    pitch: float
    contact_bandwidth: float
    attack: float
    travel: float
    return_strength: float
    volume: float


MATERIALS = {
    # Compact hard polymer, a quick detent and short case resonances.
    "modern": Material(
        modes=((1.0, 0.70, 0.72), (2.26, 0.26, 0.43), (3.68, 0.11, 0.28)),
        pitch=1.24,
        contact_bandwidth=7400,
        attack=0.0002,
        travel=0.83,
        return_strength=0.67,
        volume=0.85,
    ),
    # Metal lever and steel chassis with a deeper body and audible leaf spring.
    "retro": Material(
        modes=((0.78, 0.75, 1.18), (1.67, 0.37, 0.88), (2.52, 0.28, 0.62), (4.24, 0.13, 0.36)),
        pitch=1.0,
        contact_bandwidth=4700,
        attack=0.00036,
        travel=1.06,
        return_strength=1.0,
        volume=1.0,
    ),
    # Rubber-damped key with a soft, short contact and little upper ringing.
    "neumorphism": Material(
        modes=((0.90, 0.93, 0.71), (1.88, 0.16, 0.30), (3.08, 0.07, 0.21)),
        pitch=0.85,
        contact_bandwidth=1700,
        attack=0.00083,
        travel=0.96,
        return_strength=0.55,
        volume=0.64,
    ),
    # Rigid toggle on a thick panel. A low knock sits under a bright snap.
    "neubrutalism": Material(
        modes=((0.67, 1.0, 1.1), (1.91, 0.24, 0.72), (3.17, 0.38, 0.38), (5.05, 0.15, 0.28)),
        pitch=0.88,
        contact_bandwidth=5900,
        attack=0.00015,
        travel=0.93,
        return_strength=0.83,
        volume=0.94,
    ),
}

# Each actuation is a short series of impacts: time, force, base resonance
# and decay. Separate parts can strike at different pitches within one event.
IMPACTS = {
    "dial": ((0, 0.68, 1200, 0.0029), (0.004, 0.40, 1540, 0.0027), (0.013, 0.20, 720, 0.003)),
    "button": ((0, 0.67, 390, 0.008), (0.012, 1.0, 550, 0.007), (0.028, 0.31, 265, 0.009)),
    "counter_reset": (
        (0, 0.88, 365, 0.009),
        (0.016, 0.27, 970, 0.004),
        (0.238, 1.05, 265, 0.012),
        (0.253, 0.32, 640, 0.004),
    ),
    "start": ((0, 0.45, 570, 0.005), (0.017, 1.0, 285, 0.010), (0.047, 0.38, 745, 0.005)),
    "stop": ((0, 1.0, 410, 0.0077), (0.010, 0.51, 880, 0.0038), (0.027, 0.36, 245, 0.008)),
    "focus": ((0, 0.70, 840, 0.0027),),
}
LENGTH = {
    "switch": 0.052,
    "dial": 0.037,
    "button": 0.094,
    "counter_reset": 0.302,
    "start": 0.111,
    "stop": 0.092,
    "focus": 0.019,
}
PEAK = {
    "switch": 0.22,
    "dial": 0.16,
    "button": 0.25,
    "counter_reset": 0.27,
    "start": 0.24,
    "stop": 0.25,
    "focus": 0.029,
}


def _contact(count, bandwidth, rng):
    # Shape a short contact-noise excitation with an AC-coupled material loss.
    grain = signal.lfilter([1, -1], [1, -0.965], rng.standard_normal(count))
    decay = np.exp(-2 * np.pi * bandwidth / RATE)
    return signal.lfilter([1 - decay], [1, -decay], grain)


def _impact(samples, offset, force, pitch, decay, material, rng, *, bend=0.0):
    index = round(offset * RATE * material.travel)
    count = min(len(samples) - index, round(9 * decay * RATE))
    if count <= 0:
        return
    elapsed = np.arange(count) / RATE
    contact = 0.60 * _contact(count, material.contact_bandwidth, rng)
    contact *= np.exp(-elapsed / (0.22 * decay))
    resonances = np.zeros_like(elapsed)
    base_frequency = pitch * material.pitch
    position = elapsed
    if bend:
        # Integrate the spring's frequency as its initial tension relaxes.
        relaxation = 0.6 * decay
        position = elapsed - bend * relaxation * np.expm1(-elapsed / relaxation)
    for ratio, amplitude, damping in material.modes:
        resonances += amplitude * (
            np.sin(2 * np.pi * base_frequency * ratio * position) * np.exp(-elapsed / (decay * damping))
        )
    attack = -np.expm1(-elapsed / material.attack)
    samples[index : index + count] += force * attack * (contact + resonances)


def _friction(samples, offset, duration, force, material, rng):
    index = round(offset * RATE * material.travel)
    count = min(len(samples) - index, round(duration * RATE * material.travel))
    motion = np.sin(np.linspace(0, np.pi, count)) ** 2
    samples[index : index + count] += force * motion * _contact(count, 0.58 * material.contact_bandwidth, rng)


def _switch(samples, material, rng):
    # The lever rubs in its guide, the spring snaps over centre, and the lever
    # strikes the panel stop. The little contact bounce follows the stop.
    _friction(samples, 0, 0.0068, 0.16, material, rng)
    _impact(samples, 0.0035, 1.0, 1040, 0.0034, material, rng, bend=0.10)
    stop = material.return_strength
    _impact(samples, 0.0085, 0.89 * stop, 540, 0.0063, material, rng)
    _impact(samples, 0.0095, 0.21 * stop, 1760, 0.0017, material, rng)
    _impact(samples, 0.014, 0.19 * stop, 810, 0.0032, material, rng)


def _ratchet(samples, material, rng):
    # The reset cam releases the number wheels. The teeth accelerate, coast
    # and slow into the end stop. The higher digits rattle behind the first.
    _friction(samples, 0.026, 0.193, 0.12, material, rng)
    positions = (0.032, 0.055, 0.074, 0.091, 0.105, 0.118, 0.132, 0.147, 0.165, 0.185, 0.209)
    for tooth, offset in enumerate(positions):
        force = 0.32 + 0.23 * np.sin(np.pi * tooth / (len(positions) - 1))
        pitch = 1180 + 160 * np.sin(0.84 * tooth)
        force *= rng.uniform(0.85, 1.05)
        _impact(samples, offset, force, pitch, 0.0034, material, rng)
        _impact(samples, offset + rng.uniform(0.0011, 0.0039), 0.39 * force, 0.87 * pitch, 0.0021, material, rng)


def _synth(theme, name):
    material = MATERIALS[theme]
    samples = np.zeros(round(LENGTH[name] * RATE * material.travel), dtype=np.float64)
    theme_seed = tuple(MATERIALS).index(theme)
    rng = np.random.default_rng(EFFECT_SEEDS[name] + theme_seed * 13)
    if name == "switch":
        _switch(samples, material, rng)
    else:
        for index, (offset, force, pitch, decay) in enumerate(IMPACTS[name]):
            if index and name != "counter_reset":
                force *= material.return_strength
            _impact(samples, offset, force, pitch, decay, material, rng)
        if name == "counter_reset":
            _ratchet(samples, material, rng)

    samples -= samples.mean()
    edge = round(RATE * 0.002)
    samples[:edge] *= np.linspace(0, 1, edge)
    samples[-edge:] *= np.linspace(1, 0, edge)
    level = PEAK[name] * material.volume
    return (samples * (level / np.max(np.abs(samples)))).astype(np.float32)


def _output_frames(samples, rate, channels):
    if rate != RATE:
        divisor = gcd(rate, RATE)
        samples = signal.resample_poly(samples, rate // divisor, RATE // divisor)
    return np.ascontiguousarray(np.repeat(samples[:, np.newaxis], channels, axis=1), dtype=np.float32)


class InterfaceSounds:
    """Use a short-lived default-output stream without touching the measurement graph."""

    def __init__(self):
        self.last_error = ""
        self._pending = deque(maxlen=8)
        self._condition = Condition()
        self._thread = None
        self._closed = False
        self._generation = 0
        self._samples = {}
        self._last_event = {}

    @staticmethod
    def _backend():
        import sounddevice

        return sounddevice

    def play(self, appearance, name):
        if name not in EFFECTS:
            return
        theme = appearance if appearance in MATERIALS else "modern"
        now = time.monotonic()
        focus = name == "focus"
        minimum_interval = 0.10 if focus else 0.045
        with self._condition:
            if self._closed or now - self._last_event.get(name, float("-inf")) < minimum_interval:
                return
            if self._pending:
                if focus:
                    return
                if self._pending[0][1] == "focus":
                    self._pending.popleft()
            self._last_event[name] = now
            self._pending.append((theme, name, now, self._generation))
            if self._thread is None:
                self._thread = Thread(target=self._run, name="Fluttery interface audio", daemon=True)
                self._thread.start()
            self._condition.notify()

    def stop(self):
        """Cancel queued effects and interrupt any current effect."""
        with self._condition:
            self._generation += 1
            self._pending.clear()
            self._last_event.clear()
            self._condition.notify()

    def close(self):
        self.stop()
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(timeout=0.6)

    def _sample(self, theme, name, rate, channels):
        key = (theme, name, rate, channels)
        if key not in self._samples:
            self._samples[key] = _output_frames(_synth(theme, name), rate, channels)
        return self._samples[key]

    def _dispose_stream(self, stream, backend, *, abort):
        errors = (OSError, RuntimeError, ValueError, getattr(backend, "PortAudioError", OSError))
        try:
            (stream.abort if abort else stream.stop)()
        except errors as error:
            self.last_error = str(error)
        finally:
            try:
                stream.close()
            except errors as error:
                self.last_error = str(error)

    def _run(self):
        backend = None
        stream = None
        rate, channels = 0, 0
        retry_at = 0.0
        try:
            while True:
                with self._condition:
                    if not self._closed and not self._pending:
                        self._condition.wait(IDLE_SECONDS if stream is not None else None)
                    if self._closed:
                        break
                    item = self._pending.popleft() if self._pending else None
                if item is None:
                    if stream is not None:
                        self._dispose_stream(stream, backend, abort=False)
                        stream = None
                    continue
                theme, name, requested_at, generation = item
                now = time.monotonic()
                focus = name == "focus"
                max_age = 0.16 if focus else 0.5
                if now < retry_at or now - requested_at > max_age or generation != self._generation:
                    continue

                try:
                    if backend is None:
                        backend = self._backend()
                    if stream is None:
                        info = backend.query_devices(kind="output")
                        rate = round(info["default_samplerate"])
                        channels = min(2, info["max_output_channels"])
                    frames = self._sample(theme, name, rate, channels)
                    if stream is None:
                        # The system default is intentional. Never send these
                        # effects to a user-selected measurement DAC.
                        stream = backend.OutputStream(
                            samplerate=rate, channels=channels, dtype="float32", latency="low"
                        )
                        stream.start()
                    if time.monotonic() - requested_at > max_age:
                        continue
                    chunk = max(1, rate // 50)
                    for offset in range(0, len(frames), chunk):
                        if generation != self._generation:
                            break
                        if focus:
                            with self._condition:
                                if self._pending and self._pending[0][1] != "focus":
                                    break
                        stream.write(frames[offset : offset + chunk])
                    if generation != self._generation:
                        self._dispose_stream(stream, backend, abort=True)
                        stream = None
                    else:
                        self.last_error = ""
                except (
                    ImportError,
                    OSError,
                    RuntimeError,
                    ValueError,
                    getattr(backend, "PortAudioError", OSError),
                ) as error:
                    if stream is not None:
                        self._dispose_stream(stream, backend, abort=True)
                        stream = None
                    self.last_error = str(error)
                    retry_at = time.monotonic() + 1
        finally:
            if stream is not None:
                self._dispose_stream(stream, backend, abort=True)
