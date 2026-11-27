"""Legacy session migration and self-contained report regression tests."""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

import numpy as np
import pytest

from fluttery import sessions
from fluttery.preferences import DEFAULTS
from fluttery.reporting import (
    build_report,
    comparison_statistics,
    csv_report,
    distortion_history,
    distortion_results,
    history_graph,
    html_report,
    json_report,
    measurement_graph,
    measurement_summary,
    measurement_table,
    pdf_report,
    report_filename,
    report_sections,
    session_statistics,
    spectrogram_graph,
    spectrum_graph,
    write_export,
)
from fluttery.sessions import load_session, new_session, save_session


def test_reports_preserve_imd_noise_bounds_and_stereo_input_failures():
    measurement = {'type': 'bench-imd', 'data': {'method': 'smpte', 'data': [
        {'imdPercent': .0004, 'imdLimited': True, 'imdLimitPercent': .0012, 'products': [
            {'product': 'Upper', 'frequencyHz': 7060, 'levelDbc': -122, 'limited': True},
        ]},
        {'error': 'No valid reference tones.', 'products': []},
    ]}}
    assert 'at most 0.0012%' in measurement_summary(measurement)
    assert 'Right unavailable (No valid reference tones.)' in measurement_summary(measurement)
    _, rows = measurement_table(measurement)
    assert rows[0][4] == '≤0.0012'
    assert rows[1][-1] == 'Unavailable (No valid reference tones.)'
    trace, = measurement_graph(measurement).traces
    assert 'near noise floor' in trace['label']


def test_reports_preserve_only_in_band_harmonics_and_two_tone_azimuth():
    sweep = {'type': 'bench-distortion', 'data': {'axis': 'frequencyHz', 'data': [
        {'channel': 1, 'frequencyHz': 12000, 'thd': None, 'thdn': .015, 'hd3': None},
    ]}}
    assert '1 of 1' in measurement_summary(sweep)
    assert measurement_table(sweep)[1][0][-1] == 'THD+N only'
    azimuth = {'type': 'alignment', 'data': {'title': 'Tape Head Azimuth', 'trials': [
        {'step': 'Verify', 'metric': 'Delay (µs)', 'left': 127.3, 'right': 129.2, 'delay_margin_us': .18,
         'verified_delay_us': 127.25, 'verification_fine_hz': 9997.3},
    ]}}
    assert '1 captured reading. Latest verified right-channel delay +127.250 µs' in measurement_summary(azimuth)
    headers, rows = measurement_table(azimuth)
    assert headers[-2:] == ('Verified delay (µs)', 'Fine tone (Hz)')
    assert rows[0][-2:] == ('127.25', '9997.3')


def _legacy_session():
    return {
        "version": "1.0",
        "timestamp": 1700000000000,
        "history": [
            {"timestamp": 1700000000, "peak": 0.05, "bufferPeak": 0.07, "rms": 0.02, "currentFreq": 3000,
             "spectrum": [0, 240, 5]},
            {"timestamp": 1700000001.25, "peak": 0.06, "bufferPeak": 0.08, "rms": 0.03, "currentFreq": 3001,
             "spectrum": [6, 230, 11]},
        ],
        "measurementLog": [
            {"timestamp": 1700000002, "type": "thd", "data": [
                {"timestamp": 1700000002, "thd": 0.14, "thdOnly": 0.06, "fundamental": 1000},
            ], "stats": {"avg": 0.14, "max": 0.14}},
        ],
        "settings": {"technician": "Legacy account name", "targetFreq": 3000},
        "metadata": {"technician": "Previously saved name", "equipment": "Reference turntable", "notes": "Bench A"},
    }


def test_load_legacy_session_and_save_compatible_copy(tmp_path):
    old = tmp_path / "original.fluttery"
    old.write_text(json.dumps(_legacy_session()), encoding="utf-8")
    session = load_session(old)
    assert session["metadata"] == {
        "technician": "Previously saved name",
        "equipment": "Reference turntable",
        "notes": "Bench A",
    }
    assert session["history"][0]["spectrum"] == [0, 240, 5]
    assert session["measurementLog"][0]["data"][0]["thdOnly"] == 0.06

    output = tmp_path / "converted.fluttery"
    save_session(output, session)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["sessionLog"] == payload["history"]
    assert payload["metadata"]["technician"] == "Previously saved name"
    reopened = load_session(output)
    assert "sessionLog" not in reopened
    assert reopened["metadata"]["technician"] == "Previously saved name"


def test_sessionlog_wins_and_report_options_restore(tmp_path):
    old = _legacy_session()
    old["sessionLog"] = []
    old["settings"] = {"paperSize": "LETTER", "filenameTemplate": "foo_{technician}_{date}"}
    file = tmp_path / "export.json"
    file.write_text(json.dumps(old), encoding="utf-8")
    session = load_session(file)
    assert session["history"] == []
    assert session["settings"]["exportSettings"]["paperSize"] == "LETTER"
    assert session["settings"]["exportSettings"]["filenameTemplate"] == "foo_{technician}_{date}"

    file.write_text('{"unrelated": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a Fluttery"):
        load_session(file)


def test_dense_session_can_reopen_and_an_oversized_save_preserves_the_file(monkeypatch, tmp_path):
    session = _legacy_session()
    session["history"][0]["spectrum"] = [-60.12345678901234] * 128
    session["history"][0]["spectrumEncoding"] = "db"
    path = tmp_path / "dense.fluttery"
    monkeypatch.setattr(sessions, "MAX_SESSION_BYTES", 7000)
    assert len(json.dumps(sessions.session_payload(session), indent=2).encode()) > sessions.MAX_SESSION_BYTES
    save_session(path, session)
    original = path.read_bytes()
    assert load_session(path)["history"][0]["spectrum"] == session["history"][0]["spectrum"]

    session["history"] *= 2
    with pytest.raises(ValueError, match="too large to save"):
        save_session(path, session)
    assert path.read_bytes() == original


def test_native_and_legacy_spectra_retain_dc_and_use_separate_scales():
    old = _legacy_session()["history"]
    legacy = spectrum_graph(old)
    assert legacy.title.startswith("Flutter spectrum (FFT) — legacy scale")
    assert "48 kHz assumed" in legacy.title
    np.testing.assert_allclose(legacy.traces[0]["x"], [0, 11.71875, 23.4375])
    np.testing.assert_allclose(legacy.traces[0]["y"], -100 + np.asarray([6, 230, 11]) * (70 / 255))
    assert not legacy.log_x

    native = {"timestamp": 1700000004, "spectrum": [-80, -20, -90], "spectrumEncoding": "db", "spectrumBinHz": 2.0}
    graph = spectrum_graph([*old, native])
    np.testing.assert_allclose(graph.traces[0]["x"], [0, 2, 4])
    np.testing.assert_allclose(graph.traces[0]["y"], [-80, -20, -90])
    assert spectrogram_graph(old).title.startswith("Flutter spectrogram — legacy scale")
    time, frequency, values = spectrogram_graph([*old, native]).heatmap
    np.testing.assert_allclose(time, [4])
    np.testing.assert_allclose(frequency, [0, 2, 4])
    np.testing.assert_allclose(values[:, 0], [-80, -20, -90])


def test_comparison_uses_relative_sample_times_and_raw_peaks():
    legacy = _legacy_session()["history"]
    offset = [{**sample, "timestamp": sample["timestamp"] + 3600} for sample in legacy]
    graph = history_graph("graph", legacy, "rawBlockPeak", offset, "Baseline")
    assert graph.traces[1]["label"] == "Baseline"
    np.testing.assert_allclose(graph.traces[0]["x"], [0, 1.25])
    np.testing.assert_allclose(graph.traces[1]["x"], [0, 1.25])
    np.testing.assert_allclose(graph.traces[0]["y"], [0.07, 0.08])


def test_comparison_exposes_peak_speed_and_sample_differences():
    current = _legacy_session()
    baseline = _legacy_session()
    baseline["history"] = [{**baseline["history"][0], "bufferPeak": 0.01, "currentFreq": 2999}]
    rows = comparison_statistics(current["history"], baseline["history"], "rawBlockPeak")
    assert rows == [
        ("Maximum raw block peak", "0.0800 %", "0.0100 %", "+0.0700 percentage points"),
        ("Average frequency", "3000.50 Hz", "2999.00 Hz", "+1.50 Hz"),
        ("Samples", "2", "1", "+1"),
    ]
    assert comparison_statistics([], baseline["history"], "rawBlockPeak")[0][1] == "Unavailable"
    report = build_report(current, {**DEFAULTS, "graphSource": "rawBlockPeak"}, comparison=baseline,
                          comparison_name="Reference")
    sections = {name: content for _, name, content in report_sections(report)}
    assert sections["Session comparison"][1] == rows
    assert "Comparison session — Reference" in sections


def test_session_statistics_exclude_unlocks_and_report_frequency_variation():
    steady, second = _legacy_session()["history"]
    unlocked = {"locked": False, "peak": 90, "rms": 0, "currentFreq": 2000}
    samples = [{**steady, "locked": True}, unlocked, second]
    rows = dict(session_statistics(samples, "detectorPeak"))
    assert rows["Samples"] == "3" and rows["Unlocked samples excluded"] == "1"
    assert rows["Minimum detector peak"] == "0.0500 %"
    assert rows["Maximum detector peak"] == "0.0600 %"
    assert rows["Average RMS"] == "0.0250 %"
    assert rows["Average frequency"] == "3000.50 Hz"
    assert rows["Frequency standard deviation"] == "0.5000 Hz (166.6 ppm)"
    comparison = comparison_statistics(samples, [unlocked], "detectorPeak")
    assert comparison[0][1:] == ("0.0600 %", "Unavailable", "Unavailable")
    assert comparison[1][1:] == ("3000.50 Hz", "Unavailable", "Unavailable")


def test_report_minimum_respects_the_selected_peak_source():
    session = _legacy_session()
    report = build_report(session, {**DEFAULTS, "graphSource": "rawBlockPeak"})
    statistics = dict(next(content[1] for _, title, content in report_sections(report)
                           if title == "Session statistics"))
    lowest = min(row["bufferPeak"] for row in session["history"])
    assert statistics["Minimum raw block peak"] == f"{lowest:.4f} %"


@pytest.mark.parametrize(("value", "reading"), (
    (.7, "0.70 dB toward the left channel"),
    (-.42, "0.42 dB toward the right channel"),
    (0, "centred (0.00 dB)"),
))
def test_recorded_balance_states_the_louder_channel(value, reading):
    entry = {"type": "balance", "data": {"balance": value}}
    assert measurement_summary(entry) == f"Balance {reading}."
    entry["stats"] = {"avg": value}
    assert measurement_summary(entry) == f"Average balance {reading}."


def test_distortion_results_combine_confirmed_readings_and_preserve_sample_times():
    entries = [
        {"type": "tracking", "timestamp": 1700000005, "data": [
            {"timestamp": 1700000004.25, "thd": 0.54, "rightThdn": 0.35, "fundamental": 1001},
            {"timestamp": 1700000003, "thd": 0.30, "thdOnly": 0.09},
        ]},
        {"type": "tracking", "timestamp": 1700000002, "data": [5, 146, 12]},
        {"type": "thd", "timestamp": 1700000001, "data": {"thd": "0.15", "fundamental": 1000, "hd3": .01}},
    ]
    points = distortion_history(entries)
    text, graph, rows = distortion_results(points)
    assert [point[0] for point in points] == [1700000001, 1700000003, 1700000004.25]
    assert "Average 0.330 %. Minimum 0.150 %. Peak 0.540 %." in text
    assert rows[0][1:] == ("Distortion analyzer", "0.150", "—", "—", "1000.000", "0.010")
    assert rows[-1][0].endswith(".250")
    np.testing.assert_allclose(graph.traces[0]["x"], [0, 2, 3.25])
    np.testing.assert_allclose(graph.traces[1]["y"], [0.09])
    np.testing.assert_allclose(graph.traces[2]["y"], [0.35])
    np.testing.assert_allclose(graph.traces[3]["y"], [.01])


def test_distortion_results_keep_invalid_samples_as_breaks():
    entries = [{"type": "tracking", "timestamp": 1700000000, "data": [
        {"timestamp": 1700000000, "thd": .3, "hd3": .1},
        {"timestamp": 1700000001, "thd": 50, "clipped": True, "hd3": 10},
        {"timestamp": 1700000002, "thd": .5, "hd3": .2},
    ]}]
    points = distortion_history(entries)
    summary, graph, rows = distortion_results(points)
    assert "2 THD+N readings" in summary and "Peak 0.500 %" in summary
    assert rows[1][2:] == ("—",) * 5
    np.testing.assert_allclose(graph.traces[0]["y"], [.3, np.nan, .5])
    np.testing.assert_allclose(graph.traces[1]["y"], [.1, np.nan, .2])
    text, empty, _ = distortion_results([points[1]])
    assert text == "No usable THD+N readings." and not empty.traces


def test_right_only_tracking_is_available_through_results_and_reports():
    entry = {"type": "tracking", "timestamp": 1700000000,
             "stats": {"rightAvg": .25, "rightMax": .3},
             "data": [{"rightThdn": .25, "rightThdOnly": .1, "rightFundamental": 3150}]}
    assert measurement_summary(entry) == 'Right average THD+N 0.250 %. Peak 0.300 %.'
    headers, rows = measurement_table(entry)
    assert headers[1:] == ("Right THD+N %", "Right THD %", "Right fundamental Hz")
    assert rows[0][1:] == ("0.250", "0.100", "3150.000")
    graph = measurement_graph(entry)
    assert [trace["label"] for trace in graph.traces] == ["Right THD+N (%)", "Right THD (%)"]
    combined, graph, rows = distortion_results(distortion_history([entry]))
    assert '1 THD+N reading from the right tracking channel.' in combined
    assert rows[0][2:5] == ("—", "—", "0.250")
    assert [trace["label"] for trace in graph.traces] == ["Right tracking THD+N"]
    session = new_session()
    session["measurementLog"].append(entry)
    assert b"Right average THD+N 0.250 %" in csv_report(build_report(session, DEFAULTS))


def test_legacy_nonfinite_values_and_optional_run_fields_reopen_and_save(tmp_path):
    source = tmp_path / 'legacy.json'
    source.write_text('{"settings":{"custom":1e400},"sessionLog":[{"peak":Infinity,"rms":0}],"runSettings":"invalid",'
                      '"signalLoss":123,"measurementLog":[{"type":"thd","data":{"thd":NaN}}]}', encoding='utf-8')
    session = load_session(source)
    assert session['history'][0]['peak'] is None and session['history'][0]['rms'] == 0
    assert session['settings']['custom'] is None and session['measurementLog'][0]['data']['thd'] is None
    assert session['runSettings'] == {} and session['signalLoss'] == []
    destination = tmp_path / 'saved.fluttery'
    save_session(destination, session)
    assert load_session(destination)['history'] == session['history']


def test_damaged_compressed_manifest_is_reported_as_a_document_error(tmp_path):
    path = tmp_path / 'broken.fluttery'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('session.json', '{"version":"2.0"}')
    data = bytearray(path.read_bytes())
    data[30 + len('session.json')] = 0xff  # Invalidate the first DEFLATE block.
    path.write_bytes(data)
    with pytest.raises(ValueError, match='incomplete or damaged'):
        load_session(path)


def test_report_json_reopens_with_recorded_setup_and_respects_metadata_setting(tmp_path):
    source = new_session()
    source.update(settings={"targetFreq": 3000}, runSettings={"targetFreq": 3150, "technician": "Casey"},
                  recordedAudio={"file": "private.wav", "channel": "left"},
                  runRetention={"keepCompleteHistory": True})
    options = {"exportSettings": {"includeMetadata": False}, "graphSource": "rawBlockPeak"}
    report = build_report(source, options)
    path = tmp_path / 'report.json'
    path.write_bytes(json_report(report))
    reopened = load_session(path)
    assert reopened['settings']['targetFreq'] == 3150
    assert reopened['settings']['graphSource'] == 'rawBlockPeak'
    assert reopened['settings']['exportSettings']['includeMetadata'] is False
    assert reopened['recordedAudio'] == {'channel': 'left'}
    assert reopened['runRetention'] == {'keepCompleteHistory': True}
    assert 'technician' not in reopened['settings']


def test_live_results_accept_software_loopback_spectrum_without_physical_input():
    from fluttery.results import ResultsPage

    snapshot = {"health": {"inputActive": False, "loopbackActive": True},
                "flutter_spectrum": {"db": [-40, -50], "frequencies": [30, 37.5]}}
    assert ResultsPage._capture_spectrum(snapshot)["spectrumBinHz"] == 7.5
    snapshot['health']['loopbackActive'] = False
    assert ResultsPage._capture_spectrum(snapshot) is None


def test_distortion_table_exposes_thd_and_thdn():
    entry = _legacy_session()["measurementLog"][0]
    headers, rows = measurement_table(entry)
    assert headers == ("Elapsed time s", "THD+N %", "THD %", "Fundamental Hz")
    assert rows == [("0.000", "0.140", "0.060", "1000.000")]


def test_csv_exports_legacy_series_summaries_single_points_and_spectrum_extra_data():
    source = _legacy_session()
    samples = [{"timestamp": 1700000008 + index, "rpm": 33.3, "deviation": -0.1} for index in range(1000)]
    point = {"timestamp": 1700000005, "thd": 0.3, "fundamental": 1000}
    bins = [240, 115, 70]
    source["measurementLog"] = [
        {"timestamp": 1700000006, "type": "rpm", "data": samples, "stats": {"avg": 33.3, "min": 33.3, "max": 33.3}},
        {"timestamp": 1700000005, "type": "thd", "data": point},
        {"timestamp": 1700000004, "type": "tracking", "data": bins},
    ]
    report = build_report(source, DEFAULTS)
    rows = {row[1]: row for row in csv.reader(io.StringIO(csv_report(report).decode()))
            if len(row) == 5 and row[1] in {"rpm", "thd", "tracking"}}
    assert rows["rpm"][2:] == ["33.30 RPM", "1000", ""]
    assert rows["thd"][2:4] == ["0.300%", "1"]
    assert json.loads(rows["thd"][-1]) == point
    assert json.loads(rows["tracking"][-1]) == bins
    assert json.loads(json_report(report))["measurementLog"][0]["data"] == samples


def test_flat_electron_report_settings_and_spectra_restore_through_report_options(tmp_path):
    legacy = _legacy_session()
    legacy["sessionLog"] = legacy.pop("history")
    legacy["metadata"].update(exportDate="2023-11-14T22:14:00.000Z", version="1.0")
    legacy["settings"] = {
        "filenameTemplate": "bench_{equipment}_{date}", "paperSize": "LEGAL", "scaleFactor": 1.25,
        "graphSource": "rawBlockPeak", "includeMetadata": False, "includeDataTables": False,
        "includeAccessibility": True, "sessionData": {"fft": False, "stats": False},
        "turntableData": {"thd": False},
    }
    legacy["measurementLog"].append({"timestamp": 1700000003, "type": "resonance", "data": [0, 127, 255]})
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    session = load_session(path)
    report = build_report(session, session["settings"])
    sections = list(report_sections(report))

    assert report.options["paperSize"] == "LEGAL" and report.options["scaleFactor"] == 1.25
    assert report.graph_source == "rawBlockPeak"
    assert "fft" not in report.graphs and {entry["type"] for entry in report.measurements} == {"resonance"}
    assert not any(kind == "table" for kind, _name, _value in sections)
    resonance = measurement_graph(report.measurements[0])
    np.testing.assert_allclose(resonance.traces[0]["x"], [0, 23.4375, 46.875])
    np.testing.assert_allclose(resonance.traces[0]["y"], -100 + np.asarray([0, 127, 255]) * 70 / 255)
    page = html_report(report)
    assert b"Reference turntable" not in page and b"resonance_1" in page
    assert b"Maximum at frequency" in page
    assert pdf_report(report).startswith(b"%PDF-")


def test_exported_values_and_report_bundle(tmp_path):
    source = _legacy_session()
    source["metadata"] = {"technician": "Alex Smith", "equipment": "=5+5", "notes": "A\nB <script>unsafe</script>"}
    settings = {**DEFAULTS, "exportSettings": {"filenameTemplate": "{equipment}_{technician}_{date}"}}
    report = build_report(source, settings)
    rows = list(csv.reader(io.StringIO(csv_report(report).decode())))
    assert ["Equipment", "'=5+5"] in rows
    assert ["Technician", "Alex Smith"] in rows
    assert ["Notes", "A\nB <script>unsafe</script>"] in rows
    assert ["Timestamp", "Detector Peak(%)", "Raw Block Peak(%)", "RMS(%)", "Freq(Hz)"] in rows
    index = rows.index(["Timestamp", "Type", "Avg Value", "Duration", "Extra Data"])
    assert rows[index + 1] == ["2023-11-14T22:13:22.000Z", "thd", "0.14%", "1", ""]
    exported = json.loads(json_report(report))
    assert exported["sessionLog"][0]["peak"] == 0.05
    assert exported["metadata"]["technician"] == "Alex Smith"
    assert exported["settings"]["filenameTemplate"] == "{equipment}_{technician}_{date}"

    path = tmp_path / "fluttery.zip"
    write_export(path, "zip", report)
    with zipfile.ZipFile(path) as bundle:
        assert bundle.testzip() is None
        assert bundle.read("fluttery.pdf").startswith(b"%PDF-")
        assert bundle.read("images/wow_flutter_graph.png").startswith(b"\x89PNG")
        report_html = bundle.read("fluttery.html").decode("utf-8")
        assert "Alex Smith" in report_html
        assert "&lt;script&gt;unsafe&lt;/script&gt;" in report_html
        assert "<script>unsafe</script>" not in report_html
        assert "Explore data points" in report_html
        assert "Explore spectrum levels" in report_html
        assert 'src="data:image/png;base64,' in report_html
        assert 'src="https://' not in report_html


def test_complete_bundle_keeps_graph_images_when_report_sections_are_hidden(tmp_path):
    report = build_report(_legacy_session(), {**DEFAULTS, "exportSettings": {
        "sessionData": {key: False for key in ("graph", "fft", "drift", "spectrogram", "stats")},
    }})
    assert report.graphs == {}
    path = tmp_path / "complete.zip"
    write_export(path, "zip", report)
    with zipfile.ZipFile(path) as bundle:
        for graph in ("wow_flutter_graph", "speed_drift_graph", "spectrum_fft", "flutter_spectrogram"):
            assert bundle.read(f"images/{graph}.png").startswith(b"\x89PNG")
        assert b'id="graph"' not in bundle.read("complete.html")
        assert b'id="spectrogram"' not in bundle.read("complete.html")


def test_empty_report_and_filename_templates():
    report = build_report(new_session(), DEFAULTS)
    report.created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert b"Fluttery measurement report" in html_report(report)
    assert report.date_label.encode() in html_report(report)
    assert json.loads(json_report(report))["metadata"]["exportDate"] == "2026-01-02T03:04:05.000Z"
    assert report.graphs == {}
    metadata = {"equipment": "Turntable/1", "technician": "A. B."}
    name = report_filename("{technician} - {equipment} - {date}:{time}", metadata, now=datetime(2026, 1, 2, 3, 4, 5))
    assert name == "A_B_-_Turntable_1_-_2026-01-02_03-04-05"


def test_pdf_line_plots_remain_vector():
    settings = {**DEFAULTS, "exportSettings": {"sessionData": {"spectrogram": False}}}
    report = build_report(_legacy_session(), settings)
    pdf = pdf_report(report)
    assert pdf.startswith(b"%PDF-")
    # The plot-only report still contains everything needed for vector paths,
    # selectable chart labels and tables without an embedded image XObject.
    assert b"/Subtype /Image" not in pdf


def test_legacy_technician_default_and_opt_out(tmp_path):
    source = _legacy_session()
    source["metadata"].pop("technician")
    path = tmp_path / "legacy.fluttery"
    path.write_text(json.dumps(source), encoding="utf-8")
    session = load_session(path)
    assert session["metadata"]["technician"] == "Legacy account name"
    session["metadata"]["technician"] = ""
    save_session(path, session)
    assert load_session(path)["metadata"]["technician"] == ""

    session["metadata"]["technician"] = "Private name"
    settings = {**DEFAULTS, "exportSettings": {"includeMetadata": False}}
    report = build_report(session, settings)
    assert "technician" not in json.loads(json_report(report))["metadata"]
    assert b"Private name" not in csv_report(report)
    assert b"Private name" not in html_report(report)
    assert "Session details" not in {name for _, name, _ in report_sections(report)}
    assert report_filename("{technician}_{equipment}", session["metadata"], include_metadata=False) == "unknown_unknown"
