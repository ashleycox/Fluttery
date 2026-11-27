"""Synthesis and default-output interface sound routing, without hardware."""

from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from fluttery.measurement_ui import MeasurePanel
from fluttery.preferences import normalize_settings
from fluttery.ui_sounds import EFFECTS, MATERIALS, RATE, InterfaceSounds, _synth


@pytest.mark.parametrize("theme", tuple(MATERIALS))
def test_sounds_have_distinct_short_and_faded_waveforms(theme):
    sounds = {name: _synth(theme, name) for name in EFFECTS}
    for name, waveform in sounds.items():
        assert waveform.ndim == 1
        assert waveform.dtype == np.float32
        peak = np.max(np.abs(waveform))
        if name == "focus":
            assert 0.015 < peak < 0.035
        else:
            assert 0.06 < peak < 0.30
        assert np.sqrt(np.mean(waveform * waveform)) < 0.05
        assert waveform[0] == waveform[-1] == 0
        assert abs(waveform.mean()) < 0.002
    fingerprints = {waveform.tobytes() for waveform in sounds.values()}
    assert len(fingerprints) == len(EFFECTS)
    assert len(sounds["focus"]) / RATE < 0.025
    assert 0.23 < len(sounds["counter_reset"]) / RATE < 0.35


@pytest.mark.parametrize("theme", tuple(MATERIALS))
def test_counter_reset_has_a_gear_burst_and_a_final_stop(theme):
    reset = _synth(theme, "counter_reset")
    travel = MATERIALS[theme].travel

    def energy(start, end):
        first = round(start * RATE * travel)
        last = round(end * RATE * travel)
        return float(np.max(np.abs(reset[first:last])))

    assert energy(0, 0.03) > 0.1
    assert energy(0.04, 0.20) > 0.05
    assert energy(0.22, 0.235) < 0.015
    assert energy(0.24, 0.265) > 0.1


def test_themes_change_the_switch_timbre_and_decay():
    def features(theme):
        samples = _synth(theme, "switch")
        power = np.square(np.abs(np.fft.rfft(samples)))
        frequencies = np.fft.rfftfreq(len(samples), d=1 / RATE)
        return (
            power[frequencies > 2000].sum() / power.sum(),
            power[frequencies < 600].sum() / power.sum(),
            np.square(samples[RATE // 100 :]).sum() / np.square(samples).sum(),
        )

    modern = features("modern")
    retro = features("retro")
    soft = features("neumorphism")
    blunt = features("neubrutalism")
    assert modern[0] > 2 * blunt[0] and blunt[0] > 4 * soft[0]
    assert blunt[1] > retro[1] > 5 * modern[1]
    assert retro[2] > 1.5 * soft[2] and soft[2] > 2 * modern[2]


def test_counter_advances_silently_and_can_still_be_reset():
    cues = []
    controller = SimpleNamespace(measurement_elapsed=0.89, is_measuring=True, play_sound=cues.append,
                                 announce=lambda _message: None)
    panel = SimpleNamespace(counter_base=0.0, counter_start=None, controller=controller, snapshot={},
                            refresh_measurement=lambda _snapshot: None)
    for elapsed in (0.89, 0.94, 1.01, 1.11):
        controller.measurement_elapsed = elapsed
        assert MeasurePanel._counter_time(panel, True) == pytest.approx(elapsed)
    assert not cues

    MeasurePanel._reset_counter(panel)
    assert MeasurePanel._counter_time(panel, True) == 0
    assert cues == ["counter_reset"]
    assert "counterSounds" not in normalize_settings({"counterSounds": True})


def test_removed_counter_sounds_cannot_queue():
    sounds = InterfaceSounds()
    try:
        sounds.play("retro", "counter_tick")
        sounds.play("retro", "counter_carry")
        assert not sounds._pending and sounds._thread is None
    finally:
        sounds.close()


def test_shared_native_stream_plays_nonzero_pcm_and_closes(monkeypatch):
    config = {}
    blocks = []
    wrote = Event()
    closed = Event()

    class FakeOutput:
        def __init__(self, **options):
            config.update(options)

        def start(self):
            pass

        def write(self, block):
            blocks.append(block.copy())
            wrote.set()

        def abort(self):
            pass

        def stop(self):
            pass

        def close(self):
            closed.set()

    def devices(*, kind):
        assert kind == "output"
        return {"default_samplerate": 96_000, "max_output_channels": 2}

    sounds = InterfaceSounds()
    monkeypatch.setattr(
        sounds, "_backend", lambda: SimpleNamespace(OutputStream=FakeOutput, query_devices=devices)
    )
    try:
        sounds.play("retro", "switch")
        assert wrote.wait(1)
        assert config == {"samplerate": 96_000, "channels": 2, "dtype": "float32", "latency": "low"}
        assert np.max(np.abs(blocks[0])) > 0.1
        assert np.array_equal(blocks[0][:, 0], blocks[0][:, 1])
        assert sounds.last_error == ""
    finally:
        sounds.close()
    assert closed.wait(1)


def test_missing_output_is_reported_without_breaking_controls(monkeypatch):
    class OutputFailure(Exception):
        pass

    attempted = Event()

    def no_output(**kwargs):
        attempted.set()
        raise OutputFailure("No default output")

    sounds = InterfaceSounds()
    monkeypatch.setattr(
        sounds,
        "_backend",
        lambda: SimpleNamespace(PortAudioError=OutputFailure, query_devices=no_output),
    )
    sounds.play("retro", "button")
    assert attempted.wait(1)
    sounds.close()
    assert sounds.last_error == "No default output"


def test_focus_is_throttled_separately_and_never_queues_ahead_of_actions(monkeypatch):
    sounds = InterfaceSounds()
    monkeypatch.setattr(sounds, "_run", lambda: None)
    try:
        sounds.play("retro", "focus")
        sounds.play("retro", "focus")
        assert [item[1] for item in sounds._pending] == ["focus"]
        sounds.play("retro", "switch")
        sounds.play("retro", "start")
        sounds.play("retro", "stop")
        sounds.play("retro", "focus")
        assert [item[1] for item in sounds._pending] == ["switch", "start", "stop"]
    finally:
        sounds.close()


def test_action_preempts_an_in_flight_focus_cue(monkeypatch):
    focus_wrote = Event()
    action_wrote = Event()
    continue_playback = Event()
    played = []

    class FakeOutput:
        def __init__(self, **options):
            pass

        def start(self):
            pass

        def write(self, block):
            peak = np.max(block)
            if peak < 0.2:
                played.append("focus")
                focus_wrote.set()
                continue_playback.wait(1)
            else:
                played.append("switch")
                action_wrote.set()

        def abort(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    def device_info(**kwargs):
        return {"default_samplerate": RATE, "max_output_channels": 2}

    def sample(_theme, name):
        if name == "focus":
            return np.full(RATE, 0.1, dtype=np.float32)
        return np.full(RATE // 25, 0.3, dtype=np.float32)

    sounds = InterfaceSounds()
    monkeypatch.setattr("fluttery.ui_sounds._synth", sample)
    monkeypatch.setattr(
        sounds, "_backend", lambda: SimpleNamespace(OutputStream=FakeOutput, query_devices=device_info)
    )
    try:
        sounds.play("retro", "focus")
        assert focus_wrote.wait(1)
        sounds.play("retro", "switch")
        continue_playback.set()
        assert action_wrote.wait(1)
        assert played[:2] == ["focus", "switch"]
        assert played.count("focus") == 1
    finally:
        continue_playback.set()
        sounds.close()


def test_failed_idle_stop_still_closes_stream_and_next_cue_plays(monkeypatch):
    streams = []
    first_closed, second_played = Event(), Event()

    class FakeOutput:
        def __init__(self, **options):
            self.number = len(streams)
            streams.append(self)

        def start(self):
            pass

        def write(self, _block):
            if self.number == 1:
                second_played.set()

        def stop(self):
            if self.number == 0:
                raise OSError('Output device disconnected')

        def abort(self):
            pass

        def close(self):
            if self.number == 0:
                first_closed.set()

    sounds = InterfaceSounds()
    monkeypatch.setattr('fluttery.ui_sounds.IDLE_SECONDS', .01)
    monkeypatch.setattr(sounds, '_backend', lambda: SimpleNamespace(
        OutputStream=FakeOutput, query_devices=lambda **_: {'default_samplerate': RATE, 'max_output_channels': 1},
    ))
    try:
        sounds.play('modern', 'switch')
        assert first_closed.wait(1)
        assert sounds.last_error == 'Output device disconnected'
        sounds.play('modern', 'start')
        assert second_played.wait(1)
    finally:
        sounds.close()
