"""`claude-tts daemon install-service`: the daemon's LaunchAgent, written and started from the package.

A fresh Mac gets the CLI from a wheel, and the wheel carries the LaunchAgent template (services/,
inside the package; pyproject.toml force-include). This module renders that template for the user
running it, parses it, lints it with plutil, loads it with launchctl, and waits for the daemon to
write its pid and a fresh heartbeat. It needs no repo checkout and no network.

It refuses where a second daemon would play every message twice: a daemon is alive on this Mac
already, or the state directory has a fresh heartbeat and no daemon runs here (another machine
shares the directory). Every read and every command goes through Env, so the tests inject fakes.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from claude_code_tts import state

PLIST_NAME = "com.claude-tts.daemon.plist"
LABEL = "com.claude-tts.daemon"
# The installed package. A wheel ships services/ here (pyproject.toml force-include).
PACKAGE_DIR = Path(__file__).resolve().parent
# How long the daemon gets to write its pid and a heartbeat after launchctl loads it.
WAIT_S = 20.0
POLL_S = 0.25
# launchctl's words when the gui/<uid> domain has no login session (ssh, or no console login).
_DOMAIN_UNAVAILABLE = ("Domain does not support specified action", "Could not find domain")

Runner = Callable[[list[str]], tuple[int, str, str]]
_PLACEHOLDER = re.compile(r"CLAUDE_TTS_(BIN_DIR|BIN|CONFIG|HOME)")


def service_template(name: str, repo_dir: Path | None = None) -> Path | None:
    """Where a service template is: repo_dir/services first (a checkout, or the installer's REPO_DIR
    when it is an installed wheel), else the package's own services/. None when neither has it.

    The installer and `daemon install-service` both look here, so they cannot disagree about the template.
    """
    for base in (repo_dir, PACKAGE_DIR):
        if base is None:
            continue
        candidate = base / "services" / name
        if candidate.is_file():
            return candidate
    return None


def is_packaged(template: Path) -> bool:
    """True when the template sits inside the installed package, not beside a checkout."""
    return template.resolve().is_relative_to(PACKAGE_DIR)


def render_plist(template: str, *, claude_tts_bin: str, state_dir: Path, home: Path) -> str:
    """The template with this user's paths, XML-escaped. One pass, so a value is never read again as a placeholder."""
    values = {
        "BIN_DIR": str(Path(claude_tts_bin).parent),
        "BIN": claude_tts_bin,
        "CONFIG": str(state_dir),
        "HOME": str(home),
    }
    return _PLACEHOLDER.sub(lambda m: xml_escape(values[m.group(1)]), template)


def plist_problem(text: str, *, claude_tts_bin: str, state_dir: Path) -> str | None:
    """Why this rendered text is not the daemon's LaunchAgent, or None when it is."""
    if "CLAUDE_TTS_" in text:
        return "a placeholder is left in the template"
    try:
        data = plistlib.loads(text.encode("utf-8"))
    except Exception as e:  # plistlib raises several types for bad XML
        return f"the rendered plist does not parse ({e})"
    if not isinstance(data, dict):
        return "the rendered plist is not a dictionary"
    if data.get("Label") != LABEL:
        return f"Label is {data.get('Label')!r}, not {LABEL!r}"
    if data.get("ProgramArguments") != [claude_tts_bin, "daemon", "foreground"]:
        return "ProgramArguments is not <claude-tts> daemon foreground"
    if data.get("StandardOutPath") != str(state_dir / "daemon.stdout.log"):
        return "StandardOutPath is not in the daemon's state directory"
    return None


@dataclass
class Env:
    """Everything the command reads from this Mac or runs on it. default_env() fills it; a test fills it with fakes."""

    platform: str
    home: Path
    uid: int
    claude_tts_bin: str | None
    template: Path | None
    run: Runner
    alive: Callable[[int], bool]
    is_daemon: Callable[[int], bool | None]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wait_s: float = WAIT_S
    poll_s: float = POLL_S


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Run one command and return (rc, stdout, stderr). A command that cannot start is rc 127."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return done.returncode, done.stdout, done.stderr


def default_env() -> Env:
    """The real machine: this user's home, this uid, the claude-tts on PATH, launchctl and plutil."""
    from claude_code_tts.audio import detect_platform
    from claude_code_tts.install import REPO_DIR

    found = shutil.which("claude-tts")
    if found is None and Path(sys.argv[0]).name == "claude-tts":
        found = sys.argv[0]
    bin_path = os.path.abspath(found) if found else None
    if bin_path is not None and not os.access(bin_path, os.X_OK):
        bin_path = None
    return Env(
        platform=detect_platform(),
        home=Path.home(),
        uid=os.getuid(),
        claude_tts_bin=bin_path,
        template=service_template(PLIST_NAME, REPO_DIR),
        run=_run,
        alive=state.pid_alive,
        is_daemon=state.pid_is_daemon,
    )


@dataclass(frozen=True)
class Survey:
    """What is on this Mac right now for the daemon: read, never changed."""

    pid: int | None
    pid_alive: bool
    pid_is_daemon: bool | None  # None: not asked (a dry run runs no ps)
    heartbeat_age: float | None
    plist: str  # "absent", "same" or "different" from the rendered text
    plist_path: Path
    state_dir: Path

    @property
    def local_daemon(self) -> bool:
        return self.pid_alive and self.pid_is_daemon is not False

    def refusal(self, force: bool) -> str | None:
        """The one line that refuses this install, or None when it may go ahead."""
        if self.local_daemon:
            return (
                f"refused: a daemon is already running on this Mac (PID {self.pid}); "
                "stop it with `claude-tts daemon stop`, then run this again"
            )
        age = self.heartbeat_age
        if age is not None and age < state.HEARTBEAT_MAX_AGE_S:
            return (
                f"refused: {self.state_dir} has a heartbeat {age:.0f}s old but no daemon runs on this Mac: "
                "another machine shares it, and two daemons would play every message twice; "
                "stop that machine's daemon (claude-tts daemon stop), or install on the machine that should speak"
            )
        if self.plist == "different" and not force:
            return (
                f"refused: {self.plist_path} has different content from this install; "
                "read it with --dry-run, then run again with --force to replace it"
            )
        return None

    def describe(self) -> list[str]:
        out: list[str] = []
        if self.pid is None:
            out.append("daemon.pid: none")
        else:
            state_word = "alive" if self.pid_alive else "not alive"
            if not self.pid_alive:
                who = ""
            elif self.pid_is_daemon is None:
                who = ", identity not checked in a dry run"
            else:
                who = ", a daemon" if self.pid_is_daemon else ", not a daemon"
            out.append(f"daemon.pid: {self.pid}, {state_word}{who}")
        if self.heartbeat_age is None:
            out.append("heartbeat: none")
        else:
            out.append(f"heartbeat: {self.heartbeat_age:.1f}s old")
        out.append(f"plist: {self.plist}")
        return out


def _plist_state(path: Path, rendered: str) -> str:
    if not path.exists():
        return "absent"
    try:
        current = path.read_bytes()
    except OSError:
        return "different"
    return "same" if current == rendered.encode("utf-8") else "different"


def survey(env: Env, plist_path: Path, rendered: str, state_dir: Path) -> Survey:
    pid = state.daemon_pid()
    alive = pid is not None and env.alive(pid)
    is_daemon = env.is_daemon(pid) if (pid is not None and alive) else None
    return Survey(
        pid=pid,
        pid_alive=alive,
        pid_is_daemon=is_daemon,
        heartbeat_age=state.heartbeat_age(),
        plist=_plist_state(plist_path, rendered),
        plist_path=plist_path,
        state_dir=state_dir,
    )


def _say(msg: str) -> None:
    print(msg)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def install_launchagent(*, dry_run: bool = False, force: bool = False, env: Env | None = None) -> int:
    """Write and start the daemon's LaunchAgent. Exit 0 only when the daemon is up.

    0: the daemon wrote its pid and a fresh heartbeat. 1: refused, or not up in time. 2: not macOS (usage).
    A dry run writes nothing and runs nothing; it prints what a real run would do and exits 0 if that would proceed.
    """
    env = env or default_env()
    if env.platform != "macos":
        _err(
            "claude-tts daemon install-service is macOS-only for now; the systemd unit is not wired "
            "into this command yet, so on Linux run `claude-tts daemon foreground` under your own service manager"
        )
        return 2
    if env.template is None:
        _err(
            f"refused: {PLIST_NAME} is not in this install (no services/ in the package); "
            "reinstall the CLI from its wheel: uv tool install --force claude-code-tts"
        )
        return 1
    if env.claude_tts_bin is None:
        _err("refused: no executable claude-tts on PATH; run this from the install that owns the daemon")
        return 1
    try:
        template_text = env.template.read_text(encoding="utf-8")
    except OSError as e:
        _err(f"refused: cannot read {env.template}: {e}")
        return 1

    state_dir = state.PID_FILE.parent
    plist_path = env.home / "Library" / "LaunchAgents" / PLIST_NAME
    rendered = render_plist(template_text, claude_tts_bin=env.claude_tts_bin, state_dir=state_dir, home=env.home)
    problem = plist_problem(rendered, claude_tts_bin=env.claude_tts_bin, state_dir=state_dir)
    if problem is not None:
        _err(f"refused: the rendered LaunchAgent is wrong ({problem}); nothing was written")
        return 1

    if dry_run:
        return _dry_run(env, plist_path, rendered, state_dir, force)

    sv = survey(env, plist_path, rendered, state_dir)
    refused = sv.refusal(force)
    if refused is not None:
        _err(refused)
        return 1
    written = _write_plist(env, plist_path, rendered, state_dir)
    if written is not None:
        _err(f"refused: {written}; any plist already in place is unchanged")
        return 1
    return _load_and_wait(env, plist_path, sv)


def _write_plist(env: Env, plist_path: Path, rendered: str, state_dir: Path) -> str | None:
    """Write the plist atomically: a temp file beside it, linted by plutil, then renamed over it.

    A file plutil rejects never replaces a good one. The daemon's log directory must exist before
    launchd opens the log files, so it is made here too. None when the plist is in place.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{PLIST_NAME}.", suffix=".tmp", dir=plist_path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        os.chmod(tmp, 0o644)
        rc, out, err = env.run(["plutil", "-lint", str(tmp)])
        if rc != 0:
            return f"plutil rejected the rendered plist ({(err or out).strip()})"
        os.replace(tmp, plist_path)
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _load_and_wait(env: Env, plist_path: Path, sv: Survey) -> int:
    target = f"gui/{env.uid}/{LABEL}"
    loaded = env.run(["launchctl", "print", target])[0] == 0
    if not (loaded and sv.plist == "same"):
        # Loaded from another plist: unload it first, so the new one is the one that runs.
        if loaded:
            rc, _out, err = env.run(["launchctl", "bootout", target])
            if rc != 0:
                _err(f"refused: launchctl bootout {target} failed ({rc}): {err.strip()}")
                return 1
        rc, out, err = env.run(["launchctl", "bootstrap", f"gui/{env.uid}", str(plist_path)])
        if rc != 0:
            text = (err or out).strip()
            if any(marker in text for marker in _DOMAIN_UNAVAILABLE):
                _err(
                    "refused: launchd has no login session for this user here (ssh, or no console login); "
                    "run `claude-tts daemon install-service` at the Mac's own console, logged in"
                )
                return 1
            _err(f"refused: launchctl bootstrap failed ({rc}): {text}")
            return 1

    up = _wait_for_daemon(env)
    if up is None:
        _err(
            f"the LaunchAgent is loaded but no daemon heartbeat came within {env.wait_s:.0f}s; "
            f"read `claude-tts daemon logs` and `launchctl print {target}`"
        )
        return 1
    pid, age = up
    _say(f"LaunchAgent loaded from {plist_path}")
    _say(f"daemon.pid: {pid}")
    _say(f"daemon.release: {state.daemon_release() or 'unknown'}")
    _say(f"heartbeat: {age:.1f}s old")
    return 0


def _wait_for_daemon(env: Env) -> tuple[int, float] | None:
    """The daemon's pid and heartbeat age once both are live and fresh; None when the wait runs out."""
    deadline = env.clock() + env.wait_s
    while True:
        pid = state.daemon_pid()
        age = state.heartbeat_age()
        if pid is not None and age is not None and age < state.HEARTBEAT_MAX_AGE_S and env.alive(pid):
            return pid, age
        if env.clock() >= deadline:
            return None
        env.sleep(env.poll_s)


def _dry_run(env: Env, plist_path: Path, rendered: str, state_dir: Path, force: bool) -> int:
    target = f"gui/{env.uid}/{LABEL}"
    # A dry run runs no ps, so the pid's identity is not asked; a live pid counts as this Mac's daemon.
    probe = dataclasses.replace(env, is_daemon=lambda pid: None)
    sv = survey(probe, plist_path, rendered, state_dir)
    refused = sv.refusal(force)
    _say("dry run: nothing is written and nothing is run")
    _say(f"template:    {env.template}")
    _say(f"claude-tts:  {env.claude_tts_bin}")
    _say(f"state dir:   {state_dir} (created if missing; the daemon's logs go here)")
    _say(f"plist:       {plist_path}")
    _say("would write: a temp file beside it, then rename over it (--force replaces different content)")
    _say("would run:   plutil -lint <that temp file>")
    _say(f"would run:   launchctl print {target}        (is it loaded?)")
    _say(f"would run:   launchctl bootout {target}      (only when it is loaded and the plist changes)")
    _say(f"would run:   launchctl bootstrap gui/{env.uid} {plist_path}   (not when loaded from the same plist)")
    _say(f"then:        wait up to {env.wait_s:.0f}s for daemon.pid and a fresh heartbeat under {state_dir}")
    _say("today:       " + "; ".join(sv.describe()))
    _say("rendered plist:")
    _say(rendered.rstrip("\n"))
    if refused is not None:
        _say(f"a real run would refuse: {refused}")
        return 1
    _say("a real run would proceed")
    return 0
