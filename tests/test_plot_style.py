"""Coverage for display choices that could hide or mislabel recorded data."""

import numpy as np

from fluttery.plot_style import (
    PlotStyle,
    axis_ticks,
    cell_edges,
    contrast_ratio,
    heat_image,
    peak_sample_indices,
)


def test_pixel_decimation_retains_a_one_sample_peak_and_trough():
    x = np.repeat(np.arange(4), 30)
    y = np.zeros(len(x))
    y[47], y[48] = 0.6, -0.3
    indices = peak_sample_indices(x, y)
    assert indices.tolist() == [0, 29, 30, 47, 48, 59, 60, 89, 90, 119]
    assert (y[indices].min(), y[indices].max()) == (-0.3, 0.6)


def test_logarithmic_ticks_and_zoomed_drift_labels_stay_distinct():
    assert axis_ticks(1, 4, 7, logarithmic=True) == ((1, "10"), (2, "100"), (3, "1,000"), (4, "10,000"))
    labels = [label for _value, label in axis_ticks(3150.012, 3150.018, 5)]
    assert len(labels) >= 3
    assert len(labels) == len(set(labels))


def test_ticks_handle_unrepresentable_spans_and_subnormal_steps():
    assert axis_ticks(0, 0, 5) == ()
    assert axis_ticks(-1e308, 1e308, 5) == ()
    assert axis_ticks(0, 1, 7, step=0) == axis_ticks(0, 1, 7)
    positions, _ = zip(*axis_ticks(0.0, np.nextafter(0, 1), 7), strict=True)
    assert positions == (0.0, np.nextafter(0, 1))
    assert axis_ticks(-400, -395, 7, logarithmic=True) == ()
    assert all(float(label) > 0 for _, label in axis_ticks(-323, -322, 7, logarithmic=True))


def test_trace_contrast_and_heat_scale_follow_presentation_choices():
    for background in ((25, 29, 35), (255, 255, 255)):
        for index in range(5):
            color = PlotStyle(high_contrast=True).trace_rgb("#aaaaaa", index, background)
            assert contrast_ratio(color, background) >= 4.5

    levels = np.array([[-140, -100, -50, 0]])
    both_ends = np.array([0, 255])
    narrow = heat_image(levels, PlotStyle(heat_palette="grayscale", heat_range=50))
    wide = heat_image(levels, PlotStyle(heat_palette="grayscale", heat_range=100))
    np.testing.assert_array_equal(narrow[0, (0, -1), 0], both_ends)
    np.testing.assert_array_equal(wide[0, (0, -1), 0], both_ends)
    assert narrow[0, 2, 0] == 0 < wide[0, 2, 0]
    np.testing.assert_array_equal(levels, [[-140, -100, -50, 0]])


def test_plot_surface_can_follow_or_override_app_appearance():
    automatic = PlotStyle.from_settings({'theme': 'retro'})
    assert automatic.colors(False).panel == (255, 255, 255)
    assert automatic.colors(True).panel != automatic.colors(False).panel
    dark = PlotStyle.from_settings({'colorMode': 'dark'})
    assert dark.colors(False) == dark.colors(True)
    light = PlotStyle.from_settings({'colorMode': 'dark', 'plotCanvas': 'light'})
    assert light.colors(True).panel == (255, 255, 255)
    assert contrast_ratio(light.colors(True).text, light.colors(True).panel) >= 4.5


def test_spectrogram_cells_follow_recorded_centres():
    np.testing.assert_array_equal(cell_edges(np.array([0.0, 2.0, 5.0])), [-1, 1, 3.5, 6.5])
    left, right = cell_edges(np.array([0.0]))
    assert left < 0 < right
