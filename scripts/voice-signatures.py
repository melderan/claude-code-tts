#!/usr/bin/env python3
"""voice-signatures.py - baseline and re-check what the real engines sound like (`just voices-*`).

Runs on the machine that owns the daemon and its engines. Baselines come from what the daemon
already keeps: every spoken WAV in ~/.claude-tts/speech_history with its text, persona and
speed in handy_analysis.db. Nothing here leaves the machine; the baseline holds the spoken
text, so it lives under ~/.claude-tts/signatures/, never in the repository.

    just voices-capture            sign every history WAV not yet in the baseline
    just voices-spread             synthesize the 20 shortest baselines 3 times each; record the spread
    just voices-verify             synthesize the 50 shortest baselines once; compare to their baselines

`verify` is the check to run before and after a release that touches the speech path: a
refactor that keeps every test green and still comes out quieter, faster, clipped or with a
pause missing shows here. Tolerances are the defaults widened by twice the spread `spread`
measured for that utterance. Engines differ: Kokoro on mlx repeats bit for bit, so a drift
there is a hard failure (exit 1); Piper samples noise and jitters its timing per run, so its
drift is reported as a note and does not fail the run (JMO, 2026-09-30: Piper is nice to
have). Which rule applies is read from the utterance's measured spread, not from the engine's
name. Standard library plus this package.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036  the point is to run on an older interpreter and say so
    sys.exit(
        f"voice-signatures needs Python 3.10+, this is {sys.version.split()[0]} ({sys.executable}); "
        "`just voices-*` picks python3.10+ from the PATH, or run it with python3.12 yourself"
    )

from claude_code_tts.signature import (  # noqa: E402
    VERSION,
    Signature,
    Tolerance,
    compare,
    sign,
    spread,
)

TTS_DIR = Path.home() / ".claude-tts"
HISTORY_DIR = TTS_DIR / "speech_history"
HISTORY_DB = TTS_DIR / "handy_analysis.db"
BASELINE_DIR = TTS_DIR / "signatures"


def history_rows(limit: int) -> list[dict]:
    if not HISTORY_DB.exists():
        sys.exit(f"no speech history database at {HISTORY_DB}")
    db = sqlite3.connect(f"file:{HISTORY_DB}?vfs=unix-dotfile&mode=ro", uri=True)
    rows = db.execute(
        "select file_name, persona, text, speed, created_at from speech_history "
        "where text is not null and persona is not null order by created_at desc limit ?",
        (limit,),
    ).fetchall()
    return [
        {"file": HISTORY_DIR / f, "persona": p, "text": t, "speed": s, "created_at": c}
        for f, p, t, s, c in rows
        if (HISTORY_DIR / f).exists()
    ]


def baseline_path(persona: str, sha: str) -> Path:
    return BASELINE_DIR / persona / f"{sha}.json"


def load_baselines(persona: str | None, max_seconds: float | None = None) -> list[dict]:
    """Baselines, shortest first: short utterances are the cheaper and steadier ruler."""
    out = []
    for p in sorted(BASELINE_DIR.glob("*/*.json")):
        if p.name.endswith(".spread.json"):
            continue
        if persona and p.parent.name != persona:
            continue
        b = json.loads(p.read_text())
        if max_seconds is not None and b["signature"]["seconds"] > max_seconds:
            continue
        out.append(b)
    out.sort(key=lambda b: b["signature"]["seconds"])
    return out


def cmd_capture(args: argparse.Namespace) -> int:
    added = skipped = 0
    for row in history_rows(args.limit):
        s = sign(row["file"], text=row["text"], voice=row["persona"])
        if s is None:
            print(f"skip {row['file'].name}: not a 16-bit WAV")
            continue
        path = baseline_path(row["persona"], s.text_sha)
        if path.exists() and not args.refresh:
            if json.loads(path.read_text())["signature"].get("version", 1) == VERSION:
                skipped += 1
                continue
            print(f"refresh {row['persona']}/{s.text_sha}: baseline is an older signature version")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "persona": row["persona"], "speed": row["speed"], "text": row["text"],
            "captured_at": time.time(), "source": row["file"].name,
            "signature": json.loads(s.to_json()),
        }, indent=1))
        added += 1
        print(f"baseline {row['persona']}/{s.text_sha}: {s.seconds:.1f}s, speech {s.speech_dbfs:.1f} dBFS, "
              f"{s.pauses} pauses  {row['text'][:60]!r}")
    print(f"capture: {added} added, {skipped} already known, baseline in {BASELINE_DIR}")
    return 0


def synthesize(text: str, persona: str, out: Path) -> bool:
    from claude_code_tts.daemon import daemon_generate_speech

    return daemon_generate_speech(text, persona, out)


def cmd_spread(args: argparse.Namespace) -> int:
    worst = Tolerance()
    done = 0
    with tempfile.TemporaryDirectory(prefix="voice-spread-") as tmp:
        for b in load_baselines(args.persona, args.max_seconds)[: args.limit]:
            if b["signature"].get("version", 1) != VERSION:
                print(f"skip {b['persona']}/{b['signature']['text_sha']}: older signature version, run `just voices-capture` to refresh")
                continue
            sigs = []
            for i in range(args.runs):
                wav = Path(tmp) / f"{b['signature']['text_sha']}-{i}.wav"
                if not synthesize(b["text"], b["persona"], wav):
                    print(f"skip {b['persona']}/{b['signature']['text_sha']}: synthesis failed")
                    break
                s = sign(wav, text=b["text"], voice=b["persona"])
                if s:
                    sigs.append(s)
            if len(sigs) < 2:
                continue
            sp = spread(sigs)
            path = baseline_path(b["persona"], b["signature"]["text_sha"]).with_suffix(".spread.json")
            path.write_text(json.dumps(sp.__dict__, indent=1))
            tol = sp.tolerance()
            worst = Tolerance(
                seconds_rel=max(worst.seconds_rel, tol.seconds_rel), seconds_abs=worst.seconds_abs,
                speech_db=max(worst.speech_db, tol.speech_db), peak_db=max(worst.peak_db, tol.peak_db),
                silence_s=max(worst.silence_s, tol.silence_s), pauses=max(worst.pauses, tol.pauses),
                envelope_dtw_db=max(worst.envelope_dtw_db, tol.envelope_dtw_db),
                envelope_dist_db=max(worst.envelope_dist_db, tol.envelope_dist_db),
                zcr_rel=max(worst.zcr_rel, tol.zcr_rel),
            )
            done += 1
            print(f"spread {b['persona']}/{b['signature']['text_sha']}: length {sp.seconds_rel:.1%}, "
                  f"speech {sp.speech_db:.2f} dB, shape {sp.envelope_dtw_db:.2f} dB warped, "
                  f"distribution {sp.envelope_dist_db:.2f} dB, brightness {sp.zcr_rel:.1%}")
    print(f"spread: {done} utterances x {args.runs} runs; widest tolerance needed: {worst}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    passed = drifted = advisory = failed = 0
    with tempfile.TemporaryDirectory(prefix="voice-verify-") as tmp:
        for b in load_baselines(args.persona, args.max_seconds)[: args.limit]:
            sha = b["signature"]["text_sha"]
            if b["signature"].get("version", 1) != VERSION:
                failed += 1
                print(f"FAIL {b['persona']}/{sha}: older signature version, run `just voices-capture` to refresh")
                continue
            expected = Signature.from_json(json.dumps(b["signature"]))
            wav = Path(tmp) / f"{sha}.wav"
            if not synthesize(b["text"], b["persona"], wav):
                failed += 1
                print(f"FAIL {b['persona']}/{sha}: synthesis failed")
                continue
            actual = sign(wav, text=b["text"], voice=b["persona"])
            if actual is None:
                failed += 1
                print(f"FAIL {b['persona']}/{sha}: no WAV")
                continue
            tol = Tolerance()
            deterministic = False  # only a measured spread can say so
            sp_path = baseline_path(b["persona"], sha).with_suffix(".spread.json")
            if sp_path.exists():
                from claude_code_tts.signature import Spread

                sp = Spread(**json.loads(sp_path.read_text()))
                tol = sp.tolerance()
                deterministic = sp.runs >= 2 and sp.seconds_rel == 0.0 and sp.envelope_dtw_db < 0.5
            diffs = compare(expected, actual, tol)
            if not diffs:
                passed += 1
                print(f"ok    {b['persona']}/{sha}: {actual.seconds:.1f}s, speech {actual.speech_dbfs:.1f} dBFS")
            elif deterministic:
                drifted += 1
                print(f"DRIFT {b['persona']}/{sha}: " + "; ".join(diffs) + f"  {b['text'][:50]!r}")
            else:
                advisory += 1
                print(f"note  {b['persona']}/{sha} (engine jitters, advisory): " + "; ".join(diffs))
    print(f"verify: {passed} ok, {drifted} drifted, {advisory} advisory, {failed} failed")
    return 1 if drifted or failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("capture", help="sign history WAVs into the baseline")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--refresh", action="store_true", help="re-sign utterances already in the baseline")
    p.set_defaults(fn=cmd_capture)
    p = sub.add_parser("spread", help="measure run-to-run spread per baseline utterance")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-seconds", type=float, default=30.0,
                   help="skip baselines longer than this (default 30; long ones cost minutes)")
    p.add_argument("--persona")
    p.set_defaults(fn=cmd_spread)
    p = sub.add_parser("verify", help="re-synthesize each baseline utterance and compare")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--max-seconds", type=float, default=30.0,
                   help="skip baselines longer than this (default 30)")
    p.add_argument("--persona")
    p.set_defaults(fn=cmd_verify)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
