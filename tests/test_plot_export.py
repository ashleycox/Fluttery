from dataclasses import replace
from io import BytesIO
from xml.etree import ElementTree

import numpy as np
import pytest
from PIL import Image

from fluttery.plot_export import (
    PlotCursor,
    PlotImageOptions,
    PlotSnapshot,
    PlotTrace,
    _create_figure,
    render_plot_image,
    write_plot_image,
)
from fluttery.plot_style import DEFAULT_PLOT_STYLE
from fluttery.reporting import GraphData, graph_png


def test_snapshot_freezes_live_arrays_and_keeps_gaps_and_views():
    x = np.array([1, 2, 3, 4, 5, 6], dtype=float)
    y = np.array([0, 0.5, np.nan, 0.7, np.nan, 0.2])
    trace = PlotTrace('Left', x, y, style='dashed')
    snapshot = PlotSnapshot('Response', 'Frequency (Hz)', 'Gain (dB)', (trace,), log_x=True,
                            x_bounds=(2, 5), y_bounds=(0, 1), full_x_bounds=(1, 6), full_y_bounds=(-1, 2))
    x[0] = 100
    y[3] = 50
    options = PlotImageOptions(show_grid=False)
    visible, _ = _create_figure(snapshot, options)
    full, _ = _create_figure(snapshot, replace(options, full_view=True))
    axis = visible.axes[0]
    assert (axis.get_xlim(), axis.get_ylim()) == ((2, 5), (0, 1))
    assert (full.axes[0].get_xlim(), full.axes[0].get_ylim()) == ((1, 6), (-1, 2))
    line, = axis.lines
    assert line.get_xdata()[0] == 1
    assert line.get_ydata()[3] == 0.7
    assert np.isnan(line.get_ydata()[2])
    assert line.get_markevery() == [3, 5]
    assert axis.get_xscale() == 'log'
    with pytest.raises(ValueError, match='WRITEABLE'):
        trace.y.setflags(write=True)


def test_png_dimensions_density_paper_title_and_markers():
    trace = PlotTrace('Left', [1, 2, 3], [2, 4, 3])
    snapshot = PlotSnapshot('Input', 'Time (s)', 'Level (V RMS)', (trace,), cursors=(PlotCursor('A', 2, 4),))
    options = PlotImageOptions(width=490, height=245, dpi=120, background='transparent', title='Bench input')
    with Image.open(BytesIO(render_plot_image(snapshot, options))) as image:
        assert image.size == (490, 245)
        assert image.mode == 'RGBA' and image.getpixel((0, 0))[3] == 0
        assert image.info['dpi'] == pytest.approx((120, 120), abs=0.03)
        assert image.info['Title'] == 'Bench input'
        assert 'A (2, 4)' in image.info['Description']
    options = replace(options, background='dark', show_cursors=False)
    with Image.open(BytesIO(render_plot_image(snapshot, options))) as image:
        assert max(image.getpixel((0, 0))[:3]) < 70 and image.getpixel((0, 0))[3] == 255
        assert 'Markers' not in image.info['Description']


def test_full_range_phase_export_preserves_the_angle_of_the_trace():
    trace = PlotTrace('Stereo', [0, 2], [0, 1])
    snapshot = PlotSnapshot('Phase', 'Left', 'Right', (trace,), square_axes=True,
                            full_x_bounds=(0, 2), full_y_bounds=(0, 1))
    figure, _ = _create_figure(snapshot, PlotImageOptions(full_view=True))
    axis = figure.axes[0]
    assert axis.get_box_aspect() == 1
    assert axis.get_xlim() == (0, 2)
    assert axis.get_ylim() == (-0.5, 1.5)


def test_vector_formats_render_heatmap_and_preserve_existing_file_on_error(tmp_path):
    snapshot = PlotSnapshot('Speed < reference', 'Seconds', 'Frequency (Hz)',
                            heatmap=([0, 1, 5], [4, 12], [[-25, np.nan, -42], [-60, -35, -20]]))
    options = PlotImageOptions(width=900, height=600, dpi=100)
    svg = tmp_path / 'spectrum.SVG'
    write_plot_image(svg, snapshot, options)
    rendered_svg = svg.read_bytes()
    document = ElementTree.fromstring(rendered_svg)
    svg_ns = {'svg': 'http://www.w3.org/2000/svg'}
    assert document.find('svg:title', svg_ns).text == 'Speed < reference'
    assert document.find('.//svg:image', svg_ns) is not None  # Colour cells are a raster layer.
    assert render_plot_image(snapshot, options, format='pdf').startswith(b'%PDF-')
    with pytest.raises(ValueError, match='Choose PNG'):
        write_plot_image(svg, snapshot, options, format='tiff')
    assert svg.read_bytes() == rendered_svg
    assert list(tmp_path.iterdir()) == [svg]


def test_report_graph_keeps_light_paper_and_existing_dimensions():
    graph = GraphData('Level', 'Time (s)', 'Level (dB)', traces=[{'label': 'Left', 'x': [0, 1], 'y': [-1, 2]}])
    settings = replace(DEFAULT_PLOT_STYLE, canvas='dark')
    with Image.open(BytesIO(graph_png(graph, plot_style=settings))) as report_image:
        assert report_image.size == (1148, 406)
        assert min(report_image.getpixel((0, 0))) >= 250


@pytest.mark.parametrize('values', [{'width': 0}, {'height': 1.5}, {'dpi': 1200}, {'width': 8000, 'height': 8000}])
def test_reject_image_dimensions_outside_supported_range(values):
    with pytest.raises(ValueError):
        PlotImageOptions(**values)
