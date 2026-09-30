"""The next queue message is synthesized while the current one plays.

JMO, 2026-09-29, after hearing the gap between messages: "I wonder if our
processing queue and our playback queues are decoupled as they should be."
They were not: the loop synthesized message N+1 only after N had finished
playing, so every boundary cost a full synthesis. Now a one-slot Prefetch
synthesizes N+1 during N's playback, and the loop takes the result when it
is for the very file it picks next, discarding it otherwise.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from unittest.mock import patch

import claude_code_tts.daemon as daemon_mod
import tests.test_daemon_integration as _integ
from claude_code_tts.daemon import Prefetch, PreparedMessage, next_speakable, prepare_message

daemon_env = _integ.daemon_env  # the shared fixture, registered under its own name in this module
make_fake_player, make_wav, read_playback_state = _integ.make_fake_player, _integ.make_wav, _integ.read_playback_state


def _msg(text="hello", **over) -> dict:
    m = {
        "id": secrets.token_hex(8), "timestamp": time.time(), "session_id": "s", "project": "p",
        "text": text, "persona": "claude-prime", "speed": 2.0, "speed_method": "playback",
        "voice_kokoro": "", "voice_kokoro_blend": "", "_file": Path(f"/nowhere/{secrets.token_hex(4)}.json"),
    }
    m.update(over)
    return m


class TestPrepareMessage:
    def test_resolves_like_the_loop_did(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "get_persona_config", lambda _n: {"voice": "en_US-lessac-medium", "speed": 1.5})
        p = prepare_message(_msg("hi there", speed=3.0), {})
        assert (p.speed, p.effective_speed, p.effective_speed_method) == (3.0, 3.0, "playback")
        assert p.speaker_key == "s:p" and p.job_id is None and "id" not in p.current_msg_info
        assert p.audio_file.name.startswith("tts_queue_s_") and p.audio_file.suffix == ".wav"

    def test_two_messages_get_two_wav_files(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "get_persona_config", lambda _n: {})
        a, b = prepare_message(_msg(), {}), prepare_message(_msg(), {})
        assert a.audio_file != b.audio_file  # N+1 is synthesized while N plays: no shared file

    def test_bridge_job_and_mlx_override_carry_through(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "get_persona_config", lambda _n: {"voice_kokoro": "af_bella"})
        p = prepare_message(_msg(source="page", want_marks=True, engine="mlx", voice_mlx="m", speaker_mlx="v", lang_mlx="a"), {})
        assert p.job_id == p.msg["id"] and p.current_msg_info["source"] == "page" and p.want_marks
        assert (p.voice_mlx, p.speaker_mlx, p.lang_mlx) == ("m", "v", "a") and p.voice_label == "mlx:m#v"
        plain = prepare_message(_msg(voice_mlx="m"), {})  # a hook message: persona stays in charge
        assert plain.voice_mlx == "" and plain.voice_label == "kokoro:af_bella"

    def test_sherpa_speed_moves_into_synthesis(self, monkeypatch):
        monkeypatch.setattr(daemon_mod, "get_persona_config", lambda _n: {"voice_sherpa": "vctk"})
        assert prepare_message(_msg(), {}).effective_speed_method == "length_scale"


class TestNextSpeakable:
    def test_skips_the_current_and_empty_messages(self):
        cur, empty, nxt = _msg("now"), _msg("   "), _msg("next")
        assert next_speakable([cur, empty, nxt], cur["_file"]) is nxt

    def test_stops_at_a_control_message(self):
        cur, ctl, nxt = _msg("now"), _msg("", type="control"), _msg("next")
        assert next_speakable([cur, ctl, nxt], cur["_file"]) is None

    def test_none_when_nothing_follows(self):
        cur = _msg("now")
        assert next_speakable([cur], cur["_file"]) is None


class TestPrefetchSlot:
    def _prepared(self, tmp_path, name, **over) -> PreparedMessage:
        msg_file = tmp_path / f"{name}.json"
        msg_file.write_text("{}")  # start() refuses a message whose file is already gone
        with patch.object(daemon_mod, "get_persona_config", return_value={}):
            p = prepare_message(_msg(name, _file=msg_file, **over), {})
        p.audio_file = tmp_path / f"{name}.wav"
        return p

    @staticmethod
    def _synth_ok(prep):
        prep.audio_file.write_bytes(b"RIFF")
        return True, None

    def test_start_skips_a_message_already_flushed(self, tmp_path):
        p = self._prepared(tmp_path, "a")
        p.msg_file.unlink()
        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=self._synth_ok) as synth:
            pf.start(p)
        synth.assert_not_called()
        assert pf.pending is None

    def test_start_does_not_relabel_a_cancelled_bridge_job(self, tmp_path):
        """Review finding 2026-09-29: a /stop between the queue read and start() must win."""
        from claude_code_tts.bridge import JOBS
        p = self._prepared(tmp_path, "a", source="page")
        JOBS.create(p.job_id, source="page")
        JOBS.update(p.job_id, state="cancelled", position_ms=0)
        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=self._synth_ok) as synth:
            pf.start(p)
        synth.assert_not_called()
        assert JOBS.state(p.job_id) == "cancelled" and pf.pending is None

    def test_discard_puts_a_live_job_back_to_queued_and_a_gone_one_to_cancelled(self, tmp_path):
        from claude_code_tts.bridge import JOBS
        live = self._prepared(tmp_path, "live", source="page")
        gone = self._prepared(tmp_path, "gone", source="page")
        for q in (live, gone):
            JOBS.create(q.job_id, source="page")
        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=self._synth_ok):
            pf.start(live)
            assert JOBS.state(live.job_id) == "synthesizing"
            pf.take(tmp_path / "other.json")  # picked something else first: it stays in the queue
            assert JOBS.state(live.job_id) == "queued" and not live.audio_file.exists()
            pf.start(gone)
            gone.msg_file.unlink()
            pf.discard_if_gone()
            assert JOBS.state(gone.job_id) == "cancelled" and not gone.audio_file.exists()

    def test_take_returns_the_result_for_the_same_file(self, tmp_path):
        p = self._prepared(tmp_path, "a")

        def synth(prep):
            prep.audio_file.write_bytes(b"RIFF")
            return True, {"sentences": []}

        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=synth):
            pf.start(p)
            assert pf.pending == p.msg_file
            taken = pf.take(p.msg_file)
        assert taken is not None and taken[0] is p and taken[1] is True and taken[2] == {"sentences": []}
        assert p.audio_file.exists() and pf.pending is None
        assert pf.take(p.msg_file) is None  # one slot, taken once

    def test_take_for_another_file_discards_and_deletes_the_wav(self, tmp_path):
        p = self._prepared(tmp_path, "a")

        def synth(prep):
            prep.audio_file.write_bytes(b"RIFF")
            return True, None

        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=synth):
            pf.start(p)
            assert pf.take(tmp_path / "other.json") is None  # flushed, trimmed or expired meanwhile
        assert not p.audio_file.exists() and pf.pending is None

    def test_discard_if_gone_drops_a_prefetch_whose_file_vanished(self, tmp_path):
        p = self._prepared(tmp_path, "a")
        p.msg_file.write_text("{}")

        def synth(prep):
            prep.audio_file.write_bytes(b"RIFF")
            return True, None

        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=synth):
            pf.start(p)
            pf.discard_if_gone()
            pf.wait()  # the synthesis thread writes the WAV; assert after it, not racing it
            assert pf.pending == p.msg_file and p.audio_file.exists()  # file still there: kept
            p.msg_file.unlink()
            pf.discard_if_gone()
        assert pf.pending is None and not p.audio_file.exists()

    def test_a_crashing_synthesis_is_a_failed_result_not_a_dead_loop(self, tmp_path):
        p = self._prepared(tmp_path, "a")
        pf = Prefetch()
        with patch.object(daemon_mod, "synthesize_prepared", side_effect=RuntimeError("boom")):
            pf.start(p)
            taken = pf.take(p.msg_file)
        assert taken is not None and taken[1] is False


class TestLoopOverlapsSynthesisWithPlayback:
    QUEUE_CONFIG = {
        "max_depth": 20, "max_age_seconds": 300, "speaker_transition": "none",
        "coalesce_rapid_ms": 500, "idle_poll_ms": 50,
    }

    def _enqueue(self, queue_dir: Path, text: str) -> Path:
        ts = time.time()
        msg_id = secrets.token_hex(8)
        msg = {
            "id": msg_id, "timestamp": ts, "session_id": "test-session", "project": "test-project",
            "text": text, "persona": "claude-prime", "speed": 2.0, "speed_method": "playback",
            "voice_kokoro": "", "voice_kokoro_blend": "",
        }
        path = queue_dir / f"{ts:.6f}_{msg_id}.json"
        path.write_text(json.dumps(msg))
        time.sleep(0.01)  # distinct timestamps keep the order
        return path

    def _run(self, daemon_env, fake_generate, play_duration, stop_when, extra_patches=()):
        tmp = daemon_env["tmp_path"]
        fake = make_fake_player(tmp, duration=play_duration)
        patches = [
            patch.object(daemon_mod, "detect_player", return_value=[str(fake)]),
            patch.object(daemon_mod, "daemon_generate_speech", side_effect=fake_generate),
            patch.object(daemon_mod, "acquire_lock", return_value=True),
            patch.object(daemon_mod, "release_lock"),
            patch.object(daemon_mod, "speak_announcement"),
            patch.object(daemon_mod, "get_queue_config", return_value=dict(self.QUEUE_CONFIG)),
            patch.object(daemon_mod, "load_raw_config", return_value={}),
            patch("signal.signal"),  # signal.signal fails in non-main threads
            *extra_patches,
        ]
        for pt in patches:
            pt.start()
        try:
            def run_daemon():
                daemon_mod._shutdown_requested = False
                daemon_mod._daemon_mode = True
                daemon_mod.daemon_loop()

            def stopper():
                for _ in range(100):
                    time.sleep(0.1)
                    if stop_when():
                        break
                time.sleep(0.3)
                daemon_mod._shutdown_requested = True

            st = threading.Thread(target=stopper)
            rn = threading.Thread(target=run_daemon, daemon=True)
            st.start()
            rn.start()
            st.join(timeout=15)
            rn.join(timeout=3)
        finally:
            for pt in patches:
                pt.stop()

    def test_second_message_is_synthesized_while_the_first_plays(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]
        calls: list[tuple[float, str, Path]] = []

        def fake_generate(text, persona, output_file, **kw):
            calls.append((time.monotonic(), text, Path(output_file)))
            make_wav(output_file, 2.0)
            return True

        f1 = self._enqueue(queue_dir, "first message")
        f2 = self._enqueue(queue_dir, "second message")
        self._run(daemon_env, fake_generate, play_duration=1.0, stop_when=lambda: not f1.exists() and not f2.exists())

        assert [c[1] for c in calls] == ["first message", "second message"]
        assert calls[0][2] != calls[1][2]
        # Serial would put the second synthesis after the first playback (1.0 s); the prefetch
        # starts it as the first begins to play.
        assert calls[1][0] - calls[0][0] < 0.6, f"second synthesis waited {calls[1][0] - calls[0][0]:.2f}s"
        assert not calls[0][2].exists() and not calls[1][2].exists()  # both WAVs cleaned up
        assert read_playback_state().get("current_message") is None

    def test_prefetched_message_removed_before_its_turn_is_not_played(self, daemon_env):
        queue_dir = daemon_env["queue_dir"]
        calls: list[tuple[str, Path]] = []
        played: list[Path] = []
        real_play = daemon_mod.daemon_play_audio

        def fake_generate(text, persona, output_file, **kw):
            calls.append((text, Path(output_file)))
            make_wav(output_file, 2.0)
            return True

        def counting_play(wav_file, *a, **kw):
            played.append(Path(wav_file))
            return real_play(wav_file, *a, **kw)

        f1 = self._enqueue(queue_dir, "first message")
        f2 = self._enqueue(queue_dir, "doomed message")

        def pull_second():
            # Once the prefetch has synthesized it, a bridge /stop (or a trim) removes the file.
            for _ in range(100):
                time.sleep(0.05)
                if len(calls) == 2:
                    break
            f2.unlink(missing_ok=True)

        threading.Thread(target=pull_second, daemon=True).start()
        self._run(daemon_env, fake_generate, play_duration=1.0,
                  stop_when=lambda: not f1.exists() and not f2.exists(),
                  extra_patches=(patch.object(daemon_mod, "daemon_play_audio", side_effect=counting_play),))

        assert [c[0] for c in calls] == ["first message", "doomed message"]  # it was prefetched
        assert played == [calls[0][1]]                                        # and never played
        assert not calls[1][1].exists()                                       # its WAV is gone
