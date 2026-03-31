"""Voice context mutating webhook -- enrich user messages with tone metadata.

Works like a Kubernetes mutating admission webhook: intercepts the user's
message text, identifies which segments came from Handy voice recordings,
and injects per-segment tone metadata. Typed text passes through unmarked.

Flow:
    1. Hook receives raw message text
    2. Pull recent transcripts from Handy's history.db
    3. Match each transcript to a position in the message
    4. Pull tone analysis for each matched recording
    5. Output enriched context with per-segment metadata
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from claude_code_tts.handy import (
    ANALYSIS_DB,
    HANDY_HISTORY_DB,
)


def _get_recent_transcripts(
    max_age_seconds: float = 120.0,
) -> list[dict]:
    """Get recent Handy transcripts with their file names, ordered by timestamp.

    Returns list of {file_name, timestamp, text} dicts.
    """
    if not HANDY_HISTORY_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(HANDY_HISTORY_DB))
        cutoff_ts = int(time.time() - max_age_seconds)
        rows = conn.execute(
            """SELECT file_name, timestamp, transcription_text
               FROM transcription_history
               WHERE timestamp > ?
               ORDER BY timestamp ASC""",
            (cutoff_ts,),
        ).fetchall()
        conn.close()
        return [
            {"file_name": r[0], "timestamp": r[1], "text": r[2]}
            for r in rows
            if r[2] and r[2].strip()
        ]
    except sqlite3.Error:
        return []


def _get_tone_for_file(
    file_name: str,
    db_path: Path = ANALYSIS_DB,
) -> Optional[str]:
    """Get the tone summary for a specific recording file."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT tone_summary FROM voice_analysis WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


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
    transcripts = _get_recent_transcripts(max_age_seconds=max_age_seconds)
    if not transcripts:
        return None

    message_norm = _normalize_for_matching(message)

    # Match each transcript to its position in the message
    matched: list[tuple[int, dict, str | None]] = []  # (position, transcript_info, tone)
    for t in transcripts:
        span = _find_transcript_in_message(t["text"], message, message_norm)
        if span is not None:
            tone = _get_tone_for_file(t["file_name"], db_path)
            matched.append((span[0], t, tone))

    if not matched:
        return None

    # Sort by position in message
    matched.sort(key=lambda x: x[0])

    # Build output
    lines = ["[Voice context]"]
    for i, (pos, t_info, tone) in enumerate(matched, 1):
        if tone and tone != "neutral tone":
            # Get detailed features for this file
            features_str = tone
            lines.append(f"[Segment {i}: {features_str}]")

    # Only output if we have actual tone data (not just "neutral tone")
    meaningful = [line for line in lines if line != "[Voice context]"]
    if not meaningful:
        return None

    return "\n".join(lines)
