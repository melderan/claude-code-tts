"""Release: a signed tag on HEAD's version, pushed, then watched until GitHub publishes it.

Every commit in this repo already bumps the version, so a release is not a bump. It is an
annotated tag, signed with the maintainer key, whose subject is ``v<version> - <summary>`` and
whose body becomes the release notes, pushed so ``release.yml`` can verify the signature and
publish the wheel. This module encodes that once, so nobody reads old tags to learn the shape.

Steps: preflight (main, clean, unreleased version, signing key) -> gate -> tag -> push -> verify
on GitHub -> wait for the release. ``--check`` stops after the gate and prints the plan.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Conventional-commit prefix on the HEAD subject; the release subject drops it.
_TYPE_PREFIX = re.compile(
    r"^(feat|fix|docs|chore|refactor|perf|test|style|ci|build)(\([^)]*\))?!?:\s*", re.IGNORECASE
)
# Git trailers (Co-Authored-By, Signed-off-by, ...) do not belong in release notes. A trailer
# block is the final paragraph when every line is "Key: value" and some key is hyphenated, so a
# prose paragraph such as "Why: ..." survives.
_TRAILER = re.compile(r"^([A-Za-z][A-Za-z-]*): \S.*$")
_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)

RELEASE_WAIT_S = 300
POLL_S = 10


def _repo_dir() -> Path:
    """Find the repo root: the package's own checkout, else walk up from cwd."""
    d = Path(__file__).resolve().parent.parent.parent
    if (d / "pyproject.toml").exists():
        return d
    d = Path.cwd()
    while d != d.parent:
        if (d / "pyproject.toml").exists():
            return d
        d = d.parent
    print("Could not find repo root (no pyproject.toml)")
    sys.exit(1)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    """Run git in repo and return stripped stdout."""
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def head_version(repo: Path) -> str:
    """The version HEAD carries, from the package's __init__.py."""
    text = (repo / "src" / "claude_code_tts" / "__init__.py").read_text()
    m = _VERSION_LINE.search(text)
    if not m:
        raise RuntimeError("no __version__ in src/claude_code_tts/__init__.py")
    return m.group(1)


def latest_tag_version(repo: Path) -> str | None:
    """Highest v* tag, as a bare version, or None when the repo has no release tags."""
    out = _git(repo, "tag", "-l", "v*", "--sort=-v:refname", check=False)
    for line in out.splitlines():
        if line.startswith("v"):
            return line[1:]
    return None


def tag_exists(repo: Path, tag: str) -> bool:
    return bool(_git(repo, "tag", "-l", tag, check=False))


def repo_slug(repo: Path) -> str | None:
    """owner/name from the origin URL, for gh; None when origin is not GitHub."""
    url = _git(repo, "remote", "get-url", "origin", check=False)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _is_trailer_block(paragraph: str) -> bool:
    matches = [_TRAILER.match(line.strip()) for line in paragraph.splitlines() if line.strip()]
    return bool(matches) and all(matches) and any("-" in m.group(1) for m in matches if m)


def notes_from_commit(subject: str, body: str) -> tuple[str, str]:
    """Summary and notes from a commit: drop the type prefix and a trailing trailer block."""
    summary = _TYPE_PREFIX.sub("", subject).strip()
    paragraphs = [para for para in re.split(r"\n\s*\n", body.strip()) if para.strip()]
    if paragraphs and _is_trailer_block(paragraphs[-1]):
        paragraphs.pop()
    return summary, "\n\n".join(para.strip() for para in paragraphs)


def build_notes(version: str, notes: str | None, subject: str, body: str) -> tuple[str, str]:
    """The tag subject and body: --notes when given, else from the HEAD commit."""
    if notes and notes.strip():
        first, _, rest = notes.strip().partition("\n")
        summary, text = first.strip(), rest.strip()
    else:
        summary, text = notes_from_commit(subject, body)
    return f"v{version} - {summary}", text


@dataclass
class Plan:
    """What a release would do, and why it must not."""

    version: str
    tag: str
    sha: str
    branch: str
    subject: str
    body: str
    problems: list[str] = field(default_factory=list)


def preflight(repo: Path, notes: str | None = None) -> Plan:
    """Check the tree and HEAD, and build the tag text. Problems are listed, not raised."""
    problems: list[str] = []
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if branch != "main":
        problems.append(f"on branch {branch!r}; releases are tagged on main")
    if _git(repo, "status", "--porcelain", check=False):
        problems.append("working tree is not clean; commit or stash first")
    version = head_version(repo)
    tag = f"v{version}"
    latest = latest_tag_version(repo)
    if tag_exists(repo, tag):
        problems.append(
            f"{tag} already exists; every commit bumps the version, so commit the change first"
        )
    elif latest == version:
        problems.append(f"HEAD is already released as {tag}")
    if not _git(repo, "config", "user.signingkey", check=False):
        problems.append(
            "no git user.signingkey; releases publish only for tags that verify against the "
            "maintainer key, so configure signing with that key first"
        )
    sha = _git(repo, "rev-parse", "--short", "HEAD", check=False)
    subject = _git(repo, "log", "-1", "--format=%s", check=False)
    body = _git(repo, "log", "-1", "--format=%b", check=False)
    tag_subject, tag_body = build_notes(version, notes, subject, body)
    return Plan(version, tag, sha, branch, tag_subject, tag_body, problems)


def run_gate(repo: Path) -> bool:
    """The full local gate (lint, types, version, tests, build) with its real exit code."""
    gate = repo / "scripts" / "gate.py"
    if not gate.exists():
        print("no scripts/gate.py; skipping gate")
        return True
    return subprocess.run([sys.executable, str(gate), "--full"], cwd=repo).returncode == 0


def create_tag(repo: Path, plan: Plan) -> None:
    """Signed annotated tag on HEAD, verified locally before anything is pushed."""
    args = ["tag", "-s", plan.tag, "-m", plan.subject]
    if plan.body:
        args += ["-m", plan.body]
    _git(repo, *args)
    r = subprocess.run(["git", "tag", "-v", plan.tag], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        _git(repo, "tag", "-d", plan.tag, check=False)
        raise RuntimeError(f"{plan.tag} did not verify locally: {r.stderr.strip()}")


def push(repo: Path, plan: Plan) -> None:
    """Push main, then the tag. A failed main push (the pre-push gate) leaves no tag behind."""
    r = subprocess.run(["git", "push", "origin", "main"], cwd=repo)
    if r.returncode != 0:
        _git(repo, "tag", "-d", plan.tag, check=False)
        raise RuntimeError("push of main failed; the tag was removed, fix and rerun")
    r = subprocess.run(["git", "push", "origin", plan.tag], cwd=repo)
    if r.returncode != 0:
        raise RuntimeError(f"push of {plan.tag} failed; main is pushed, push the tag by hand")


def _gh(*args: str) -> tuple[int, str]:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def verify_on_github(slug: str, plan: Plan, wait_s: int = RELEASE_WAIT_S) -> str | None:
    """Confirm GitHub sees the signature, then wait for the release. Returns its URL."""
    rc, out = _gh("api", f"repos/{slug}/commits/{plan.sha}", "--jq", ".commit.verification.verified")
    if rc == 0:
        print(f"GitHub signature check on {plan.sha}: {'verified' if out == 'true' else out}")
    else:
        print("could not ask GitHub about the commit signature (gh api failed)")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        rc, out = _gh("release", "view", plan.tag, "--json", "url", "--jq", ".url")
        if rc == 0 and out:
            return out
        rc, out = _gh(
            "run", "list", "--workflow", "release.yml", "--branch", plan.tag,
            "--json", "status,conclusion,url", "--limit", "1",
        )
        if rc == 0 and out:
            try:
                runs = json.loads(out)
            except json.JSONDecodeError:
                runs = []
            if runs and runs[0].get("status") == "completed" and runs[0].get("conclusion") != "success":
                raise RuntimeError(f"release workflow {runs[0].get('conclusion')}: {runs[0].get('url')}")
        time.sleep(POLL_S)
    return None


def print_plan(plan: Plan) -> None:
    print(f"tag:      {plan.tag} on {plan.sha} ({plan.branch})")
    print(f"subject:  {plan.subject}")
    if plan.body:
        print("notes:")
        for line in plan.body.splitlines():
            print(f"  {line}")
    for p in plan.problems:
        print(f"PROBLEM:  {p}")


def main(args: list[str] | None = None) -> None:
    """Release CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="claude-tts release",
        description="Tag HEAD's version (signed), push, and wait for GitHub to publish it.",
    )
    parser.add_argument("--check", action="store_true", help="preflight and gate only; print the plan")
    parser.add_argument(
        "--notes",
        help="release notes; first line is the summary, the rest the body (default: HEAD commit)",
    )
    parser.add_argument("--dry-run", action="store_true", help="everything but tag and push")
    parser.add_argument("--no-wait", action="store_true", help="push and return without waiting")
    parsed = parser.parse_args(args)
    repo = _repo_dir()

    plan = preflight(repo, parsed.notes)
    print_plan(plan)
    if plan.problems:
        sys.exit(1)
    if not shutil.which("gh") and not parsed.no_wait:
        print("gh not found; will push without waiting for the release")
        parsed.no_wait = True

    print()
    print("== gate", flush=True)  # subprocesses write straight through; keep the order
    if not run_gate(repo):
        print("gate failed; nothing tagged")
        sys.exit(1)
    if parsed.check or parsed.dry_run:
        print(f"\n{'check' if parsed.check else 'dry run'} ok: would tag {plan.tag} and push")
        return

    print(f"\n== tag {plan.tag}", flush=True)
    create_tag(repo, plan)
    print("\n== push", flush=True)
    push(repo, plan)
    slug = repo_slug(repo)
    if parsed.no_wait or not slug:
        print(f"\n{plan.tag} pushed; release.yml publishes it once the tag verifies")
        return
    print(f"\n== github ({slug})", flush=True)
    url = verify_on_github(slug, plan)
    if url:
        print(f"\nreleased {plan.tag}: {url}")
        print("operator: `just up` on the daemon's machine to run it")
    else:
        print(f"\n{plan.tag} pushed; release not visible after {RELEASE_WAIT_S}s: gh run list --workflow release.yml")
        sys.exit(2)


if __name__ == "__main__":
    main()
