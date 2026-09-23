"""claude-tts daemon stats: a digest of the daemon log from a synthetic log."""

from claude_code_tts.daemon import format_log_stats, log_stats

LOG = """\
[2026-09-23 10:00:00] [INFO] Daemon starting...
[2026-09-23 10:00:01] [INFO] Speaking for proj-a: first message text...
[2026-09-23 10:00:02] [INFO] Audio started (PID 100), polling for pause...
[2026-09-23 10:00:10] [INFO] Speaking for proj-a: second message text...
[2026-09-23 10:00:14] [INFO] Audio started (PID 101), polling for pause...
[2026-09-23 10:00:20] [INFO] Speaking for proj-b: third, streamed...
[2026-09-23 10:00:21] [INFO] Sentence stream: first audio, sentence 1/4
[2026-09-23 10:00:21] [INFO] Audio started (PID 102), polling for pause...
[2026-09-23 10:00:23] [INFO] Audio started (PID 103), polling for pause...
[2026-09-23 10:00:24] [INFO] Mic watcher: paused for recording
[2026-09-23 10:00:24] [INFO] Audio killed for pause (PID 103) after 1.0s real time
[2026-09-23 10:00:30] [INFO] Mic watcher: resumed after recording
[2026-09-23 10:00:40] [INFO] Speaking for proj-b: fourth, fails...
[2026-09-23 10:00:40] [ERROR] Failed to generate speech for message from proj-b: boom
[2026-09-23 10:00:50] [INFO] Poll #20: paused=False, pid=104
[2026-09-23 10:00:51] [INFO] Poll #40: paused=False, pid=104
garbage line without a timestamp
"""


class TestLogStats:
    def test_counts_and_latency(self):
        s = log_stats(LOG.splitlines())
        assert s["lines"] == 17
        assert s["first"] == "2026-09-23 10:00:00"
        assert s["last"] == "2026-09-23 10:00:51"
        assert s["messages"] == 4
        assert s["streams"] == 1
        assert s["mic_pauses"] == 1
        assert s["errors"] == 1
        lat = s["latency"]
        # gaps: 1 s, 4 s, 1 s (stream's first audio); the failed message has no audio
        assert lat["n"] == 3
        assert lat["median"] == 1.0
        assert lat["max"] == 4.0

    def test_kinds_collapse_digits_and_rank_by_count(self):
        s = log_stats(LOG.splitlines())
        kinds = {sample: count for count, sample in s["kinds"]}
        assert kinds["Audio started (PID N), polling for pause..."] == 4
        assert kinds["Poll #N: paused=False, pid=N"] == 2
        assert s["kinds"][0][0] >= s["kinds"][-1][0]

    def test_empty_log(self):
        s = log_stats([])
        assert s["lines"] == 0
        assert s["latency"]["n"] == 0
        assert s["first"] is None

    def test_format_is_a_short_report(self):
        text = format_log_stats(log_stats(LOG.splitlines()), "daemon.log", 2048)
        assert "daemon.log: 17 lines, 2.0 KB" in text
        assert "messages spoken: 4" in text
        assert "streamed: 1" in text
        assert "queue to first audio: median 1.0s" in text
        assert "Poll #N" in text
