"""A new prompt in a room drops that room's queued speech that has not started playing.

A sibling room measured it: a reply still waiting in a deep queue when the next prompt is
typed is heard after the person moved on, and stale speech costs more than silence. The
UserPromptSubmit hook writes a supersede control; the daemon drops every queued message of
that session written before it. The message playing finishes; other rooms, the bridge and
messages without a session are never touched.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import claude_code_tts.audio as audio_mod
import claude_code_tts.cli as cli_mod
import claude_code_tts.daemon as daemon_mod
import claude_code_tts.msgqueue as mq
import tests.test_daemon_integration as _integ
from claude_code_tts.config import TTSConfig
from claude_code_tts.daemon import format_log_stats, log_stats

daemon_env = _integ.daemon_env  # the shared fixture, registered under its own name in this module
make_wav, read_playback_state = _integ.make_wav, _integ.read_playback_state

ROOM_A = "-room-a"
ROOM_B = "-room-b"


def _reply(text: str, session: str | None = ROOM_A, **over) -> Path:
    """A hook message through the one writer, as audio.write_queue_message builds it."""
    fields: dict = {
        "project": text.split()[0], "text": text, "persona": "claude-prime",
        "speed": 2.0, "speed_method": "playback", "voice_kokoro": "", "voice_kokoro_blend": "",
    }
    if session is not None:
        fields["session_id"] = session
    fields.update(over)
    path, _ = mq.write_message(fields)
    time.sleep(0.002)  # distinct timestamps keep the order
    return path


def _supersede(session: str = ROOM_A) -> Path:
    path = mq.write_supersede_message(session, project="proj")
    assert path is not None
    time.sleep(0.002)
    return path


# --- msgqueue.supersede, on the files ---


class TestSupersedeRule:
    @pytest.fixture(autouse=True)
    def _queue(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "queue")
        self.queue_dir = tmp_path / "queue"

    def test_older_reply_and_narration_of_the_room_are_dropped(self):
        reply = _reply("reply of the last turn")
        narration = _reply("narration between tools")
        ctl = _supersede()
        said: list[tuple[str, str]] = []
        kept = mq.supersede(mq.scan(), log=lambda m, level: said.append((level, m)))
        assert kept == []
        assert not reply.exists() and not narration.exists() and not ctl.exists()
        assert [lv for lv, _ in said] == ["INFO", "INFO"]
        assert all(m.startswith("Dropped stale message ") for _, m in said)

    def test_a_prompt_in_one_room_never_drops_another_rooms_reply(self):
        mine = _reply("mine", session=ROOM_A)
        theirs = _reply("theirs", session=ROOM_B)
        _supersede(ROOM_A)
        kept = mq.supersede(mq.scan())
        assert [m["text"] for m in kept] == ["theirs"]
        assert theirs.exists() and not mine.exists()

    def test_a_reply_written_after_the_control_speaks_whatever_order_they_are_seen_in(self):
        """Hook processes race: the control can land first. The message's own time decides."""
        ctl = _supersede()
        later = _reply("later reply")
        kept = mq.supersede(mq.scan())
        assert [m["text"] for m in kept] == ["later reply"]
        assert later.exists() and not ctl.exists()

    def test_the_control_never_reaches_the_pick_and_is_removed(self):
        ctl = _supersede()
        assert mq.supersede(mq.scan()) == []
        assert not ctl.exists() and list(self.queue_dir.iterdir()) == []

    @pytest.mark.parametrize("session", [None, "", 7])
    def test_a_message_without_a_usable_session_is_never_dropped(self, session):
        if session is None:
            path = _reply("no session field", session=None)
        else:
            path = _reply("odd session", session=session)
        _supersede()
        kept = mq.supersede(mq.scan())
        assert len(kept) == 1 and path.exists()

    @pytest.mark.parametrize("ts", [float("nan"), "nan", "soon", True, None])
    def test_a_message_without_a_usable_time_is_never_dropped(self, ts):
        # Handed in directly: scan() itself cannot sort a str or None timestamp today.
        _supersede()
        (control,) = mq.scan()
        path = self.queue_dir / "0.000001_odd.json"
        path.write_text("{}")
        odd = {"id": "odd", "timestamp": ts, "session_id": ROOM_A, "text": "x", "_file": path}
        kept = mq.supersede([odd, control])
        assert [m["id"] for m in kept] == ["odd"] and path.exists()

    def test_bridge_messages_and_other_controls_are_never_dropped(self):
        page = _reply("page block", source="page")
        restart = mq.write_control_message(post_action="reload_config")
        time.sleep(0.002)
        _supersede()
        kept = mq.supersede(mq.scan())
        assert {m["_file"] for m in kept} == {page, restart}

    def test_a_control_without_a_session_drops_nothing_and_says_so(self):
        reply = _reply("survivor")
        bad, _ = mq.write_message({"type": "control", "session_id": "system", "text": "",
                                   "pre_action": mq.SUPERSEDE})
        said: list[str] = []
        kept = mq.supersede(mq.scan(), log=lambda m, level: said.append(level))
        assert [m["_file"] for m in kept] == [reply] and not bad.exists() and said == ["WARN"]

    def test_two_instances_in_one_folder_keep_each_others_replies(self):
        """Without CLAUDE_TTS_SESSION both share the folder's session; Claude Code's id splits them."""
        mine = _reply("mine", claude_session_id="uuid-a")
        theirs = _reply("theirs", claude_session_id="uuid-b")
        old_hook = _reply("older hook")  # no claude_session_id: the folder key decides
        mq.write_supersede_message(ROOM_A, claude_session="uuid-a")
        kept = mq.supersede(mq.scan())
        assert [m["text"] for m in kept] == ["theirs"]
        assert theirs.exists() and not mine.exists() and not old_hook.exists()

    def test_a_control_without_claude_session_covers_the_whole_folder(self):
        _reply("a", claude_session_id="uuid-a")
        _reply("b", claude_session_id="uuid-b")
        _supersede()
        assert mq.supersede(mq.scan()) == []

    def test_a_cutoff_is_remembered_after_its_file_is_gone_and_then_forgotten(self):
        s = mq.Supersedes(keep_s=10.0)
        ctl = _supersede()
        ctl_ts = json.loads(ctl.read_text())["timestamp"]
        assert s.apply(mq.scan(), now=ctl_ts) == [] and not ctl.exists()
        late = _reply("late", timestamp=ctl_ts + 3, hook_started=ctl_ts - 1)
        assert s.apply(mq.scan(), now=ctl_ts + 3) == [] and not late.exists()
        later = _reply("later", timestamp=ctl_ts + 12, hook_started=ctl_ts - 1)
        assert [m["text"] for m in s.apply(mq.scan(), now=ctl_ts + 12)] == ["later"] and later.exists()

    def test_an_unremovable_control_still_applies_and_warns_once(self, tmp_path):
        """A control whose file cannot be unlinked must not stop the pick on every pass."""
        stuck = tmp_path / "stuck.json"
        stuck.mkdir()  # unlink() of a directory raises an OSError that is not FileNotFoundError
        reply = _reply("stale")
        (r,) = mq.scan()
        control = {"id": "c", "type": "control", "pre_action": mq.SUPERSEDE, "supersede_session": ROOM_A,
                   "timestamp": r["timestamp"] + 1, "_file": stuck}
        said: list[str] = []
        s = mq.Supersedes()
        assert s.apply([r, control], log=lambda m, level: said.append(level)) == []
        assert s.apply([control], log=lambda m, level: said.append(level)) == []
        assert not reply.exists() and said.count("WARN") == 1

    def test_the_writer_refuses_sessions_that_are_not_rooms(self):
        for s in ("", "system", "browser"):
            assert mq.write_supersede_message(s) is None
        assert not self.queue_dir.exists() or list(self.queue_dir.iterdir()) == []

    def test_an_older_daemon_reads_it_as_a_silent_control(self):
        """Hooks and daemon differ in version both ways: v1 fields, type control, empty text."""
        msg = json.loads(_supersede().read_text())
        assert msg["type"] == "control" and msg["text"] == "" and msg["v"] == mq.SCHEMA_VERSION
        assert msg["supersede_session"] == ROOM_A and msg["pre_action"] == "supersede"


# --- the daemon loop ---


QUEUE_CONFIG = {
    "max_depth": 20, "max_age_seconds": 300, "speaker_transition": "none",
    "coalesce_rapid_ms": 500, "idle_poll_ms": 50,
}


def _marking_player(tmp: Path, duration: float) -> tuple[Path, Path, Path]:
    """A player that touches started, sleeps, then touches done: playing is observable."""
    started, done = tmp / "started", tmp / "done"
    script = tmp / "marking-player"
    script.write_text(f"#!/bin/bash\ntouch {started}\nsleep {duration}\ntouch {done}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, started, done


def _run(daemon_env, player: Path, stop_when, during=None) -> list[str]:
    """Run the loop until stop_when(); the projects it began to speak, from its log."""
    def fake_generate(text, persona, output_file, **kw):
        make_wav(output_file, 2.0)
        return True

    patches = [
        patch.object(daemon_mod, "detect_player", return_value=[str(player)]),
        patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate),
        patch.object(daemon_mod, "acquire_lock", return_value=True),
        patch.object(daemon_mod, "release_lock"),
        patch.object(daemon_mod, "speak_announcement"),
        patch.object(daemon_mod, "get_queue_config", return_value=dict(QUEUE_CONFIG)),
        patch.object(daemon_mod, "load_raw_config", return_value={}),
        patch("signal.signal"),  # signal.signal fails in non-main threads
    ]
    for pt in patches:
        pt.start()
    try:
        def run_daemon():
            daemon_mod._shutdown_requested = False
            daemon_mod._daemon_mode = True
            daemon_mod.daemon_loop()

        def stopper():
            if during is not None:
                during()
            for _ in range(100):
                time.sleep(0.1)
                if stop_when():
                    break
            time.sleep(0.3)
            daemon_mod._shutdown_requested = True

        st = threading.Thread(target=stopper)
        rn = threading.Thread(target=run_daemon, daemon=True)
        rn.start()
        st.start()
        st.join(timeout=15)
        rn.join(timeout=3)
    finally:
        for pt in patches:
            pt.stop()
    lines = daemon_env["log_file"].read_text().splitlines()
    return [ln.split("Speaking for ", 1)[1].split(" ", 1)[0] for ln in lines if "Speaking for " in ln]


def _empty(queue_dir: Path):
    return lambda: not list(queue_dir.glob("*.json"))


class TestDaemonDropsStaleReplies:
    def test_queued_reply_is_dropped_and_counted(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]
        stale = _reply("stale reply")
        stale_id = json.loads(stale.read_text())["id"]
        _supersede()
        fresh = _reply("fresh reply")
        player, _, _ = _marking_player(daemon_env["tmp_path"], 0.1)

        spoken = _run(daemon_env, player, _empty(queue_dir))

        assert spoken == ["fresh"]
        log = daemon_env["log_file"].read_text()
        assert f"Dropped stale message {stale_id} from stale" in log
        assert not fresh.exists() and "Error in daemon loop" not in log
        stats = log_stats(log.splitlines())
        assert stats["stale_dropped"] == 1
        assert "stale dropped: 1" in format_log_stats(stats, "daemon.log", 1)

    def test_the_reply_playing_when_the_prompt_arrives_finishes(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]
        player, started, done = _marking_player(daemon_env["tmp_path"], 0.8)
        _reply("playing reply")
        behind = _reply("behind reply")  # queued behind it, maybe prefetched: stale too

        def prompt_while_playing():
            for _ in range(100):
                if started.exists():
                    break
                time.sleep(0.02)
            assert not done.exists()
            _supersede()

        spoken = _run(daemon_env, player, _empty(queue_dir), during=prompt_while_playing)

        assert spoken == ["playing"]
        assert done.exists()  # the player ran to its end: not cut mid-sentence
        log = daemon_env["log_file"].read_text()
        assert "interrupted" not in log.lower() and "Dropped stale message" in log
        assert not behind.exists() and read_playback_state().get("current_message") is None

    def test_another_rooms_reply_still_speaks(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]
        _reply("mine reply", session=ROOM_A)
        _reply("theirs reply", session=ROOM_B)
        _supersede(ROOM_A)
        player, _, _ = _marking_player(daemon_env["tmp_path"], 0.1)
        assert _run(daemon_env, player, _empty(queue_dir)) == ["theirs"]

    def _after_the_control_is_taken(self, daemon_env, **reply_times) -> tuple[list[str], str]:
        """Write the control, wait until the daemon took it, then write a reply with these times."""
        queue_dir = daemon_env["queue_dir"]
        ctl = _supersede()
        ctl_ts = json.loads(ctl.read_text())["timestamp"]
        player, _, _ = _marking_player(daemon_env["tmp_path"], 0.1)
        late: list[Path] = []

        def reply_after_the_control():
            for _ in range(100):
                if not ctl.exists():
                    break
                time.sleep(0.02)
            late.append(_reply("late reply", **{k: ctl_ts + d for k, d in reply_times.items()}))

        spoken = _run(daemon_env, player, lambda: bool(late) and _empty(queue_dir)(),
                      during=reply_after_the_control)
        return spoken, daemon_env["log_file"].read_text()

    def test_a_late_reply_from_the_old_turn_is_dropped(self, daemon_env):
        """Esc-and-retype: the Stop hook started before the prompt, wrote 3 s after it."""
        spoken, log = self._after_the_control_is_taken(daemon_env, timestamp=3.0, hook_started=-0.5)
        assert spoken == [] and "Dropped stale message" in log

    def test_a_reply_of_the_new_turn_speaks(self, daemon_env):
        spoken, log = self._after_the_control_is_taken(daemon_env, timestamp=3.0, hook_started=2.0)
        assert spoken == ["late"] and "Dropped stale message" not in log


# --- the UserPromptSubmit hook ---


class TestHookWritesTheSupersede:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mq, "QUEUE_DIR", tmp_path / "queue")
        monkeypatch.setattr(audio_mod, "daemon_healthy", lambda: True)
        self.cfg = TTSConfig(mode="queue", project_name="proj")
        monkeypatch.setattr(cli_mod, "load_config", lambda sid="": self.cfg)
        self.queue_dir = tmp_path / "queue"
        self.hook = {"transcript_path": f"/h/.claude/projects/{ROOM_A}/t.jsonl", "prompt": "next"}

    def test_queue_mode_writes_one_for_the_transcripts_session(self):
        path = cli_mod._supersede_from_hook({**self.hook, "session_id": "uuid-a"})
        assert path is not None
        msg = json.loads(path.read_text())
        assert msg["supersede_session"] == ROOM_A and msg["supersede_project"] == "proj"
        assert msg["supersede_claude_session"] == "uuid-a"

    def test_the_room_session_wins_over_the_transcript(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_TTS_SESSION", "named-room")
        path = cli_mod._supersede_from_hook(self.hook)
        assert path is not None and json.loads(path.read_text())["supersede_session"] == "named-room"

    @pytest.mark.parametrize("why", ["direct", "dead", "disabled", "nosession"])
    def test_nothing_is_written_where_nothing_is_queued(self, why, monkeypatch):
        hook = dict(self.hook)
        if why == "direct":
            self.cfg.mode = "direct"
        elif why == "dead":
            monkeypatch.setattr(audio_mod, "daemon_healthy", lambda: False)
        elif why == "disabled":
            monkeypatch.setenv("CLAUDE_TTS_ENABLED", "0")
        else:
            hook["transcript_path"] = "/elsewhere/t.jsonl"
        assert cli_mod._supersede_from_hook(hook) is None
        assert not self.queue_dir.exists()

    def test_no_prompt_in_the_input_writes_nothing(self):
        for hook in ({"transcript_path": self.hook["transcript_path"]}, {**self.hook, "prompt": "  "}):
            assert cli_mod._supersede_from_hook(hook) is None
        assert not self.queue_dir.exists()

    def _main(self, monkeypatch, argv: list[str], stdin_text: str) -> None:
        import io
        stdin = io.StringIO(stdin_text)
        stdin.isatty = lambda: False  # type: ignore[method-assign]
        monkeypatch.setattr("sys.stdin", stdin)
        monkeypatch.setattr("sys.argv", ["claude-tts", *argv])
        cli_mod.main()

    def test_the_cli_entry_writes_it_and_prints_nothing(self, monkeypatch, capsys):
        self._main(monkeypatch, ["supersede", "--from-hook"], json.dumps(self.hook))
        assert capsys.readouterr().out == ""
        written = [json.loads(p.read_text()) for p in self.queue_dir.glob("*.json")]
        assert [m["supersede_session"] for m in written] == [ROOM_A]

    @pytest.mark.parametrize("stdin_text", ["", "{not json", "[]"])
    def test_the_cli_entry_is_silent_and_harmless_on_bad_input(self, monkeypatch, capsys, stdin_text):
        self._main(monkeypatch, ["supersede", "--from-hook"], stdin_text)
        assert capsys.readouterr().out == "" and not self.queue_dir.exists()

    def test_tone_context_no_longer_supersedes(self, monkeypatch, capsys):
        import claude_code_tts.handy as handy_mod
        monkeypatch.setattr(handy_mod, "get_aggregated_tone", lambda **kw: None)
        self._main(monkeypatch, ["handy", "tone-context"], json.dumps(self.hook))
        assert capsys.readouterr().out == "" and not self.queue_dir.exists()


SHIM = Path(__file__).resolve().parent.parent / "hooks" / "prompt-submitted.sh"


@pytest.mark.parametrize("cli_body", [
    'echo "would join the prompt"; exit 0',             # a CLI that prints
    'echo "usage: invalid choice" >&2; exit 2',        # a CLI older than the subcommand
])
def test_the_shim_never_prints_and_never_blocks_the_prompt(tmp_path, cli_body):
    """A UserPromptSubmit hook's stdout is added to the prompt and exit 2 blocks it."""
    import subprocess
    fake = tmp_path / "claude-tts"
    fake.write_text(f"#!/bin/bash\n{cli_body}\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["bash", str(SHIM)], input="{}", capture_output=True, text=True, env=env, timeout=10)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_an_overflow_trims_the_stale_replies_not_another_rooms_older_message(daemon_env):
    """Drop before ageing and trimming: a spamming room's stale replies go, room B's stays."""
    theirs = _reply("theirs from room B", session=ROOM_B)  # the oldest message in the queue
    stale = [_reply(f"stale{i} reply", session=ROOM_A) for i in range(4)]
    _supersede(ROOM_A)
    supersedes = mq.Supersedes()
    with patch.object(daemon_mod, "_daemon_mode", True):
        queued = daemon_mod.drop_superseded(daemon_mod.get_queue_messages(), supersedes)
        daemon_mod.cleanup_old_messages(300, None, queued)
        trimmed = daemon_mod.enforce_max_depth(2, None, queued)
    assert theirs.exists() and not any(p.exists() for p in stale) and trimmed == 0
    assert [m["_file"] for m in queued] == [theirs]


def test_trimming_before_the_supersedes_would_take_room_bs_message(daemon_env):
    """The control for the test above: the old order loses room B's message."""
    theirs = _reply("theirs from room B", session=ROOM_B)
    for i in range(4):
        _reply(f"stale{i} reply", session=ROOM_A)
    _supersede(ROOM_A)
    with patch.object(daemon_mod, "_daemon_mode", True):
        daemon_mod.enforce_max_depth(2, None, daemon_mod.get_queue_messages())
    assert not theirs.exists()



def test_a_cutoff_outlives_a_pause_as_the_messages_it_guards_do():
    """Paused time does not age a message, so it does not age a cutoff either."""
    ledger = mq.PauseLedger()
    s = mq.Supersedes(keep_s=10.0)
    s._cutoffs[(ROOM_A, "")] = (100.0, "proj", 100.0)
    ledger.mark(True, now=101.0)
    ledger.mark(False, now=161.0)  # a 60 s mic hold
    stale = {"session_id": ROOM_A, "timestamp": 99.0, "_file": Path("/nowhere/x.json"), "id": "x"}
    s.apply([], now=165.0, ledger=ledger)
    assert s.cutoff_for(stale) is not None
    s.apply([], now=175.0, ledger=ledger)
    assert s.cutoff_for(stale) is None
