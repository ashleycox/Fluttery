"""Exercise the shared native plot widget with a desktop session."""

import os

import numpy as np
import pytest
import wx

from fluttery.charts import LinePlot

pytestmark = pytest.mark.skipif(os.getenv('FLUTTERY_GUI_TESTS') != '1', reason='requires an active desktop')


@pytest.fixture
def plot():
    app = wx.GetApp() or wx.App(False)
    frame = wx.Frame(None)
    plot = LinePlot(frame, title='Response', x_label='Frequency (Hz)', y_label='Level (dB)', log_x=True, controls=False)
    plot.canvas.SetSize(wx.Size(750, 400))
    try:
        yield plot
    finally:
        frame.Destroy()
        app.Yield()


def _press(plot, code):
    event = wx.KeyEvent(wx.EVT_KEY_DOWN.typeId)
    event.SetKeyCode(code)
    assert plot.canvas.ProcessWindowEvent(event)


def test_compare_across_traces_with_different_coordinates_and_a_gap(plot):
    plot.set_series([
        {'label': 'Left', 'x': [10, 20, 37, 55, 80], 'y': [-5, -3, np.nan, -1, -2]},
        {'label': 'Right', 'x': [12, 15, 19, 43, 55, 82], 'y': [-8, -6, -5, -4, -3, -2]},
    ])
    _press(plot, wx.WXK_RIGHT)
    _press(plot, wx.WXK_RIGHT)
    _press(plot, ord('A'))
    _press(plot, wx.WXK_UP)
    _press(plot, ord('B'))
    assert plot.readout.GetValue().startswith('B minus A. Frequency (Hz) -1. Level (dB) -2.')
    snapshot = plot.snapshot()
    assert [item['x'] for item in snapshot['markers']] == [20, 19]
    assert np.isnan(snapshot['traces'][0]['y'][2])
    assert plot._series[0].runs == ((0, 2), (2, 4))
    plot._toggle_legend(plot._all_series[1].key, isolate=True)
    assert [(trace['label'], trace['palette_index']) for trace in plot.snapshot()['traces']] == [('Right', 1)]
    _press(plot, wx.WXK_ESCAPE)
    assert not plot.snapshot()['markers']


def test_logarithmic_heatmap_reveals_actual_cells_and_masks_missing_values(plot):
    plot.set_heatmap([10, 100, 1000], [4, 12], [[np.nan, -60, -30], [-80, np.inf, -20]])
    snapshot = plot.snapshot()
    np.testing.assert_allclose(snapshot['full_xlim'], [10 ** .5, 10 ** 3.5])
    assert snapshot['full_ylim'] == (0, 16)
    assert 'Peak -20.0 dB' in plot.readout.GetValue()
    box = plot._plot_rect()
    assert plot._nearest(wx.Point(box.GetRight(), box.y)) == (1, 2)
    assert plot._nearest(wx.Point(box.x, box.GetBottom())) == (0, 0)
    dc = wx.MemoryDC(wx.Bitmap(*plot.canvas.GetClientSize()))
    plot._draw_heatmap(dc, box, *plot._bounds())
    image = plot._bitmap[0].ConvertToImage()
    assert image.HasAlpha()
    assert {0, 255} == set(image.GetAlpha())
    plot.set_view(xlim=(1, 10000))
    assert plot._nearest(wx.Point(box.x, box.GetBottom())) is None
    with pytest.raises(ValueError, match='ascending'):
        plot.set_heatmap([100, 10], [4], [[-50, -40]])


def test_zoom_and_undo_keep_physical_axes_across_live_updates(plot):
    traces = [{'label': 'Left', 'x': [10, 30, 70, 120], 'y': [-10, -5, 2, 0]}]
    plot.set_series(traces, fit=True)
    initial = plot.snapshot()['xlim'], plot.snapshot()['ylim']
    plot._zoom(0.5, axis='x')
    zoomed = plot.snapshot()['xlim'], plot.snapshot()['ylim']
    assert initial[0][0] < zoomed[0][0] < zoomed[0][1] < initial[0][1]
    assert zoomed[1] == initial[1]
    plot.set_series(traces, fit=True)
    assert plot.snapshot()['xlim'] == zoomed[0]
    _press(plot, wx.WXK_BACK)
    assert plot.snapshot()['xlim'] == initial[0]

    plot.set_view(xlim=(2, 3))
    box = plot._plot_rect()
    assert plot._nearest(wx.Point(box.x + box.width // 2, box.y + box.height // 2)) is None
    _press(plot, ord('A'))
    assert not plot.snapshot()['markers']
