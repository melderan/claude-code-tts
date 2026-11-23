"""hooks/speak-intermediate.sh starts no Python where the CLI would speak nothing and write nothing a skip loses.

Geordi's gate (2026-11-21) ported to this repo and rechecked against cli.py at HEAD. Each test runs the real shim as the
child of a stand-in `claude` (a script of that name, so ps reads it as the ancestor a real session has), with a fake
claude-tts that records what it was given. The transcript, watermark, pins, names file and /tmp marks are planted in a
tmp HOME and uuid-named /tmp files that the test removes; ~/.claude-tts is never touched.

The controls: a test that the old shim (the quiet-mark shim, HEAD before this change) fails is one that the gate
changes; the others hold on both, and say what must not change.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from claude_code_tts.config import atomic_write_json
from tests.test_watermark import _assistant, _run_hook, _tool_result, _user, _write_transcript

SHIM = Path(__file__).resolve().parent.parent / "hooks" / "speak-intermediate.sh"
SID = "-Users-dev"  # the folder id in the transcript path, which the CLI uses as the session
CS = "c0ffee00-1111-2222-3333-444455556666"  # the Claude session id in the payload
AGENT = "a372d89335898f923"

# A stand-in claude. Its pid is the ancestor the shim walks to. It plants the pins a real session's hooks write, then
# runs the shim as its child. PROBE=1 only reports its own comm, so a platform that does not name it claude is skipped.
STUB_CLAUDE = """#!/bin/bash
if [ -n "$PROBE" ]; then ps -o comm= -p $$; exit 0; fi
act="$HOME/.claude-tts/active"; mkdir -p "$act"
h=$(uname -n); h=${h%%.*}
[ -n "$PIN_SESSION" ] && printf '%s' "$PIN_SESSION" > "$act/$h-$$.session"
[ -n "$PIN_CLAUDE" ] && printf '%s' "$PIN_CLAUDE" > "$act/$h-$$.claude-session"
bash "$SHIM"
exit $?
"""

# A fake claude-tts: records its arguments and its stdin, speaks nothing.
FAKE_CLI = """#!/bin/bash
echo "$@" > "$CALLED_ARGS"
cat > "$CALLED_STDIN"
"""

# A transcript as Claude Code leaves it after one reply and one tool call: two lines were spoken, the third is new.
BASE = [_user("hi"), _assistant("first reply, long enough to speak"), _tool_result()]
SPOKEN_TO = 2  # the watermark after the reply: lines 0 and 1 are behind it, the tool result (line 2) is not


@dataclass
class Run:
    spawned: bool
    rc: int
    args: str
    stdin: object


class World:
    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        self.key = f"gate-{uuid.uuid4().hex}"
        self.transcript = self.home / "projects" / SID / f"{self.key}.jsonl"
        self.state = Path(f"/tmp/claude_tts_spoken_{self.key}.state")
        self.quiet = Path(f"/tmp/claude_tts_spoken_{self.key}.quiet")
        self.pending = Path(f"/tmp/claude_tts_spoken_{self.key}.pending")
        self.lock = Path(f"/tmp/claude_tts_wm_{self.key}.lock")
        self.claude = self.bin / "claude"
        self.claude.write_text(STUB_CLAUDE)
        self.cli = self.bin / "claude-tts"
        self.cli.write_text(FAKE_CLI)
        for p in (self.claude, self.cli):
            p.chmod(0o755)
        self.called_args = root / "called-args"
        self.called_stdin = root / "called-stdin"
        self.sessions_d = self.home / ".claude-tts" / "sessions.d"
        self.sessions_d.mkdir(parents=True)
        (self.sessions_d / f"{SID}.json").write_text('{"persona": "claude-prime"}')

    def cleanup(self) -> None:
        for p in (self.state, self.quiet, self.pending):
            p.unlink(missing_ok=True)
        if self.lock.is_dir():
            self.lock.rmdir()

    def probe(self) -> str:
        r = subprocess.run([str(self.claude)], env=self.env({"PROBE": "1"}), capture_output=True, text=True, timeout=10)
        return r.stdout.strip()

    def env(self, extra: dict) -> dict:
        base = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_TTS_")}
        # A sandbox sets BASH_ENV to its persistent environment file, which every non-interactive bash child
        # sources again: that would bring CLAUDE_TTS_SESSION back into the stand-in claude's shim.
        base.pop("BASH_ENV", None)
        base.update({
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "SHIM": str(SHIM),
            "CALLED_ARGS": str(self.called_args),
            "CALLED_STDIN": str(self.called_stdin),
        })
        base.update(extra)
        return base

    def plant_transcript(self, lines: list[dict], spoken_to: int | None = None) -> None:
        _write_transcript(self.transcript, lines)
        if spoken_to is not None:
            self.state.write_text(str(spoken_to))

    def payload(self) -> dict:
        return {"session_id": CS, "transcript_path": str(self.transcript), "tool_name": "Bash",
                "tool_input": {"command": "ls"}, "tool_response": {"stdout": "x" * 200_000}}

    def names_muted(self) -> None:
        # The writer's own function, so the gate reads the shape the CLI writes, not one this test guessed.
        path = self.home / ".claude-tts" / "names" / f"{CS}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"muted": True, "claude_session_id": CS, "set_at": 1700000000.5})

    def run(self, payload: dict | None = None, *, raw: str | None = None, agent: bool = False,
            pin_session: str | None = SID, pin_claude: str | None = CS, extra_env: dict | None = None) -> Run:
        if raw is None:
            payload = dict(payload if payload is not None else self.payload())
            if agent:
                payload["agent_id"] = AGENT
            raw = json.dumps(payload)
        for f in (self.called_args, self.called_stdin):
            f.unlink(missing_ok=True)
        env = self.env({"PIN_SESSION": pin_session or "", "PIN_CLAUDE": pin_claude or "", **(extra_env or {})})
        r = subprocess.run([str(self.claude)], input=raw, env=env, capture_output=True, text=True, timeout=30)
        spawned = self.called_args.exists()
        stdin = json.loads(self.called_stdin.read_text()) if spawned and self.called_stdin.read_text().strip() else None
        args = self.called_args.read_text().strip() if spawned else ""
        return Run(spawned, r.returncode, args, stdin)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    seen = w.probe()
    if seen != "claude":
        pytest.skip(f"ps names a script run as claude {seen!r} on this machine; the gate needs the claude ancestor")
    yield w
    w.cleanup()


def _with_text(lines: list[dict], text: str = "text before the next tool call") -> list[dict]:
    return [*lines, _assistant(text)]


@pytest.mark.parametrize(("agent", "watermark"), [
    (True, "count"),    # a brother's call with nothing new past the mark
    (False, "count"),   # the parent's call with nothing new: its tool_use line may not be written yet, the reread settles it
    (True, "tool"),     # a brother's call whose only new line is a tool result
    (False, "tool"),    # the parent's call whose only new line is a tool result
])
def test_nothing_new_or_no_new_text_starts_no_python(world, agent, watermark):
    world.plant_transcript(BASE, spoken_to=len(BASE) if watermark == "count" else SPOKEN_TO)
    before = world.state.read_text()

    run = world.run(agent=agent)

    assert (run.spawned, run.rc) == (False, 0), run.args
    assert world.state.read_text() == before  # the skip moved no mark
    assert not world.quiet.exists()  # and wrote no quiet mark: the shim reads none, so none is needed


def test_a_tool_result_carrying_text_is_not_speech(world):
    line = {"type": "user", "message": {"content": [{"type": "tool_result", "content": [
        {"type": "text", "text": "ran ok, and a long line of output that is not the assistant's"}]}]}}
    world.plant_transcript([*BASE[:2], line], spoken_to=SPOKEN_TO)

    run = world.run(agent=False)

    assert not run.spawned, "a user line is not an assistant line: the CLI skips it, so the gate may too"


@pytest.mark.parametrize("agent", [True, False])
def test_new_assistant_text_starts_python_with_the_payload_whole(world, agent):
    world.plant_transcript(_with_text(BASE), spoken_to=SPOKEN_TO)
    payload = world.payload()

    run = world.run(payload, agent=agent)

    assert run.spawned and run.rc == 0
    assert run.args == "speak --from-hook --hook-type post_tool_use"
    expected = {**payload, **({"agent_id": AGENT} if agent else {})}
    assert run.stdin == expected


def test_a_non_empty_thinking_block_starts_python(world):
    thinking = {"type": "assistant", "message": {"id": "m1", "content": [
        {"type": "thinking", "thinking": "Plan the next step.", "signature": "sig"}]}}
    world.plant_transcript([*BASE[:2], thinking], spoken_to=SPOKEN_TO)

    assert world.run(agent=False).spawned


@pytest.mark.parametrize(("case", "pins", "sessions_file"), [
    ("no pins yet", {"pin_session": None, "pin_claude": None}, True),
    ("another session pinned", {"pin_session": "-Users-other", "pin_claude": CS}, True),
    ("no sessions.d file", {}, False),
])
def test_a_session_the_cli_has_not_settled_starts_python(world, case, pins, sessions_file):
    world.plant_transcript(BASE, spoken_to=len(BASE))
    if not sessions_file:
        (world.sessions_d / f"{SID}.json").unlink()

    run = world.run(agent=False, **pins)

    assert run.spawned, case


def test_a_session_muted_on_its_own_starts_no_python_even_with_new_text(world):
    world.plant_transcript(_with_text(BASE), spoken_to=SPOKEN_TO)
    world.names_muted()

    assert not world.run(agent=False).spawned


def test_claude_tts_disabled_starts_no_python(world):
    world.plant_transcript(_with_text(BASE), spoken_to=SPOKEN_TO)

    run = world.run(agent=False, extra_env={"CLAUDE_TTS_ENABLED": "0"})

    assert (run.spawned, run.rc) == (False, 0)


def test_an_empty_payload_starts_no_python(world):
    assert not world.run(raw="", agent=False).spawned


def test_a_watermark_past_the_transcript_starts_python(world):
    world.plant_transcript(BASE, spoken_to=len(BASE) + 5)  # the CLI resets it, and writes: the gate may not skip that

    assert world.run(agent=False).spawned


def test_an_unreadable_watermark_starts_python(world):
    world.plant_transcript(BASE)
    world.state.write_text("not a number")

    assert world.run(agent=False).spawned


def test_the_gate_off_starts_python_for_every_call(world):
    world.plant_transcript(BASE, spoken_to=len(BASE))

    assert world.run(agent=False, extra_env={"CLAUDE_TTS_GATE": "off"}).spawned


def test_a_path_the_shell_cannot_read_plainly_starts_python(world):
    world.plant_transcript(BASE, spoken_to=len(BASE))
    raw = json.dumps({"session_id": CS, "transcript_path": str(world.transcript)}).replace("/", "\\/")

    assert world.run(raw=raw, agent=False).spawned


def test_python_is_silent_where_the_gate_skips(world):
    """The parity claim, against the CLI itself: where the shim skips, the same transcript and mark speak nothing."""
    world.plant_transcript(BASE, spoken_to=len(BASE))
    assert not world.run(agent=False).spawned
    assert _run_hook(world.transcript, "post_tool_use") is None
    assert world.state.read_text() == str(len(BASE))

    world.plant_transcript(BASE, spoken_to=SPOKEN_TO)
    assert not world.run(agent=False).spawned
    assert _run_hook(world.transcript, "post_tool_use") is None
    assert world.state.read_text() == str(SPOKEN_TO)
