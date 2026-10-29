#!/usr/bin/env python3
"""up.py - the operator step, run on the machine that owns the daemon (`just up`).

Rebuild the CLI from this checkout, deploy hooks and commands, restart the daemon,
verify the heartbeat, and record the run:

    .logs/just/up-<utc time>-<git ref>.log   full output of every step
    .logs/just/timeline.log                  one line per run, newest last (`just timeline`)

.logs/ is gitignored and lives in the checkout, so a sandbox sharing the working
tree can read what the host is running without asking. CLAUDE_TTS_UP_LOGDIR names another
directory, for a deploy clone that no sandbox shares (the timeline then lives where readers
can still see it). Every log is opened without following a symlink, and a log that is not a
plain file with one link, or a directory that is a symlink, refuses the run (exit 1): the
directory may be writable by others, and this script runs as the machine's owner.
Standard library only.

`just up --if-changed` is the scheduled form: when the installed CLI and the running
daemon already carry the checkout's version and the heartbeat is fresh, it writes one
"unchanged" line to the timeline and exits 0 without rebuilding or restarting. The
script puts ~/.local/bin (where uv installs tools) on its own PATH, so it runs the same
from a terminal, launchd or cron; it never reads a TTY. The scheduled form also deploys
only a released checkout: a clean working tree whose HEAD carries the tag v<version>.
The checkout is a working tree a sandbox edits in place, so a version bump mid-edit must
not reach the daemon; the tag is what "released" means here (see `claude-tts release`).

A pinned signer makes the tag prove who cut it. The fingerprint comes from
CLAUDE_TTS_UP_SIGNER, else from the file signer_file() names (outside the checkout and
outside ~/.claude-tts, so whoever can push a tag or write those cannot also move the pin).
Then a release must also pass `git verify-tag` with a signature from exactly that key; a
good signature from any other key in the keyring holds, as does a pin that is not a
fingerprint. With no pin the tag alone decides, as before; the timeline says signer=pinned
or signer=unpinned on every line.

`just up --offline` (or `UV_OFFLINE=1`, or `"up_offline": true` in ~/.claude-tts/config.json,
which is how a scheduled run on that machine gets it) builds from uv's cache alone: the build
backend and its dependencies must already be there, and no package index is asked. An empty
cache fails the install step and nothing changes. The timeline line says uv=offline or uv=online,
and a deploy line says which hatchling built it (hatchling=, "-" when uv did not name one).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

REPO = Path(__file__).resolve().parent.parent
LOGDIR = Path(os.environ["CLAUDE_TTS_UP_LOGDIR"]).expanduser() if os.environ.get("CLAUDE_TTS_UP_LOGDIR") else REPO / ".logs" / "just"
SIGNER_ENV = "CLAUDE_TTS_UP_SIGNER"
# gpg's status words for a signature that must never count, whatever key made it.
BAD_SIGNATURE = ("BADSIG", "ERRSIG", "EXPSIG", "EXPKEYSIG", "REVKEYSIG")
# Optional, gitignored: the path of a shared tool-versions ledger. When the file
# exists, every deploy appends one TSV row to the path it names, so machines
# that share the mount can tell their rooms an update is waiting (one line at
# the prompt, once per version). Append-only, no header, last row per tool wins:
# tool<TAB>version<TAB>at<TAB>subject<TAB>pull
LEDGER_POINTER = REPO / ".tool-versions-ledger"
TOOL = "claude-tts"
PULL_LINE = "uv tool install --force --build git+https://github.com/melderan/claude-code-tts@v{ver}"
HOME = Path.home()
TTS_DIR = HOME / ".claude-tts"
HEARTBEAT_MAX_AGE = 10.0
PREFERRED_PYTHON = "3.14"  # the newest interpreter in the CI matrix


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UnsafeLog(OSError):
    """A log path this script will not write through: a symlink, a non-file, or a second link."""


@contextmanager
def open_append(path: Path) -> Iterator[TextIO]:
    """Append to path without following a symlink at the file or at its directory.

    The directory is opened first with O_NOFOLLOW and the file is opened relative to it, so
    swapping either for a symlink between a check and the write changes nothing. O_NONBLOCK
    makes a planted FIFO fail at once instead of hanging the run; on a regular file it is
    a no-op. A file with a second hard link is refused too: an append through it lands in
    the other name.
    """
    try:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
    except OSError as e:
        raise UnsafeLog(f"{path.parent} is not a plain directory ({e.strerror})") from e
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path.name, flags, 0o644, dir_fd=dfd)
        except OSError as e:
            raise UnsafeLog(f"{path} is not a plain file ({e.strerror})") from e
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            os.close(fd)
            raise UnsafeLog(f"{path} is not a plain file with one link")
        with os.fdopen(fd, "a") as f:
            yield f
    finally:
        os.close(dfd)


def signer_file() -> Path:
    """Where the pinned release signer lives when the environment does not name one."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-tts" / "release-signer"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-tts" / "release-signer"


def pinned_signer() -> tuple[str | None, str | None]:
    """(fingerprint, None) when pinned, (None, None) when not, (None, why) for a pin that cannot be used."""
    raw = os.environ.get(SIGNER_ENV)
    where = SIGNER_ENV
    if raw is None:
        where = str(signer_file())
        try:
            raw = signer_file().read_text()
        except FileNotFoundError:
            return None, None
        except OSError as e:
            return None, f"release signer pin {where} unreadable ({e.strerror})"
    fpr = "".join(raw.split()).upper()
    if not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", fpr):
        return None, f"release signer pin in {where} is not a fingerprint"
    return fpr, None


def signature_hold(ver: str) -> str | None:
    """Why the tag's signature does not count, or None when it does or no signer is pinned.

    `git verify-tag` exits 0 for a good signature from any key in the keyring, so the
    VALIDSIG line's fingerprints (the signing key's and its primary key's) must hold the pin.
    """
    pin, problem = pinned_signer()
    if problem:
        return problem
    if pin is None:
        return None
    try:
        proc = subprocess.run(["git", "verify-tag", "--raw", f"v{ver}"], cwd=REPO, capture_output=True, text=True)
    except OSError as e:
        return f"tag v{ver} could not be verified ({e.strerror})"
    status = [line.split() for line in proc.stderr.splitlines() if line.startswith("[GNUPG:] ")]
    bad = sorted({s[1] for s in status if len(s) > 1 and s[1] in BAD_SIGNATURE})
    if proc.returncode != 0 or bad:
        said = ", ".join(bad) or next((line for line in proc.stderr.splitlines() if not line.startswith("[GNUPG:]")), "")
        return f"tag v{ver} does not verify (exit {proc.returncode}: {said or 'no reason given'})"
    signers = {fpr for s in status if len(s) > 2 and s[1] == "VALIDSIG" for fpr in (s[2], s[-1])}
    if pin not in signers:
        return f"tag v{ver} is signed by {', '.join(sorted(signers)) or 'no key'}, not the pinned signer {pin}"
    return None


def git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def version() -> str:
    text = (REPO / "src" / "claude_code_tts" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    return m.group(1) if m else "unknown"


def config_flag(path: list[str]) -> str:
    try:
        node: object = json.loads((TTS_DIR / "config.json").read_text())
        for key in path:
            node = node[key]  # type: ignore[index]
        return "on" if node else "off"
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return "off"


def tool_python() -> str:
    """The interpreter behind the installed claude-tts tool, from its shebang; '' if unknown."""
    tool = shutil.which("claude-tts")
    if not tool:
        return ""
    try:
        with open(tool, "rb") as f:
            first = f.readline().decode(errors="replace").strip()
    except OSError:
        return ""
    return first[2:] if first.startswith("#!") and "python" in first else ""


def tool_python_version() -> str:
    """'3.14.4' for the tool's interpreter, or '-' when it cannot be asked."""
    py = tool_python()
    if not py:
        return "-"
    try:
        out = subprocess.run([py, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "-"
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else "-"


def heartbeat_fresh() -> bool:
    try:
        return time.time() - (TTS_DIR / "daemon.heartbeat").stat().st_mtime < HEARTBEAT_MAX_AGE
    except OSError:
        return False


def daemon_state(ver: str) -> str:
    """What the timeline says about the daemon after a run: "running" only when the daemon
    alive now is the release this run installed; "DOWN" with no heartbeat; "STALE(v<x>)" when
    the heartbeat belongs to an older daemon, which is a restart that did not happen."""
    if not heartbeat_fresh():
        return "DOWN"
    have = daemon_version()
    if have == ver:
        return "running"
    return f"STALE(v{have or '?'})"


def daemon_pid() -> str:
    """The daemon's pid for the timeline: the pid file, else the lock file, else "-"."""
    for name in ("daemon.pid", "daemon.lock"):
        try:
            text = (TTS_DIR / name).read_text().strip()
        except OSError:
            continue
        if text.isdigit():
            return text
    return "-"


def installed_version() -> str:
    """The claude-tts on PATH, or "" when there is none."""
    try:
        out = subprocess.run(["claude-tts", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return ""
    return out.strip().rsplit(" ", 1)[-1] if out.strip() else ""


def daemon_version() -> str:
    """The release the running daemon wrote at start (daemon.release), or "" when unknown.

    Not daemon.version: that file is the control-protocol tag, "control-v1".
    """
    try:
        return (TTS_DIR / "daemon.release").read_text().strip()
    except OSError:
        return ""


def not_released(ver: str) -> str | None:
    """Why a scheduled run must hold, or None when the checkout is a release.

    A release is a clean working tree (git status reports nothing, .logs/ is ignored)
    whose HEAD carries the tag v<ver>, signed by the pinned signer when one is pinned.
    Anything else is work in progress that a person may still deploy by hand with a
    plain `just up`.
    """
    dirty = git("status", "--porcelain")
    if dirty not in ("", "unknown"):
        # Name the paths: a hold that only says "uncommitted changes" cannot be acted on from
        # anywhere but this machine (2026-10-09: a sandbox sharing the checkout saw a clean
        # tree while the host held for 15 minutes on something it could not see).
        lines = [line.strip() for line in dirty.splitlines() if line.strip()]
        shown = ", ".join(lines[:3]) + (f" and {len(lines) - 3} more" if len(lines) > 3 else "")
        return f"working tree has uncommitted changes: {shown}"
    tags = git("tag", "--points-at", "HEAD").split()
    if f"v{ver}" not in tags:
        return f"HEAD is not tagged v{ver}"
    return signature_hold(ver)


def unchanged(ver: str) -> str | None:
    """Why nothing needs doing, or None when a run is due.

    Installed CLI, running daemon and checkout must all be the same version and
    the daemon must be alive; anything else is a reason to run.
    """
    if installed_version() != ver:
        return None
    if daemon_version() != ver:
        return None
    if not heartbeat_fresh():
        return None
    return f"installed and running daemon are already v{ver}"


def tag_subject(ver: str) -> str:
    """The release tag's subject line, "v<ver> - <what changed>", or the bare tag when unknown."""
    out = git("tag", "-n1", "--format=%(contents:subject)", f"v{ver}")
    return out.strip() or f"v{ver}"


def ledger_row(ver: str, at: str | None = None) -> str:
    """One ledger row for this deploy; field 2 is the version, which readers pin."""
    fields = [TOOL, ver, at or stamp(), tag_subject(ver).replace("\t", " "), PULL_LINE.format(ver=ver)]
    return "\t".join(fields)


def announce(ver: str) -> str | None:
    """Append the deploy to the shared ledger the pointer file names; the path written, or None."""
    try:
        target = LEDGER_POINTER.read_text().strip()
    except OSError:
        return None
    if not target:
        return None
    path = Path(target).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open_append(path) as f:
            f.write(ledger_row(ver) + "\n")
    except OSError as e:
        print(f"up: ledger {path}: {e}", file=sys.stderr)
        return None
    return str(path)


def build_backend(run: Path) -> str:
    """The hatchling version the install step's build resolved, from uv's -v output in the
    run log, or "-" when it is not there. The last install section and its last selection win:
    a failed first command is followed by the one that built."""
    try:
        text = run.read_text(errors="replace")
    except OSError:
        return "-"
    section = re.split(r"^== \S+ install$", text, flags=re.M)[-1].split("\n== ", 1)[0]
    found = re.findall(r"Selecting: hatchling==(\S+)", section)
    return found[-1] if found else "-"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if_changed = "--if-changed" in args
    allow_dirty = "--dirty" in args
    unknown = [a for a in args if a not in ("--if-changed", "--dirty", "--offline")]
    if unknown:
        print(f"up: unknown argument(s): {' '.join(unknown)} (only --if-changed, --dirty, --offline)", file=sys.stderr)
        return 2
    # uv installs tools into ~/.local/bin, which a launchd or cron PATH does not have.
    # The run must not depend on who launched it, so put it first ourselves.
    os.environ["PATH"] = f"{Path.home() / '.local' / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
    # Offline, uv builds only from its cache and asks no index for the build backend, so the
    # run adds no code from a package index to the machine. A scheduled run has no flags to
    # pass, so the config key `up_offline` says it for that machine.
    if "--offline" in args or config_flag(["up_offline"]) == "on":
        os.environ["UV_OFFLINE"] = "1"
    uv_mode = "offline" if os.environ.get("UV_OFFLINE") not in (None, "", "0", "false") else "online"
    try:
        LOGDIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"up: refused, log directory {LOGDIR}: {e.strerror or e}", file=sys.stderr)
        return 1
    try:
        return _main(if_changed, allow_dirty, uv_mode)
    except UnsafeLog as e:
        print(f"up: refused, {e}; nothing was written through it", file=sys.stderr)
        return 1


def _main(if_changed: bool, allow_dirty: bool, uv_mode: str) -> int:
    ref = git("describe", "--always", "--dirty", "--tags")
    branch = git("branch", "--show-current") or "detached"
    ver = version()
    started = time.time()
    run = LOGDIR / f"up-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{ref}.log"
    timeline = LOGDIR / "timeline.log"
    # uv= and signer= on every line, held and unchanged ones too, so both are visible before a deploy.
    signer = "pinned" if pinned_signer()[0] else "unpinned"
    ident = f"up v{ver} {ref} {branch} signer={signer} uv={uv_mode}"

    def record(line: str) -> None:
        with open_append(timeline) as f:
            f.write(line + "\n")
        with open_append(run) as f:
            f.write(line + "\n")
        print(line)

    def step(name: str, *cmds: list[str]) -> None:
        """Run commands in order; the first that succeeds ends the step, all failing fails it."""
        with open_append(run) as f:
            f.write(f"== {stamp()} {name}\n")
            f.flush()
            for cmd in cmds:
                f.write(f"$ {' '.join(cmd)}\n")
                f.flush()
                proc = subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT)
                if proc.returncode == 0:
                    return
                f.write(f"(exit {proc.returncode})\n")
        record(f"{stamp()} {ident} FAILED at {name} run={run.name}")
        print(f"details: {run}")
        sys.exit(1)

    if if_changed:
        why = unchanged(ver)
        if why:
            # One timeline line, no run file: a scheduled run that found nothing to do
            # still shows it ran.
            with open_append(timeline) as f:
                f.write(f"{stamp()} {ident} unchanged ({why})\n")
            print(f"up: nothing to do, {why}")
            return 0
        hold = not_released(ver)
        if hold:
            with open_append(timeline) as f:
                f.write(f"{stamp()} {ident} held ({hold})\n")
            print(f"up: holding, {hold}")
            return 0
    elif not allow_dirty and git("status", "--porcelain") not in ("", "unknown"):
        # A plain run by hand on a tree someone is still editing (a sandbox shares this
        # checkout) put half-done work on the daemon on 2026-10-15; it asks for --dirty now.
        why = not_released(ver) or "working tree has uncommitted changes"
        with open_append(timeline) as f:
            f.write(f"{stamp()} {ident} refused ({why})\n")
        print(f"up: not installing, {why}; `just up --dirty` installs it anyway", file=sys.stderr)
        return 1

    print(f"up: v{ver} {ref} ({branch}) -> {run}")
    # --build: some uv configs refuse to build source distributions; harmless elsewhere.
    # --python: the daemon runs on the newest Python we test; a machine that cannot supply it
    # (no such interpreter, downloads forbidden) falls back to whatever uv finds, and the
    # timeline line records which one the tool landed on (py=).
    # -v: uv names the build backend it selected ("Selecting: hatchling==X"), which the
    # timeline line carries as hatchling=, so a pin in pyproject can match this machine's cache.
    step(
        "install",
        ["uv", "tool", "install", ".", "--force", "--build", "-v", "--python", PREFERRED_PYTHON],
        ["uv", "tool", "install", ".", "--force", "--build", "-v"],
    )
    # The installer would restart the daemon itself; we do the one restart below instead,
    # so the daemon finishes its current sentence once, not twice.
    step("hooks", ["claude-tts-install", "--upgrade", "--no-daemon-restart"])
    # No `daemon start` as a second try: next to a daemon the restart failed to stop it says
    # "already running" and exits 0, and the old daemon would pass as the new one (it did,
    # from 9.49.3 to 9.51.0: a missing pid file crashed every restart, 2026-10-09).
    step("restart", ["claude-tts", "daemon", "restart"])
    time.sleep(2)

    with open_append(run) as f:
        f.write(f"== {stamp()} verify\n")
        f.flush()
        subprocess.run(["claude-tts", "daemon", "status"], stdout=f, stderr=subprocess.STDOUT)

    daemon = daemon_state(ver)
    pid = daemon_pid()
    record(
        f"{stamp()} {ident} daemon={daemon} pid={pid} py={tool_python_version()} hatchling={build_backend(run)} "
        f"bridge={config_flag(['http', 'enabled'])} mic={config_flag(['mic_aware_pause'])} "
        f"took={int(time.time() - started)}s run={run.name}"
    )
    # The shared ledger tells other machines to pull v<ver>: only a release has that tag.
    if daemon == "running" and not_released(ver) is None:
        ledger = announce(ver)
        if ledger:
            with open_append(run) as f:
                f.write(f"== {stamp()} ledger row appended to {ledger}\n")
    return 0 if daemon == "running" else 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
