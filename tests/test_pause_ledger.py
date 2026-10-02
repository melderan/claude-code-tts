"""A pause holds the queue: paused time does not age messages, held ones are not trimmed."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.daemon as d
import claude_code_tts.msgqueue as mq
import claude_code_tts.state as st
from claude_code_tts.daemon import (
    PauseLedger,
    cleanup_old_messages,
    enforce_max_depth,
    read_playback_state,
    write_playback_state,
)


class TestPauseLedger:
    def test_no_pauses_holds_nothing(self):
        assert PauseLedger().held_since(0.0, now=100.0) == 0.0

    def test_closed_pause_counts_only_its_overlap(self):
        led = PauseLedger()
        led.mark(True, now=10.0)
        led.mark(False, now=40.0)
        assert led.held_since(0.0, now=100.0) == 30.0
        assert led.held_since(25.0, now=100.0) == 15.0  # arrived mid-pause
        assert led.held_since(50.0, now=100.0) == 0.0  # arrived after it
        assert led.paused is False

    def test_open_pause_counts_up_to_now(self):
        led = PauseLedger()
        led.mark(True, now=10.0)
        led.mark(True, now=20.0)  # repeated marks do not restart the interval
        assert led.paused is True
        assert led.held_since(0.0, now=70.0) == 60.0

    def test_two_pauses_add_up(self):
        led = PauseLedger()
        led.mark(True, now=0.0)
        led.mark(False, now=10.0)
        led.mark(True, now=20.0)
        led.mark(False, now=25.0)
        assert led.held_since(0.0, now=30.0) == 15.0


def enqueue(queue_dir: Path, text: str, age_s: float) -> Path:
    ts = time.time() - age_s
    f = queue_dir / f"{ts}_{abs(hash(text)) % 10**8:08x}.json"
    f.write_text(json.dumps({"session_id": "s", "project": "p", "text": text, "timestamp": ts}))
    return f


@pytest.fixture
def queue_dir(tmp_path):
    q = tmp_path / "queue"
    q.mkdir()
    with patch.object(mq, "QUEUE_DIR", q), patch.object(d, "LOG_FILE", tmp_path / "log"):
        yield q


class TestCleanupWithLedger:
    def test_without_ledger_wall_clock_age_applies(self, queue_dir):
        old = enqueue(queue_dir, "old", age_s=400)
        fresh = enqueue(queue_dir, "fresh", age_s=10)
        assert cleanup_old_messages(300) == 1
        assert not old.exists() and fresh.exists()

    def test_paused_time_is_not_age(self, queue_dir):
        led = PauseLedger()
        led.mark(True, now=time.time() - 390)  # paused for the last 390 s
        old = enqueue(queue_dir, "held", age_s=400)  # 400 s old, 390 s of it paused
        assert cleanup_old_messages(300, led) == 0
        assert old.exists()

    def test_message_older_than_max_age_after_discounting_pause_still_goes(self, queue_dir):
        led = PauseLedger()
        led.mark(True, now=time.time() - 100)
        led.mark(False, now=time.time() - 50)
        old = enqueue(queue_dir, "too old", age_s=400)  # 400 - 50 held = 350 > 300
        assert cleanup_old_messages(300, led) == 1
        assert not old.exists()


class TestDepthWithLedger:
    def test_held_messages_neither_count_nor_get_trimmed(self, queue_dir):
        led = PauseLedger()
        led.mark(True, now=time.time() - 60)
        led.mark(False, now=time.time() - 30)
        held = [enqueue(queue_dir, f"held {i}", age_s=50) for i in range(5)]  # waited through it
        new = [enqueue(queue_dir, f"new {i}", age_s=20 - i) for i in range(4)]  # after resume
        assert enforce_max_depth(2, led) == 2
        assert all(f.exists() for f in held)
        assert sum(f.exists() for f in new) == 2
        assert not new[0].exists() and not new[1].exists()  # oldest of the new go first

    def test_without_ledger_oldest_go(self, queue_dir):
        files = [enqueue(queue_dir, f"m {i}", age_s=10 - i) for i in range(4)]
        assert enforce_max_depth(3) == 1
        assert not files[0].exists()


class TestLoopHoldsQueueWhilePaused:
    """daemon_loop with a stale message and a user pause: the message survives the pause."""

    def test_stale_message_survives_a_pause_and_plays_after(self, tmp_path):
        state_dir = tmp_path / ".claude-tts"
        state_dir.mkdir()
        queue_dir = state_dir / "queue"
        queue_dir.mkdir()
        player = tmp_path / "fake-player"
        player.write_text("#!/bin/bash\nsleep 0.05\n")
        player.chmod(0o755)
        spoken: list[str] = []

        def fake_generate(text, persona, output_file, **kw):
            spoken.append(text)
            import wave

            with wave.open(str(output_file), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(8000)
                w.writeframes(b"\x00\x00" * 800)
            return True

        patches = [
            patch.object(st, "PLAYBACK_STATE_FILE", state_dir / "playback.json"),
            patch.object(st, "HEARTBEAT_FILE", state_dir / "daemon.heartbeat"),
            patch.object(d, "LOG_FILE", state_dir / "daemon.log"),
            patch.object(mq, "QUEUE_DIR", queue_dir),
            patch.object(st, "PID_FILE", state_dir / "daemon.pid"),
            patch.object(st, "LOCK_FILE", state_dir / "daemon.lock"),
            patch.object(st, "VERSION_FILE", state_dir / "daemon.version"),
            patch.object(st, "RESPAWN_MARKER", state_dir / "daemon.respawn"),
            patch.object(d.signal, "signal"),  # the loop runs in a thread here
            patch.object(d, "detect_player", return_value=[str(player)]),
            patch.object(d, "daemon_generate_speech", side_effect=fake_generate),
            patch.object(d, "acquire_lock", return_value=True),
            patch.object(d, "release_lock"),
            patch.object(d, "speak_announcement"),
            patch.object(d, "save_speech_wav", lambda p, **kw: p),
            patch.object(d, "get_http_config", return_value={"enabled": False}),
            patch.object(
                d,
                "get_queue_config",
                return_value={
                    "max_depth": 20,
                    "max_age_seconds": 1,  # one second: anything that waits expires
                    "speaker_transition": "none",
                    "coalesce_rapid_ms": 500,
                    "idle_poll_ms": 20,
                },
            ),
            patch.object(d, "load_raw_config", return_value={}),
        ]
        for p in patches:
            p.start()
        try:
            # Paused 5 s ago (before this daemon started), message arrived 4 s ago: a
            # restart while paused must still hold it, so the pause time comes from
            # the state file's last write.
            (state_dir / "playback.json").write_text(
                json.dumps(
                    {
                        "paused": True,
                        "paused_by": "user",
                        "audio_pid": None,
                        "current_message": None,
                        "updated_at": time.time() - 5,
                    }
                )
            )
            msg = enqueue(queue_dir, "waited through the meeting", age_s=4)

            def run():
                d._shutdown_requested = False
                d._daemon_mode = True
                d.daemon_loop()

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            time.sleep(1.0)  # well past max_age while paused
            assert msg.exists(), "paused queue must not expire"
            assert "holding the queue since the pause" in (state_dir / "daemon.log").read_text()
            write_playback_state(paused=False, paused_by=None)
            for _ in range(100):
                if spoken:
                    break
                time.sleep(0.05)
            d._shutdown_requested = True
            runner.join(timeout=5)
        finally:
            d._shutdown_requested = False
            for p in patches:
                p.stop()
        assert spoken == ["waited through the meeting"]
        assert read_playback_state().get("current_message") is None


def test_set_paused_writes_the_flag_and_a_release_clears_paused_by(tmp_path: Path) -> None:
    """set_paused is the bridge's hold: flag only, no pid, release clears who held it."""
    with patch.object(st, "PLAYBACK_STATE_FILE", tmp_path / "playback.json"):
        d.write_playback_state(audio_pid=4242, current_message={"id": "m1"})
        state = d.set_paused(True, by="user")
        assert state["paused"] is True and state["paused_by"] == "user"
        assert state["audio_pid"] == 4242, "the play loop owns the pid, set_paused leaves it"
        assert state["current_message"] == {"id": "m1"}
        state = d.set_paused(False)
        assert state["paused"] is False and state["paused_by"] is None


def loop_harness(tmp_path, raw_config, player_marker=None):
    """State dir, queue dir, the texts spoken, and the patches that run daemon_loop on them.

    player_marker, if given, is a file the fake player creates when it runs: playback, as
    distinct from synthesis, is what a pause must hold back.
    """
    state_dir = tmp_path / ".claude-tts"
    state_dir.mkdir()
    queue_dir = state_dir / "queue"
    queue_dir.mkdir()
    player = tmp_path / "fake-player"
    marker = f"touch {player_marker}\n" if player_marker else ""
    player.write_text(f"#!/bin/bash\n{marker}sleep 0.05\n")
    player.chmod(0o755)
    spoken: list[str] = []

    def fake_generate(text, persona, output_file, **kw):
        spoken.append(text)
        import wave

        with wave.open(str(output_file), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        return True

    patches = [
        patch.object(st, "PLAYBACK_STATE_FILE", state_dir / "playback.json"),
        patch.object(st, "HEARTBEAT_FILE", state_dir / "daemon.heartbeat"),
        patch.object(d, "LOG_FILE", state_dir / "daemon.log"),
        patch.object(mq, "QUEUE_DIR", queue_dir),
        patch.object(st, "PID_FILE", state_dir / "daemon.pid"),
        patch.object(st, "LOCK_FILE", state_dir / "daemon.lock"),
        patch.object(st, "VERSION_FILE", state_dir / "daemon.version"),
        patch.object(st, "RESPAWN_MARKER", state_dir / "daemon.respawn"),
        patch.object(d.signal, "signal"),
        patch.object(d, "detect_player", return_value=[str(player)]),
        patch.object(d, "daemon_generate_speech", side_effect=fake_generate),
        patch.object(d, "acquire_lock", return_value=True),
        patch.object(d, "release_lock"),
        patch.object(d, "speak_announcement"),
        patch.object(d, "save_speech_wav", lambda p, **kw: p),
        patch.object(d, "get_http_config", return_value={"enabled": False}),
        patch.object(
            d,
            "get_queue_config",
            return_value={
                "max_depth": 20,
                "max_age_seconds": 300,
                "speaker_transition": "none",
                "coalesce_rapid_ms": 500,
                "idle_poll_ms": 20,
            },
        ),
        patch.object(d, "load_raw_config", return_value=raw_config),
    ]
    return state_dir, queue_dir, spoken, patches



class TestMicHoldLimit:
    """A mic pause the watcher never releases is released by the loop after mic_pause_max_s.

    2026-09-30 08:12Z: Handy logged a recording start and no stop (a start that failed), the
    watcher paused the queue, and nothing spoke for six minutes until a restart, which then
    dropped 17 messages as stale. The limit is the second line of defence behind the watcher
    matching Handy's start-failure lines; a person's pause is never limited.
    """

    def test_open_for_measures_only_the_current_pause(self):
        led = PauseLedger()
        assert led.open_for(now=10.0) == 0.0
        led.mark(True, now=10.0)
        assert led.open_for(now=25.0) == 15.0
        led.mark(False, now=30.0)
        assert led.open_for(now=40.0) == 0.0

    @pytest.mark.parametrize(
        ("state", "held", "max_s", "expect"),
        [
            ({"paused": True, "paused_by": "mic"}, 200.0, 180.0, True),
            ({"paused": True, "paused_by": "mic"}, 100.0, 180.0, False),
            ({"paused": True, "paused_by": "user"}, 1000.0, 180.0, False),
            ({"paused": True, "paused_by": "mic"}, 1000.0, 0.0, False),
            ({"paused": False, "paused_by": None}, 1000.0, 180.0, False),
        ],
    )
    def test_mic_hold_expired(self, state, held, max_s, expect):
        assert d.mic_hold_expired(state, held, max_s) is expect

    def test_default_cap_outlasts_a_long_dictation(self):
        """Every cap release in the log to 2026-10-02 (five) was a real dictation; the longest was 14 min
        and a 10 min 20 s one was cut 20 s early at 600. The saved recording now ends the hold; the cap
        is the last resort and must outlast any dictation seen."""
        assert d.MIC_PAUSE_MAX_S >= 1800

    @pytest.mark.parametrize(
        ("held", "every", "expect"),
        [
            (0.0, 60.0, 0),
            (59.9, 60.0, 0),
            (60.0, 60.0, 1),
            (185.0, 60.0, 3),
            (185.0, 0.0, 0),
            (-5.0, 60.0, 0),
        ],
    )
    def test_hold_notices_due(self, held, every, expect):
        assert d.hold_notices_due(held, every) == expect

    def _loop_harness(self, tmp_path, raw_config):
        return loop_harness(tmp_path, raw_config)

    def test_loop_releases_a_mic_hold_nobody_ends(self, tmp_path):
        state_dir, queue_dir, spoken, patches = self._loop_harness(tmp_path, {"mic_pause_max_s": 0.3})
        for p in patches:
            p.start()
        try:
            write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)

            def run():
                d._shutdown_requested = False
                d._daemon_mode = True
                d.daemon_loop()

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            # Startup clears a mic pause it finds as stale, so the hold must land after the
            # loop is past reconciliation: the heartbeat is written on every idle pass.
            for _ in range(200):
                if (state_dir / "daemon.heartbeat").exists():
                    break
                time.sleep(0.025)
            assert (state_dir / "daemon.heartbeat").exists(), "loop never reached its idle pass"
            time.sleep(0.1)
            write_playback_state(paused=True, paused_by="mic")  # the watcher's hold, never released
            enqueue(queue_dir, "spoken once the hold expires", age_s=0)
            time.sleep(0.2)
            assert spoken == [], "held: the message must wait while the mic pause is fresh"
            for _ in range(100):
                if spoken:
                    break
                time.sleep(0.05)
        finally:
            stop_loop(runner)
            for p in patches:
                p.stop()
        assert spoken == ["spoken once the hold expires"]
        log = (state_dir / "daemon.log").read_text()
        assert "passed mic_pause_max_s=0" in log and "resuming" in log
        assert json.loads((state_dir / "playback.json").read_text())["paused"] is False

    def test_loop_says_still_paused_every_interval(self, tmp_path):
        """A hold nobody can see is how 60 messages waited two hours on 2026-09-30."""
        state_dir, queue_dir, spoken, patches = self._loop_harness(tmp_path, {})
        patches.append(patch.object(d, "HOLD_NOTICE_EVERY_S", 0.1))
        for p in patches:
            p.start()
        try:
            write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)

            def run():
                d._shutdown_requested = False
                d._daemon_mode = True
                d.daemon_loop()

            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            for _ in range(200):
                if (state_dir / "daemon.heartbeat").exists():
                    break
                time.sleep(0.025)
            assert (state_dir / "daemon.heartbeat").exists(), "loop never reached its idle pass"
            time.sleep(0.1)
            # Pause first, then enqueue: the other order raced the idle poll (20 ms) once.
            write_playback_state(paused=True, paused_by="user")
            time.sleep(0.05)
            enqueue(queue_dir, "waits through the hold", age_s=0)
            time.sleep(0.45)
            assert spoken == [], "held: nothing may play while paused"
            write_playback_state(paused=False, paused_by=None)
            for _ in range(100):
                if spoken:
                    break
                time.sleep(0.05)
        finally:
            stop_loop(runner)
            for p in patches:
                p.stop()
        log = (state_dir / "daemon.log").read_text()
        notices = [ln for ln in log.splitlines() if "Still paused by user for" in ln]
        assert len(notices) >= 2, log
        # The first notice is due 0.1 s into the hold and the enqueue lands 0.05 s in; on a
        # slow runner the enqueue can come second and the first notice says 0 waiting, which
        # is true at that instant (CI, 2026-10-01). The claim is that a notice counts the queue.
        assert any("1 message(s) waiting" in n for n in notices), notices
        assert "Resumed with" in log
        assert spoken == ["waits through the hold"]


def stop_loop(runner: threading.Thread) -> None:
    """Stop a daemon_loop thread, in a finally: a failed assertion must not leave it running.

    2026-10-01: a loop test failed its first assertion, its finally stopped the patches but
    not the loop, and the loop went on polling under the next tests' patches: one message
    synthesized twice in the next test, and the prefetch test four files later lost a message
    to it. Three CI failures from one leaked thread.
    """
    d._shutdown_requested = True
    runner.join(timeout=5)
    d._shutdown_requested = False
    assert not runner.is_alive(), "daemon_loop did not stop within 5 s"


def start_loop() -> threading.Thread:
    def run():
        d._shutdown_requested = False
        d._daemon_mode = True
        d.daemon_loop()

    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    return runner


def wait_for_idle_pass(state_dir) -> None:
    """Startup clears a mic pause it finds as stale, so a hold must land after reconciliation:
    the heartbeat is written on every idle pass."""
    for _ in range(200):
        if (state_dir / "daemon.heartbeat").exists():
            break
        time.sleep(0.025)
    assert (state_dir / "daemon.heartbeat").exists(), "loop never reached its idle pass"
    time.sleep(0.1)


class TestPauseLandsWithTheMessage:
    """The loop checks the pause once per pass, before it lists the queue. A pause and a
    message that arrive together, the watcher's hold and a hook's message in the same
    instant, used to be spoken: 2026-10-01, CI, 0.2 s into a fresh mic pause. The loop
    looks again once the message is picked, and again after synthesis, before any audio.
    """

    def test_a_pause_written_just_before_the_message_holds_it(self, tmp_path):
        state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {})
        real_list = d.get_queue_messages
        armed = {"fire": False}

        def pause_then_enqueue_then_list():
            # Runs inside the pass whose pause check already saw "not paused".
            if armed["fire"]:
                armed["fire"] = False
                write_playback_state(paused=True, paused_by="mic")
                enqueue(queue_dir, "arrived with the pause", age_s=0)
            return real_list()

        patches.append(patch.object(d, "get_queue_messages", side_effect=pause_then_enqueue_then_list))
        for p in patches:
            p.start()
        runner = None
        try:
            write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            runner = start_loop()
            wait_for_idle_pass(state_dir)
            armed["fire"] = True
            time.sleep(0.3)
            assert spoken == [], "a message that arrives with the pause waits like any other"
            write_playback_state(paused=False, paused_by=None)
            for _ in range(100):
                if spoken:
                    break
                time.sleep(0.05)
        finally:
            if runner is not None:
                stop_loop(runner)
            for p in patches:
                p.stop()
        assert spoken == ["arrived with the pause"]

    def test_a_pause_during_synthesis_holds_playback(self, tmp_path):
        played = tmp_path / "player-ran"
        state_dir, queue_dir, spoken, patches = loop_harness(tmp_path, {}, player_marker=played)
        for p in patches:
            p.start()
        runner = None
        try:
            write_playback_state(paused=False, paused_by=None, audio_pid=None, current_message=None)
            runner = start_loop()
            wait_for_idle_pass(state_dir)
            real_generate = d.daemon_generate_speech

            def pause_while_synthesizing(text, persona, output_file, **kw):
                ok = real_generate(text, persona, output_file, **kw)
                if len(spoken) == 1:
                    write_playback_state(paused=True, paused_by="user")  # the hotkey, mid-synthesis
                return ok

            with patch.object(d, "daemon_generate_speech", side_effect=pause_while_synthesizing):
                enqueue(queue_dir, "synthesized, then held", age_s=0)
                for _ in range(100):
                    if spoken:
                        break
                    time.sleep(0.05)
                time.sleep(0.3)
                assert spoken == ["synthesized, then held"]
                assert not played.exists(), "no audio may start once the pause has landed"
                log = (state_dir / "daemon.log").read_text()
                assert "Paused before playback" in log, log
                write_playback_state(paused=False, paused_by=None)
                for _ in range(100):
                    if played.exists():
                        break
                    time.sleep(0.05)
        finally:
            if runner is not None:
                stop_loop(runner)
            for p in patches:
                p.stop()
        assert played.exists(), "the held message plays on resume"
        assert spoken == ["synthesized, then held"] * 2, "resumed as the interrupted message, synthesized again"
