"""Which device the Mac plays through, seen from a room (the night watch's ask, 2026-10-06).

The daemon polls macOS's system profiler off the play loop and writes `output_device` into
playback.json; `claude-tts status` prints it as an Output line and the bridge's /pause view
carries it. A room that cannot hear the Mac reads whether the headphones are the default output.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from unittest.mock import patch

import pytest

import claude_code_tts.outputdevice as od
import claude_code_tts.state as st
from claude_code_tts.bridge import _rfc3339, pause_view

SPEAKERS = {
    "_name": "MacBook Pro Speakers",
    "coreaudio_default_audio_input_device": "spaudio_yes",
    "coreaudio_default_audio_output_device": "spaudio_yes",
    "coreaudio_default_audio_system_device": "spaudio_yes",
    "coreaudio_device_transport": "spaudio_builtin",
    "coreaudio_output_source": "spaudio_default",
}
AIRPODS = {
    "_name": "JMO's AirPods Pro",
    "coreaudio_default_audio_output_device": "spaudio_yes",
    "coreaudio_device_transport": "spaudio_bluetooth",
}


def _profile(*items: dict) -> dict:
    return {"SPAudioDataType": [{"_items": list(items), "_name": "coreaudio_device"}]}


class TestParse:
    @pytest.mark.parametrize("raw", ["spaudio_bluetooth", "coreaudio_device_type_bluetooth", "bluetooth"])
    def test_every_known_transport_prefix_goes(self, raw):
        item = dict(AIRPODS, coreaudio_device_transport=raw)
        assert od.parse_default_output(_profile(item))["transport"] == "bluetooth"

    def test_the_flagged_item_is_the_default_output(self):
        speakers = dict(SPEAKERS, coreaudio_default_audio_output_device="spaudio_no")
        assert od.parse_default_output(_profile(speakers, AIRPODS)) == {
            "name": "JMO's AirPods Pro", "transport": "bluetooth",
        }

    def test_builtin_speakers(self):
        assert od.parse_default_output(_profile(SPEAKERS)) == {"name": "MacBook Pro Speakers", "transport": "builtin"}

    def test_transport_missing_is_empty(self):
        item = {"_name": "USB DAC", "coreaudio_default_audio_output_device": "spaudio_yes"}
        assert od.parse_default_output(_profile(item)) == {"name": "USB DAC", "transport": ""}

    @pytest.mark.parametrize(
        "data",
        [None, "text", [], {}, {"SPAudioDataType": {}}, {"SPAudioDataType": [{"_items": "x"}]},
         _profile(dict(SPEAKERS, coreaudio_default_audio_output_device="spaudio_no")),
         _profile({"coreaudio_default_audio_output_device": "spaudio_yes"}),
         _profile({"_name": "   ", "coreaudio_default_audio_output_device": "spaudio_yes"})],
    )
    def test_no_default_or_a_bad_shape_is_none(self, data):
        assert od.parse_default_output(data) is None


class TestProbe:
    def test_only_macos_asks(self, monkeypatch):
        monkeypatch.setattr(od, "detect_platform", lambda: "linux")
        with patch.object(od.subprocess, "run") as run:
            assert od.current_output_device() is None
        run.assert_not_called()

    def test_macos_runs_the_profiler_by_absolute_path_and_parses(self, monkeypatch):
        """The launchd service's PATH has no /usr/sbin: 9.49.0 asked by name and never found it."""
        monkeypatch.setattr(od, "detect_platform", lambda: "macos")
        monkeypatch.setattr(od.Path, "exists", lambda self: str(self) == od.PROFILER)
        done = subprocess.CompletedProcess([od.PROFILER], 0, stdout=json.dumps(_profile(AIRPODS)), stderr="")
        with patch.object(od.subprocess, "run", return_value=done) as run:
            assert od.current_output_device() == {"name": "JMO's AirPods Pro", "transport": "bluetooth"}
        assert run.call_args.args[0] == ["/usr/sbin/system_profiler", "SPAudioDataType", "-json"]
        assert od.last_error() == ""

    def test_without_the_absolute_path_the_name_on_path_is_used(self, monkeypatch):
        monkeypatch.setattr(od, "detect_platform", lambda: "macos")
        monkeypatch.setattr(od.Path, "exists", lambda self: False)
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(_profile(SPEAKERS)), stderr="")
        with patch.object(od.subprocess, "run", return_value=done) as run, patch("shutil.which", lambda n: "/opt/x/system_profiler"):
            assert od.current_output_device()["name"] == "MacBook Pro Speakers"
        assert run.call_args.args[0][0] == "/opt/x/system_profiler"

    def test_a_blank_answer_says_why(self, monkeypatch):
        monkeypatch.setattr(od, "detect_platform", lambda: "macos")
        with patch.object(od.subprocess, "run", side_effect=FileNotFoundError(2, "No such file")):
            assert od.current_output_device() is None
        assert "did not run" in od.last_error()
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(_profile(dict(SPEAKERS, coreaudio_default_audio_output_device="spaudio_no"))), stderr="")
        with patch.object(od.subprocess, "run", return_value=done):
            assert od.current_output_device() is None
        assert od.last_error() == "no item is flagged as the default output"

    @pytest.mark.parametrize(
        "outcome",
        [subprocess.CompletedProcess([], 1, stdout="", stderr="no"),
         subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
         subprocess.CompletedProcess([], 0, stdout="", stderr=""),
         subprocess.TimeoutExpired("system_profiler", 20), OSError("gone")],
    )
    def test_a_failed_profiler_is_none(self, monkeypatch, outcome):
        monkeypatch.setattr(od, "detect_platform", lambda: "macos")
        kw = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
        with patch.object(od.subprocess, "run", **kw):
            assert od.current_output_device() is None


class TestDescribe:
    def test_name_transport_and_age(self):
        assert od.describe({"name": "AirPods", "transport": "bluetooth", "checked_at": 90.0}, now=100.0) == (
            "AirPods (bluetooth), checked 10s ago"
        )

    def test_without_transport_or_stamp(self):
        assert od.describe({"name": "USB DAC", "transport": ""}) == "USB DAC"
        assert od.describe(None) == "unknown"
        assert od.describe({}) == "unknown"


@pytest.fixture
def device_file(tmp_path, monkeypatch):
    f = tmp_path / "output-device.json"
    monkeypatch.setattr(st, "OUTPUT_DEVICE_FILE", f)
    return f


class TestDeviceFile:
    def test_written_with_a_stamp_and_removed_by_none(self, device_file):
        st.write_output_device({"name": "AirPods", "transport": "bluetooth"})
        dev = st.read_output_device()
        assert dev["name"] == "AirPods" and dev["transport"] == "bluetooth" and dev["checked_at"] > 0
        st.write_output_device(None)
        assert not device_file.exists() and st.read_output_device() is None

    def test_a_nameless_device_removes(self, device_file):
        st.write_output_device({"name": "x"})
        st.write_output_device({"transport": "usb"})
        assert st.read_output_device() is None

    def test_a_bad_file_reads_as_none(self, device_file):
        device_file.write_text("not json")
        assert st.read_output_device() is None
        device_file.write_text('{"transport": "usb"}')
        assert st.read_output_device() is None

    def test_playback_json_is_not_where_it_lives(self, tmp_path, monkeypatch, device_file):
        monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
        st.write_output_device({"name": "AirPods", "transport": "bluetooth"})
        st.write_playback_state(paused=True)
        assert "output_device" not in st.read_playback_state()


class TestWatch:
    def test_elsewhere_nothing_starts(self):
        w = od.OutputDeviceWatch(lambda m, lvl="INFO": None, lambda d: None, platform=lambda: "linux")
        assert w.start() is False

    def test_poll_logs_changes_once_writes_answers_and_keeps_the_last_on_a_blank(self, monkeypatch):
        monkeypatch.setattr(od, "_last_error", "")
        lines: list[tuple[str, str]] = []
        writes: list[dict | None] = []
        answers = iter([
            {"name": "Speakers", "transport": "builtin"},
            {"name": "Speakers", "transport": "builtin"},
            {"name": "AirPods", "transport": "bluetooth"},
            None,
        ])
        w = od.OutputDeviceWatch(
            lambda m, lvl="INFO": lines.append((lvl, m)), writes.append,
            probe=lambda: next(answers), platform=lambda: "macos",
        )
        for _ in range(4):
            w.poll_once()
        assert [m for _lvl, m in lines] == [
            "Output device: Speakers (builtin)",
            "Output device: AirPods (bluetooth)",
            "Output device: unknown this round (the probe answered nothing); the last one seen stands",
        ]
        assert lines[-1][0] == "WARN"
        assert writes == [
            {"name": "Speakers", "transport": "builtin"}, {"name": "Speakers", "transport": "builtin"},
            {"name": "AirPods", "transport": "bluetooth"},
        ]

    def test_an_unknown_first_answer_is_logged_and_nothing_written(self):
        lines: list[str] = []
        writes: list[dict | None] = []
        w = od.OutputDeviceWatch(lambda m, lvl="INFO": lines.append(m), writes.append, probe=lambda: None, platform=lambda: "macos")
        w.poll_once()
        assert len(lines) == 1 and lines[0].startswith("Output device: unknown this round")
        assert writes == []

    def test_a_probe_that_outlived_stop_writes_nothing(self):
        writes: list[dict | None] = []
        w = od.OutputDeviceWatch(lambda m, lvl="INFO": None, writes.append, probe=lambda: {"name": "Speakers", "transport": "builtin"}, platform=lambda: "macos")
        w.stop()  # never started; the flag alone is what poll_once consults
        w.poll_once()
        assert writes == []

    def test_the_thread_polls_and_stops(self):
        polled = threading.Event()
        w = od.OutputDeviceWatch(
            lambda m, lvl="INFO": None, lambda d: polled.set(), poll_every_s=0.05,
            probe=lambda: {"name": "Speakers", "transport": "builtin"}, platform=lambda: "macos",
        )
        assert w.start() is True
        assert polled.wait(2.0)
        w.stop()
        assert w._thread is not None and not w._thread.is_alive()

    def test_a_probe_error_does_not_end_the_thread(self):
        lines: list[str] = []
        calls = {"n": 0}

        def probe() -> dict | None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return {"name": "Speakers", "transport": "builtin"}

        w = od.OutputDeviceWatch(lambda m, lvl="INFO": lines.append(m), lambda d: None, poll_every_s=0.02, probe=probe, platform=lambda: "macos")
        w.start()
        pause = threading.Event()
        for _ in range(100):
            if calls["n"] >= 2:
                break
            pause.wait(0.02)
        w.stop()
        assert calls["n"] >= 2
        assert any(m.startswith("Output device probe failed: boom") for m in lines)


class TestReaders:
    def test_status_prints_an_output_line(self, device_file, tmp_path, monkeypatch, capsys):
        from claude_code_tts import cli

        monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
        st.write_output_device({"name": "JMO's AirPods Pro", "transport": "bluetooth"})
        with patch.object(cli, "get_session_id", lambda: "x"):
            try:
                cli.cmd_status(argparse.Namespace())
            except Exception:  # noqa: BLE001  config may be missing in a bare test env; the lines we need print first
                pass
        out = capsys.readouterr().out
        assert "Output:   JMO's AirPods Pro (bluetooth), checked 0s ago" in out

    def test_status_without_the_file_prints_no_output_line(self, device_file, tmp_path, monkeypatch, capsys):
        from claude_code_tts import cli

        monkeypatch.setattr(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json")
        with patch.object(cli, "get_session_id", lambda: "x"):
            try:
                cli.cmd_status(argparse.Namespace())
            except Exception:  # noqa: BLE001
                pass
        assert "Output:" not in capsys.readouterr().out

    def test_pause_view_carries_the_device_it_is_given(self):
        view = pause_view({"paused": False}, 0.0, {"name": "AirPods", "transport": "bluetooth", "checked_at": 1759800000.0})
        assert view["output_device"] == {"name": "AirPods", "transport": "bluetooth", "checked_at": _rfc3339(1759800000.0)}
        assert pause_view({"paused": False})["output_device"] is None
        assert pause_view({"paused": False}, 0.0, {"transport": "usb"})["output_device"] is None
