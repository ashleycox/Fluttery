import numpy as np
import pytest
from scipy import signal

from fluttery.audio.bench import (
    SpectrumAverage,
    StepStimulus,
    analyse_imd_capture,
    analyse_steps,
    two_tone_imd,
)
from fluttery.audio.scope import ScopeBuffer

RATE = 48000


def test_joint_imd_tracks_noncoherent_carriers_and_sidebands():
    time = np.arange(round(0.83 * RATE)) / RATE
    first, second = 60.17, 7001.3
    carrier = .08
    sideband = .0008
    samples = (.31 * np.sin(2 * np.pi * first * time + .20)
               + carrier * np.sin(2 * np.pi * second * time - .86)
               + sideband * np.sin(2 * np.pi * (second - first) * time + .60)
               + sideband * np.sin(2 * np.pi * (second + first) * time))
    reading = two_tone_imd(samples, RATE, 60, 7000)
    assert reading['firstHz'] == pytest.approx(first, abs=.01)
    assert reading['secondHz'] == pytest.approx(second, abs=.01)
    phase_offset = .60 - 2 * -.86
    assert reading['imdPercent'] == pytest.approx(200 * sideband / carrier * abs(np.cos(phase_offset / 2)), rel=.005)
    assert not reading['clipping']


def test_modulation_imd_recovers_am_depth_and_rejects_phase_quadrature():
    time = np.arange(round(0.71 * RATE)) / RATE
    envelope = .072 * np.cos(2 * np.pi * 250.3 * time + 1.3)
    phase = 2 * np.pi * 8014.5 * time - .45
    bass = .40 * np.sin(2 * np.pi * 250.3 * time)
    am = bass + .10 * (1 + envelope) * np.sin(phase)
    quadrature = bass + .10 * (np.sin(phase) + envelope * np.cos(phase))
    assert two_tone_imd(am, RATE, 250, 8000, mode='din')['imdPercent'] == pytest.approx(7.2, abs=.02)
    assert two_tone_imd(quadrature, RATE, 250, 8000, mode='din')['imdPercent'] < .005


def test_ccif_measures_difference_tone_against_the_sum_and_lists_im3():
    time = np.arange(RATE // 2) / RATE
    samples = sum(amplitude * np.sin(2 * np.pi * frequency * time - .2) for frequency, amplitude in (
        (19000, .12), (20000, .12), (1000, .0012), (18000, .0006), (21000, .0006),
    ))
    reading = two_tone_imd(samples, RATE, 19000, 20000, mode='ccif')
    assert reading['imdPercent'] == pytest.approx(.5, rel=.005)
    assert [product['levelDbc'] for product in reading['products']] == pytest.approx(
        [-46.0206, -52.0412, -52.0412], abs=.02,
    )


@pytest.mark.parametrize('rate', (44_100, 48_000, 96_000))
def test_generated_imd_uses_the_full_minimum_observation_and_preserves_usable_channel(rate):
    first, second = 60, 7000
    stimulus = StepStimulus(((first, -12.),), settle=.1, observe=.25, second_hz=second, low_to_high=4)
    sent = stimulus.render(rate)
    start, _, _ = stimulus.segments(rate)
    hold = stimulus.observation(0, rate)
    assert hold.stop - hold.start == round(rate / 4)
    time = np.arange(hold.stop - hold.start) / rate
    observed = slice(start + hold.start, start + hold.stop)
    # A calibrated add-on modulation present only in the left capture.
    reference = sent.copy()
    reference[observed] += .0008 * np.sin(2 * np.pi * (second + first) * (time + stimulus.settle))
    pcm = np.pad(np.column_stack((reference, np.zeros_like(reference))), ((1510, 3410), (0, 0)))
    left, right = analyse_imd_capture(pcm, rate, first, second, mode='smpte', stimulus=stimulus,
                                    reference=sent, output_rate=rate)
    assert left['imdPercent'] == pytest.approx(100 * .0008 * 5 / (10**(-12 / 20)), abs=.002)
    assert right['error'] and right['imdPercent'] is None and not right['products']


def test_imd_rejects_noise_as_a_reference_and_reports_the_difference_tone_limit():
    random = np.random.default_rng(83)
    noise = random.normal(0, .001, RATE)
    with pytest.raises(ValueError, match='noise floor'):
        two_tone_imd(25 * noise, RATE, 60, 7000)
    time = np.arange(RATE) / RATE
    signal = sum(amplitude * np.sin(2 * np.pi * frequency * time) for frequency, amplitude in
                 ((19000, .12), (20000, .12), (18000, .001)))
    reading = two_tone_imd(signal + noise, RATE, 19000, 20000, mode='ccif')
    assert reading['imdLimited'] and 0 < reading['imdLimitPercent'] < .05
    assert not reading['products'][1]['limited']


def test_spectrum_density_integrates_to_rolling_mean_square_and_retains_peak():
    fft = SpectrumAverage()
    time = np.arange(4800) / RATE
    for amplitude in (.8, .2, .4):
        _, mean, maximum = fft.observe(amplitude * np.cos(2 * np.pi * 1000 * time), RATE, average=2)
    rms_squared = np.sum(10 ** (mean / 10)) * 10
    assert rms_squared == pytest.approx((.2**2 + .4**2) / 4, rel=1e-5)
    assert np.max(np.sum(10 ** (maximum / 10), axis=0) * 10) == pytest.approx(.8**2 / 2, rel=1e-5)


def test_spectrum_consumes_peaks_that_happen_between_display_polls():
    fft = SpectrumAverage()
    length = 4096
    epoch = object()
    assert fft.consume(ScopeBuffer(RATE, np.zeros((length, 1)), length, epoch), length)
    t = np.arange(length) / RATE
    block = .6 * np.sin(2 * np.pi * (100 * RATE / length) * t)
    queue = np.concatenate((np.zeros(length), block, np.zeros(2 * length)))[:, None]
    final = ScopeBuffer(RATE, queue, len(queue), epoch)
    assert fft.consume(final, length, average=1)
    frequency, average, peak = fft.values()
    assert fft.frames == 7 and not fft.consume(final, length)
    assert np.max(average) <= -290
    assert frequency[np.argmax(peak)] == pytest.approx(100 * RATE / length)
    # Peak hold is the maximum per bin, so summing it can include sidelobes
    # that peaked in different blocks. Check the on-bin Hann density instead.
    assert np.max(10 ** (peak / 10)) == pytest.approx((.6**2 / 2) / (1.5 * RATE / length), rel=1e-5)


def test_step_analysis_retains_independent_channels_and_rejects_clipping():
    stimulus = StepStimulus(((701., -20.), (1297., -18.)), settle=.15, observe=.31)
    reference = stimulus.render(RATE)
    clipped = reference.copy()
    start = stimulus.segments(RATE)[0] + round(stimulus.period * RATE)
    clipped[start:start + round(stimulus.period * RATE)] *= 20
    returned = np.pad(np.column_stack((reference, np.clip(clipped, -1, 1))), ((3100, 9000), (0, 0)))
    rows, delay, _ = analyse_steps(reference, returned, RATE, RATE, stimulus)
    assert delay == pytest.approx(1000 * 3100 / RATE, abs=.02)
    assert [row['channel'] for row in rows] == [1, 2, 1, 2]
    assert rows[2]['detectedHz'] == pytest.approx(1297, abs=.02)
    assert rows[0]['thdn'] < .001
    assert rows[3]['clipping'] and rows[3]['thdn'] is None


def test_step_detects_converter_clipping_before_fractional_alignment():
    frequency = RATE / 4
    stimulus = StepStimulus(((frequency, -20.),), settle=.1, observe=.25)
    reference = stimulus.render(RATE)
    padded = np.pad(reference, (2100, 4000))
    returned = signal.resample_poly(padded, 2, 1)[1::2]
    start, _, _ = stimulus.segments(RATE)
    window = stimulus.observation(0, RATE)
    indices = np.arange(start + 2100 + window.start - 32, start + 2100 + window.stop + 32)
    # At one-quarter sample rate every raw converter value has a magnitude
    # near 0.85, while reconstruction between those samples reaches 1.2.
    returned[indices] = 1.2 * np.sin((np.pi / 2) * (indices - 2100 - start + .5))
    rows, _, _ = analyse_steps(reference, returned, RATE, RATE, stimulus)
    assert not rows[0]['clipping']
    assert rows[0]['signalDbfs'] == pytest.approx(20 * np.log10(1.2 / np.sqrt(2)), abs=.02)
    assert rows[0]['thd'] is None and rows[0]['thdn'] < .005
