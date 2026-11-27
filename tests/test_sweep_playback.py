"""Exercise streaming sweeps with a simulated DAC and internal loopback."""

from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import pytest

from fluttery.audio.engine import AudioEngine
from fluttery.audio.sweep import SweepRenderer, SweepSettings

RATE = 8000
OUTPUT_BLOCK = 320


def wait_until(predicate):
    deadline = monotonic() + 2
    while not predicate():
        assert monotonic() < deadline, 'Timed out waiting for the audio worker.'
        sleep(0.004)


@pytest.fixture
def simulated_output(monkeypatch):
    engine = AudioEngine()
    streams = []

    class Output:
        latency = 0.12

        def __init__(self, *, samplerate, channels, callback, **_options):
            self.samplerate = samplerate
            self.channels = channels
            self.callback = callback
            self.active = False
            streams.append(self)

        def start(self):
            self.active = True

        def abort(self):
            self.active = False

        def close(self):
            self.active = False

        def consume(self):
            wait_until(lambda: engine._output_queue.count >= OUTPUT_BLOCK)
            block = np.empty((OUTPUT_BLOCK, self.channels), dtype=np.float32)
            self.callback(block, len(block), None, SimpleNamespace(output_underflow=False))
            return block

    backend = SimpleNamespace(
        PortAudioError=OSError,
        query_devices=lambda _device, _direction: {'max_output_channels': 2, 'default_samplerate': RATE},
        check_output_settings=lambda **_options: None,
        OutputStream=Output,
    )
    monkeypatch.setattr(engine, '_backend', lambda: backend)
    engine.start(input_enabled=False)
    try:
        yield engine, streams[0]
    finally:
        engine.close()


def test_output_sweep_waits_for_callback_delivery_and_device_latency(simulated_output):
    engine, output = simulated_output
    settings = SweepSettings(start_frequency=80, end_frequency=1000, duration=0.12)
    identifier = engine.start_sweep(settings)
    np.testing.assert_array_equal(output.consume(), 0)  # The existing buffer starts with silence.
    wait_until(lambda: engine._output_queue.count >= OUTPUT_BLOCK)
    pending = engine.snapshot()['sweep']
    assert pending['active'] and pending['id'] == identifier and pending['elapsed'] == 0
    assert pending['output'] and not pending['loopback']

    first = output.consume()
    wait_until(lambda: engine.snapshot()['sweep']['elapsed'] >= 0.04)
    assert engine.snapshot()['sweep']['active']
    second = output.consume()
    third = output.consume()
    wait_until(lambda: engine.snapshot()['sweep']['elapsed'] >= settings.duration)
    assert engine.snapshot()['sweep']['active']  # DAC latency has yet to elapse.
    wait_until(lambda: engine.snapshot()['sweep']['status'] == 'completed')

    delivered = np.concatenate((first, second, third))
    expected = SweepRenderer(settings, RATE).render(len(delivered))
    np.testing.assert_array_equal(delivered[:, 0], delivered[:, 1])
    np.testing.assert_array_equal(delivered[:, 0], expected)


def test_stopping_a_sweep_ramps_to_zero_after_the_queued_samples(simulated_output):
    engine, output = simulated_output
    output.latency = 0
    settings = SweepSettings(start_frequency=700, end_frequency=900, duration=1)
    engine.start_sweep(settings)
    output.consume()
    wait_until(lambda: engine._output_queue.count >= OUTPUT_BLOCK)
    assert engine.stop_sweep()
    assert not engine.stop_sweep()
    assert engine.snapshot()['sweep']['status'] == 'stopping'

    first, final = output.consume(), output.consume()
    fade_length = round(RATE * 0.005)
    expected = SweepRenderer(settings, RATE).render(OUTPUT_BLOCK + fade_length)
    np.testing.assert_array_equal(first[:, 0], expected[:OUTPUT_BLOCK])
    fade = 1 - np.arange(1, fade_length + 1) / fade_length
    np.testing.assert_allclose(final[:fade_length, 0], expected[OUTPUT_BLOCK:] * fade, atol=1e-8)
    np.testing.assert_array_equal(final[fade_length:, :], 0)
    wait_until(lambda: engine.snapshot()['sweep']['status'] == 'stopped')


def test_loopback_sweeps_feed_the_analyser_and_restart_from_the_beginning(monkeypatch):
    engine = AudioEngine()
    monkeypatch.setattr(engine, '_backend', lambda: pytest.fail('Internal loopback opened a device.'))
    engine.set_loopback(True)
    engine.start(input_enabled=False, output_enabled=False)
    settings = SweepSettings(start_frequency=2995, end_frequency=3005, duration=0.24)
    try:
        first = engine.start_sweep(settings)
        wait_until(lambda: engine.snapshot()['metrics']['locked'])
        playing = engine.snapshot()
        assert playing['sweep']['loopback'] and 0 < playing['sweep']['elapsed'] < settings.duration
        assert playing['health']['loopbackActive'] and playing['health']['generatorActive']
        assert not playing['health']['inputActive'] and not playing['health']['outputActive']
        assert 2995 < playing['sweep']['frequency'] < 3005
        wait_until(lambda: engine.snapshot()['sweep']['status'] == 'completed')

        second = engine.start_sweep(settings)
        assert second != first
        assert engine.snapshot()['sweep']['elapsed'] < 0.08
        engine.set_generator(enabled=True, frequency=1000)
        wait_until(lambda: engine.snapshot()['sweep']['status'] == 'stopped')
        assert engine.snapshot()['generator']['enabled']
    finally:
        engine.close()


def test_sweep_preserves_fixed_tone_on_validation_failure_and_interrupts_on_shutdown(simulated_output):
    engine, _ = simulated_output
    engine.set_generator(enabled=True, frequency=300)
    with pytest.raises(ValueError, match='sample rate'):
        engine.start_sweep(SweepSettings(start_frequency=50, end_frequency=4000))
    assert engine.snapshot()['generator']['enabled']
    assert engine.snapshot()['sweep']['status'] == 'idle'

    run = engine.start_sweep(SweepSettings(start_frequency=50, end_frequency=1000, duration=2))
    assert not engine.snapshot()['generator']['enabled']
    engine.stop()
    sweep = engine.snapshot()['sweep']
    assert not sweep['active'] and sweep['status'] == 'interrupted' and sweep['id'] == run
