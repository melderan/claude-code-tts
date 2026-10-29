"""up.py's trust edges: a tag from the pinned signer only, and logs that never write through a link.

The signer tests run real gpg and git in a throwaway home: verify-tag exits 0 for a good
signature from any key in the keyring, and only a real keyring shows whether the pin is what
decides. They are skipped where gpg is missing.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "up.py"


def _load(name: str = "up_trust"):  # noqa: ANN202  (the script loaded as a module)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


up = _load()


@pytest.fixture(autouse=True)
def _no_pinned_signer(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(up.SIGNER_ENV, raising=False)
    missing = tmp_path_factory.mktemp("nopin") / "release-signer"
    monkeypatch.setattr(up, "signer_file", lambda: missing)


# --- the pinned signer -------------------------------------------------------------------


class Keys:
    def __init__(self, home: str, repo: Path, pinned: str, other: str) -> None:
        self.home, self.repo, self.pinned, self.other = home, repo, pinned, other


def _git(repo: Path, *args: str, env: dict[str, str]) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture(scope="module")
def keys() -> Iterator[Keys]:
    """Two signing keys and a repo whose tags v1 (pinned key), v2 (other key), v3 (unsigned) mark one commit."""
    if not shutil.which("gpg"):
        pytest.skip("gpg is not installed")
    # A short path: gpg-agent's socket path has a length limit that a pytest tmp path can pass.
    home = tempfile.mkdtemp(prefix="g")
    env = {
        **os.environ,
        "GNUPGHOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    fprs = []
    for who in ("pinned", "other"):
        subprocess.run(
            ["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-gen-key", f"{who} <{who}@example.com>", "ed25519", "sign", "never"],
            env=env, check=True, capture_output=True,
        )
        out = subprocess.run(["gpg", "--with-colons", "--list-keys", f"{who}@example.com"], env=env, check=True, capture_output=True, text=True).stdout
        fprs.append(next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")))
    repo = Path(home) / "r"
    repo.mkdir()
    _git(repo, "init", "-q", env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one", env=env)
    _git(repo, "-c", f"user.signingkey={fprs[0]}", "tag", "-s", "-m", "good", "v1", env=env)
    _git(repo, "-c", f"user.signingkey={fprs[1]}", "tag", "-s", "-m", "other key", "v2", env=env)
    _git(repo, "tag", "-a", "-m", "unsigned", "v3", env=env)
    yield Keys(home, repo, fprs[0], fprs[1])
    subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, capture_output=True)
    shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def signed_repo(keys: Keys, monkeypatch: pytest.MonkeyPatch) -> Keys:
    monkeypatch.setattr(up, "REPO", keys.repo)
    monkeypatch.setenv("GNUPGHOME", keys.home)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return keys


def test_a_tag_from_the_pinned_signer_counts(signed_repo: Keys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(up.SIGNER_ENV, signed_repo.pinned)
    assert up.signature_hold("1") is None


def test_a_tag_signed_by_a_second_key_holds(signed_repo: Keys, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify-tag calls this signature good; only the pin says it is the wrong signer."""
    monkeypatch.setenv(up.SIGNER_ENV, signed_repo.pinned)
    hold = up.signature_hold("2")
    assert hold is not None and signed_repo.other in hold and "not the pinned signer" in hold


def test_an_unsigned_tag_holds_once_a_signer_is_pinned(signed_repo: Keys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(up.SIGNER_ENV, signed_repo.pinned)
    hold = up.signature_hold("3")
    assert hold is not None and "does not verify" in hold


def test_the_pin_file_is_read_as_written_by_hand(signed_repo: Keys, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spaces and lower case, as gpg --fingerprint prints it, still name the key."""
    pin = tmp_path / "release-signer"
    fpr = signed_repo.pinned.lower()
    pin.write_text(" ".join(fpr[i : i + 4] for i in range(0, len(fpr), 4)) + "\n")
    monkeypatch.setattr(up, "signer_file", lambda: pin)
    assert up.pinned_signer() == (signed_repo.pinned, None)
    assert up.signature_hold("1") is None
    assert up.signature_hold("2") is not None


@pytest.mark.parametrize("pin", ["", "not-a-key", "ABCD" * 9])
def test_a_pin_that_is_not_a_fingerprint_holds(pin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(up.SIGNER_ENV, pin)
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: pytest.fail("no verify without a usable pin"))
    hold = up.signature_hold("1")
    assert hold is not None and "not a fingerprint" in hold


def test_no_pin_leaves_the_tag_to_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up.subprocess, "run", lambda *a, **k: pytest.fail("nothing to verify without a pin"))
    assert up.signature_hold("1") is None


def test_not_released_holds_a_tag_from_the_wrong_signer(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {("status", "--porcelain"): "", ("tag", "--points-at", "HEAD"): "v9.30.0"}
    monkeypatch.setattr(up, "git", lambda *a: answers[a])
    monkeypatch.setattr(up, "signature_hold", lambda ver: f"tag v{ver} is signed by X, not the pinned signer Y")
    assert "not the pinned signer" in (up.not_released("9.30.0") or "")


def test_the_timeline_says_whether_a_signer_is_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(up, "LOGDIR", tmp_path)
    monkeypatch.setattr(up, "unchanged", lambda ver: None)
    monkeypatch.setattr(up, "not_released", lambda ver: "HEAD is not tagged v9.30.0")
    monkeypatch.setattr(up, "git", lambda *a: "abc123")
    monkeypatch.setattr(up, "version", lambda: "9.30.0")
    assert up.main(["--if-changed"]) == 0
    monkeypatch.setenv(up.SIGNER_ENV, "A" * 40)
    assert up.main(["--if-changed"]) == 0
    lines = (tmp_path / "timeline.log").read_text().splitlines()
    assert " signer=unpinned " in lines[0] and " signer=pinned " in lines[1]


# --- logs that never write through a link ------------------------------------------------


def test_open_append_appends_to_a_plain_file(tmp_path: Path) -> None:
    log = tmp_path / "timeline.log"
    for line in ("one\n", "two\n"):
        with up.open_append(log) as f:
            f.write(line)
    assert log.read_text() == "one\ntwo\n"


def test_a_symlinked_log_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "timeline.log").symlink_to(target)
    with pytest.raises(up.UnsafeLog), up.open_append(logs / "timeline.log") as f:
        f.write("planted\n")
    assert target.read_text() == "keep\n"


def test_a_symlinked_log_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "logs").symlink_to(real)
    with pytest.raises(up.UnsafeLog), up.open_append(tmp_path / "logs" / "timeline.log") as f:
        f.write("planted\n")
    assert list(real.iterdir()) == []


def test_a_hard_linked_log_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    os.link(target, tmp_path / "timeline.log")
    with pytest.raises(up.UnsafeLog), up.open_append(tmp_path / "timeline.log") as f:
        f.write("planted\n")
    assert target.read_text() == "keep\n"


def test_a_planted_fifo_is_refused_without_hanging(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "timeline.log")
    with pytest.raises(up.UnsafeLog), up.open_append(tmp_path / "timeline.log") as f:
        f.write("planted\n")


def test_main_refuses_a_planted_timeline_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "timeline.log").symlink_to(target)
    monkeypatch.setattr(up, "LOGDIR", logs)
    monkeypatch.setattr(up, "unchanged", lambda ver: "already v9.30.0")
    monkeypatch.setattr(up, "git", lambda *a: "abc123")
    monkeypatch.setattr(up, "version", lambda: "9.30.0")
    assert up.main(["--if-changed"]) == 1
    assert "refused" in capsys.readouterr().err
    assert target.read_text() == "keep\n"


def test_announce_never_writes_through_a_planted_ledger_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    ledger = tmp_path / "host" / "tool-versions.tsv"
    ledger.parent.mkdir()
    ledger.symlink_to(target)
    pointer = tmp_path / ".tool-versions-ledger"
    pointer.write_text(f"{ledger}\n")
    monkeypatch.setattr(up, "LEDGER_POINTER", pointer)
    monkeypatch.setattr(up, "git", lambda *a: "v9.38.2 - x")
    assert up.announce("9.38.2") is None
    assert target.read_text() == "keep\n"


def test_the_log_directory_can_live_outside_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_TTS_UP_LOGDIR", str(tmp_path / "up-logs"))
    assert _load("up_logdir").LOGDIR == tmp_path / "up-logs"
    monkeypatch.delenv("CLAUDE_TTS_UP_LOGDIR")
    assert _load("up_logdir_default").LOGDIR == SCRIPT.parent.parent / ".logs" / "just"
