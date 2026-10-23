"""Voice context mutating webhook -- enrich user messages with tone metadata.

Works like a Kubernetes mutating admission webhook: intercepts the user's
message text, identifies which segments came from Handy voice recordings,
and injects per-segment tone metadata. Typed text passes through unmarked.

Flow:
    1. Hook receives raw message text
    2. Pull recent transcripts and their tone from the analysis DB
    3. Match each transcript to a position in the message
    4. Keep the tone of each matched recording
    5. Output enriched context with per-segment metadata
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from claude_code_tts.handy import ANALYSIS_DB


def _get_recent_transcripts(
    max_age_seconds: float = 120.0,
    db_path: Path = ANALYSIS_DB,
) -> list[dict]:
    """Get recent analyzed recordings with their transcripts and tone, oldest first.

    Reads the daemon's analysis DB, not Handy's history.db: the analysis DB
    lives in ~/.claude-tts, which a sandbox sees through its mount, while
    Handy's own files exist only on the Mac that runs it.

    Returns list of {file_name, timestamp, text, tone} dicts.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        cutoff_ts = int(time.time() - max_age_seconds)
        rows = conn.execute(
            """SELECT file_name, timestamp, transcript, tone_summary
               FROM voice_analysis
               WHERE timestamp > ?
               ORDER BY timestamp ASC""",
            (cutoff_ts,),
        ).fetchall()
        conn.close()
        return [
            {"file_name": r[0], "timestamp": r[1], "text": r[2], "tone": r[3]}
            for r in rows
            if r[2] and r[2].strip()
        ]
    except sqlite3.Error:
        return []


def _normalize_for_matching(text: str) -> str:
    """Normalize text for fuzzy matching: lowercase, collapse whitespace."""
    return " ".join(text.lower().split())


def _find_transcript_in_message(
    transcript: str,
    message: str,
    message_normalized: str,
) -> tuple[int, int] | None:
    """Find where a transcript appears in the message.

    Uses a multi-strategy approach:
    1. Try full normalized substring match
    2. Try matching first 30 + last 30 chars (handles middle edits)
    3. Try matching first 50 chars (handles truncation)

    Returns (start, end) character positions in the original message,
    or None if no match found.
    """
    transcript_norm = _normalize_for_matching(transcript)

    # Strategy 1: Full substring match
    pos = message_normalized.find(transcript_norm)
    if pos >= 0:
        return (pos, pos + len(transcript_norm))

    # Strategy 2: First N + last N chars (handles edits in the middle)
    if len(transcript_norm) > 80:
        head = transcript_norm[:40]
        tail = transcript_norm[-40:]
        head_pos = message_normalized.find(head)
        tail_pos = message_normalized.find(tail)
        if head_pos >= 0 and tail_pos >= 0 and tail_pos > head_pos:
            return (head_pos, tail_pos + len(tail))

    # Strategy 3: First 50 chars (handles cases where Handy added/changed ending)
    if len(transcript_norm) > 50:
        prefix = transcript_norm[:50]
        pos = message_normalized.find(prefix)
        if pos >= 0:
            # Estimate end based on transcript length ratio
            approx_end = pos + len(transcript_norm)
            return (pos, min(approx_end, len(message_normalized)))

    # Strategy 4: First 25 chars (very short transcripts or heavy edits)
    if len(transcript_norm) > 25:
        prefix = transcript_norm[:25]
        pos = message_normalized.find(prefix)
        if pos >= 0:
            approx_end = pos + len(transcript_norm)
            return (pos, min(approx_end, len(message_normalized)))

    return None


def enrich_message(
    message: str,
    max_age_seconds: float = 120.0,
    db_path: Path = ANALYSIS_DB,
) -> str | None:
    """Enrich a user message with voice tone metadata.

    Matches recent Handy transcripts to segments in the message and
    returns context metadata for each matched segment. Returns None
    if no voice segments were found.

    Output format:
        [Voice context]
        [Segment 1: animated, fast pace (pitch 164Hz, rate 5.3 wps)]
        [Segment 2: quiet, deliberate (pitch 120Hz, rate 2.1 wps)]
    """
    transcripts = _get_recent_transcripts(max_age_seconds=max_age_seconds, db_path=db_path)
    if not transcripts:
        return None

    message_norm = _normalize_for_matching(message)

    # Match each transcript to its position in the message
    matched: list[tuple[int, dict, str | None]] = []  # (position, transcript_info, tone)
    for t in transcripts:
        span = _find_transcript_in_message(t["text"], message, message_norm)
        if span is not None:
            matched.append((span[0], t, t["tone"]))

    if not matched:
        return None

    # Sort by position in message
    matched.sort(key=lambda x: x[0])

    # Build output
    lines = ["[Voice context]"]
    for i, (_pos, _t_info, tone) in enumerate(matched, 1):
        if tone and tone != "neutral tone":
            # Get detailed features for this file
            features_str = tone
            lines.append(f"[Segment {i}: {features_str}]")

    # Only output if we have actual tone data (not just "neutral tone")
    meaningful = [line for line in lines if line != "[Voice context]"]
    if not meaningful:
        return None

    return "\n".join(lines)
