"""The spoken store: one claim per utterance, so a doubled event speaks once.

A claim is a file named by a digest, created with O_EXCL: of every process that tries to
claim one key, exactly one create succeeds. Removing an existing claim (stale takeover,
release, prune) happens only under the directory's flock, so nobody removes a fresh one. The hook claims in its own directory
($TMPDIR/claude-tts-spoken, local to the machine the hook runs on), the daemon in its state
directory; neither is a shared mount, where O_EXCL and rename are not to be trusted.

What a key names (the callers build it, this module only hashes):
  hook, Stop speaking its stdin's response: session id, the turn's prompt_id, the text.
    Twins carry the same stdin and share it; two turns that both end "ok" do not.
  hook, any other speech: session id, the assistant message id (else the transcript line),
    the text.
  daemon: the queue message's own id; (project, text) only for a message without one.

No text is kept: the file name is a sha256 and the file holds only the claim's TTL in
seconds and a random token naming the claimant. There is no sweeper. A claim older than its TTL is taken over at the next claim of
the same key (see _take_over), and a claim of a key starting "00" (one in 256) also prunes
claims older than PRUNE_AGE_S, so the directory stays bounded without a background thread:
the daemon's directory would otherwise gain one file per message for good.

Stdlib only; the hook imports it on every event.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HOOK_TTL_S = 30.0
DAEMON_MIN_TTL_S = 30.0
PRUNE_AGE_S = 3600.0
_HOOK_DIR_NAME = "claude-tts-spoken"
_LOCK_NAME = ".lock"


def normalize(text: str) -> str:
    """The one normalization: words joined by one space, case folded.

    Used by the claim key, the .pending record and the landed-line match, so a text compares
    equal however Claude Code wrapped it on the way to the hook input or the transcript.
    """
    return " ".join(text.split()).casefold()


def digest(*parts: str) -> str:
    """sha256 of the parts, NUL-separated (no part of a key contains NUL)."""
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def utterance_key(scope: str, message_key: str, text: str) -> str:
    """The key of one utterance: who said it (scope), which message or turn, and what."""
    return digest(scope, message_key, normalize(text))


def hook_dir() -> Path:
    """$TMPDIR/claude-tts-spoken, or /tmp/claude-tts-spoken when TMPDIR is unset or empty."""
    return Path(os.environ.get("TMPDIR") or "/tmp") / _HOOK_DIR_NAME


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """flock on the directory's lock file: held by everyone who removes an existing claim.

    The fast path (O_EXCL create of an absent claim) never takes it. Removers do: a takeover,
    a release, a prune. Under it, a claim file seen at the path stays that file until the
    holder moves it, since only holders remove claims and a create cannot replace an existing
    file. The kernel drops the lock with the process, so a crash leaves nothing stale.
    """
    fd = os.open(directory / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _moved_name(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}")


class Claim:
    """A won claim. release() gives it back so a retry can speak; refresh() restarts its TTL.

    The file holds "<ttl> <token>", the token random per claim. release() and refresh() act
    only while the file at the path still carries this claim's token, checked under the
    directory lock: a claimant that stalled past its TTL, whose claim another process took
    over, must not remove or extend that other claim (review of 1e92b8e).

    path is None for a claim granted without a file (the directory could not be used): the
    store fails open, since the watermark and .pending still guard most doubles, and silence
    is the worse failure.
    """

    def __init__(self, path: Path | None, token: str = "") -> None:
        self.path = path
        self.token = token

    @property
    def stored(self) -> bool:
        return self.path is not None

    def _still_ours(self) -> bool:
        assert self.path is not None
        return _read_claim(self.path)[1] == self.token

    def release(self) -> None:
        """Remove the claim if it is still ours, under the directory lock (see _locked)."""
        if self.path is None:
            return
        try:
            with _locked(self.path.parent):
                if self._still_ours():
                    self.path.unlink()
        except OSError:
            pass
        self.path = None

    def refresh(self, ttl: float) -> None:
        """Restart the TTL from now, with a new length (the daemon: after playback ends)."""
        if self.path is None:
            return
        try:
            with _locked(self.path.parent):
                if not self._still_ours():
                    return
                fd = os.open(self.path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
                with os.fdopen(fd, "w") as f:
                    f.write(f"{ttl:.3f} {self.token}\n")
        except OSError:
            return


def _usable_dir(directory: Path) -> bool:
    """Create the directory 0700 if missing; usable only if it is ours and not a symlink."""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(directory)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return False
    if stat.S_IMODE(st.st_mode) != 0o700:
        try:
            os.chmod(directory, 0o700)
        except OSError:
            return False
    return True


def _read_claim(path: Path) -> tuple[float | None, str]:
    """(ttl, token) of the claim file; (None, "") when it is missing or unreadable."""
    try:
        parts = path.read_text().split()
    except OSError:
        return None, ""
    try:
        ttl = float(parts[0]) if parts else None
    except ValueError:
        ttl = None
    return ttl, parts[1] if len(parts) > 1 else ""


def _ttl_of(path: Path, default: float) -> float:
    ttl = _read_claim(path)[0]
    return default if ttl is None else ttl


def _create(path: Path, ttl: float, token: str = "") -> bool:
    """O_EXCL create; True for the one process that made the file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(f"{ttl:.3f} {token}\n")
    return True


def _after_stale_stat() -> None:
    """Test seam: called between judging a claim stale and taking it over. Does nothing."""


def _take_over(path: Path, ttl: float, now: float, token: str = "") -> bool:
    """A claim exists at `path`. Take it over if it is older than its TTL; True if we won.

    Called with the directory unlocked; takes the lock. Under it: look again (the holder of
    the lock before us may have taken it over already, and its fresh claim refuses us); a
    stale claim is moved aside with rename(path, path.<pid>.<token>) and the moved copy
    removed, then the O_EXCL create decides between us and any fast-path claimant that saw
    the path empty in between. A rename that finds nothing (FileNotFoundError) means the
    claim vanished, and the create still lets exactly one process win.

    The lock is what makes this right, not the rename. Lock-free, a claimant that judged the
    file stale can rename away the fresh claim another claimant made after its own takeover,
    and both speak; rename-then-check-and-link-back narrows that window but leaves one
    (tests/test_spoken.py runs six threads on one stale file and saw three winners).
    """
    with _locked(path.parent):
        if _create(path, ttl, token):
            return True
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return _create(path, ttl, token)
        if now - st.st_mtime <= _ttl_of(path, ttl):
            return False
        _after_stale_stat()
        moved = _moved_name(path)
        try:
            os.rename(path, moved)
        except FileNotFoundError:
            return _create(path, ttl, token)
        try:
            moved.unlink()
        except OSError:
            pass
        return _create(path, ttl, token)


def prune(directory: Path, max_age_s: float = PRUNE_AGE_S, now: float | None = None) -> int:
    """Remove claims older than max_age_s, under the directory lock; the number removed."""
    t = time.time() if now is None else now
    removed = 0
    try:
        with _locked(directory):
            for e in list(os.scandir(directory)):
                if e.name == _LOCK_NAME or not e.is_file(follow_symlinks=False):
                    continue
                try:
                    if t - e.stat(follow_symlinks=False).st_mtime > max_age_s:
                        os.unlink(e.path)
                        removed += 1
                except OSError:
                    continue
    except OSError:
        return removed
    return removed


def claim(directory: Path, key: str, ttl: float, now: float | None = None) -> Claim | None:
    """Claim `key` for `ttl` seconds; the Claim, or None when another process holds it."""
    t = time.time() if now is None else now
    if not _usable_dir(directory):
        return Claim(None)
    path = directory / key
    token = secrets.token_hex(8)
    try:
        won = _create(path, ttl, token) or _take_over(path, ttl, t, token)
    except OSError:
        return Claim(None)
    if not won:
        return None
    if key.startswith("00"):
        prune(directory, now=t)
    return Claim(path, token)


def first_unusable_notice(directory: Path) -> bool:
    """True the first time this user is told `directory` cannot be used, False after.

    An unusable store (another user made /tmp/claude-tts-spoken first, say) turns dedupe off
    silently; the caller says so once at INFO. The marker sits beside the directory, per user.
    """
    marker = directory.parent / f".{directory.name}.unusable.{os.getuid()}"
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    except OSError:
        return False
    return True
