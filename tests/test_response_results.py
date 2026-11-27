"""Review raw and calibrated response data saved in a session."""

import numpy as np
import pytest

from fluttery.reporting import (
    has_raw_response_comparison,
    measurement_graph,
    measurement_summary,
    measurement_table,
)
from fluttery.sessions import load_session, new_session, save_session


def test_legacy_response_comparison_keeps_both_traces_and_gaps():
    entry = {"type": "frequency-response", "data": {
        "frequencyHz": [0, 100, 500, 1000, 2000], "profile": "Loopback A",
        "leftDb": [8, 0, None, -1, None], "rawLeftDb": [8, -5, None, -7, -9],
        "rightDb": [8, -1, None, -2, None], "rawRightDb": [-3, -4],
        "leftPhase": [None, 40, None, 60, None], "rawLeftPhase": [None, 78, None, 104, None],
    }}

    assert has_raw_response_comparison(entry)
    assert "Gain was corrected with Loopback A." in measurement_summary(entry)
    assert [trace["label"] for trace in measurement_graph(entry).traces] == ["Left gain (dB)", "Right gain (dB)"]
    graph = measurement_graph(entry, compare_raw=True)
    assert [trace["label"] for trace in graph.traces] == [
        "Corrected left gain (dB)", "Raw left gain (dB)", "Saved right gain (dB)",
    ]
    np.testing.assert_array_equal(graph.traces[0]["x"], [100, 500, 1000, 2000])
    np.testing.assert_array_equal(graph.traces[0]["y"], [0, np.nan, -1, np.nan])
    np.testing.assert_array_equal(graph.traces[1]["y"], [-5, np.nan, -7, -9])
    assert graph.traces[1]["style"] == "dashed"
    headers, rows = measurement_table(entry, compare_raw=True)
    assert headers == ("Frequency (Hz)", "Corrected left gain (dB)", "Raw left gain (dB)", "Right gain (dB)",
                       "Corrected left phase (degrees)", "Raw left phase (degrees)")
    assert rows[0] == ("100", "0", "-5", "-1", "40", "78")
    assert rows[1] == ("500", "—", "—", "—", "—", "—")
    assert rows[-1] == ("2000", "—", "-9", "—", "—", "—")


def test_bypassed_primary_and_corrected_comparison_survive_session_round_trip(tmp_path):
    entry = {"type": "frequency-response", "data": {
        "frequencyHz": [100, 1000], "sampleRate": 48000, "profile": "Loopback A", "correctionApplied": False,
        "mode": "tape_playback", "relative": True,
        "leftDb": [-5, -6], "rawLeftDb": [-5, -6], "correctedLeftDb": [0, -1],
        "rightDb": [-7, -8], "rawRightDb": [-7, -8], "correctedRightDb": [-1, -3],
        "leftGroupDelayMs": [7, 10], "rawLeftGroupDelayMs": [7, 10], "correctedLeftGroupDelayMs": [2, 4],
    }}
    session = new_session()
    session["measurementLog"].append(entry)
    path = tmp_path / "response.fluttery"
    save_session(path, session)
    saved, = load_session(path)["measurementLog"]
    assert saved["data"] == entry["data"]

    assert has_raw_response_comparison(saved)
    assert "Fluttery saved the raw response. Compare it with the response corrected using Loopback A." in measurement_summary(saved)
    np.testing.assert_array_equal(measurement_graph(saved).traces[0]["y"], [-5, -6])
    graph = measurement_graph(saved, compare_raw=True)
    assert graph.log_x and graph.y_label == "Relative level (dB)"
    assert [trace["style"] for trace in graph.traces] == ["points"] * 4
    assert len({trace["color"] for trace in graph.traces}) == 4
    for trace, expected in zip(graph.traces, ([0, -1], [-1, -3], [-5, -6], [-7, -8]), strict=True):
        np.testing.assert_array_equal(trace["y"], expected)
    headers, rows = measurement_table(saved, compare_raw=True)
    assert headers[-2:] == ("Corrected left group delay (ms)", "Raw left group delay (ms)")
    assert rows[-1][-2:] == ("4", "10")


@pytest.mark.parametrize("metadata", ({}, {"profile": "Loopback A", "correctionApplied": False}))
def test_comparison_does_not_label_uncompensated_data_as_corrected(metadata):
    entry = {"type": "frequency-response", "data": {
        "frequencyHz": [100, 1000], "leftDb": [-4, -5], "rawLeftDb": [-4, -5], **metadata,
    }}
    assert not has_raw_response_comparison(entry)
    assert "comparison available" not in measurement_summary(entry)
    assert [trace["label"] for trace in measurement_graph(entry, compare_raw=True).traces] == ["Left gain (dB)"]
    assert measurement_table(entry, compare_raw=True) == measurement_table(entry)


def test_partial_import_only_compares_on_valid_shared_frequencies():
    entry = {"type": "frequency-response", "data": {
        "frequencyHz": [0, 100, 1000], "profile": "Loopback A",
        "leftDb": [1, 4, None], "rawLeftDb": [2, None, 5],
        "leftPhase": [10, 30, None], "rawLeftPhase": [20, 50, None],
    }}
    assert has_raw_response_comparison(entry)
    graph = measurement_graph(entry, compare_raw=True)
    assert graph.y_label == "Phase (degrees)"
    assert [trace["label"] for trace in graph.traces] == ["Corrected left phase (degrees)", "Raw left phase (degrees)"]
    assert [trace["style"] for trace in graph.traces] == ["points", "points"]
    np.testing.assert_array_equal(graph.traces[1]["x"], [100])
