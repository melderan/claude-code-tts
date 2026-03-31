"""Handy audio analyzer -- extract emotional metadata from voice recordings.

Watches Handy's recordings directory, analyzes WAV files for prosodic
features (energy, pitch, speaking rate, pauses), and stores results in
a SQLite database alongside the transcript text from Handy's history.

Zero runtime dependencies -- uses wave, struct, math, sqlite3 from stdlib.

The analysis gives Claude sessions context about HOW the user spoke,
not just WHAT they said. "JMO said this, and he was animated."
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


# --- Paths ---

HANDY_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "com.pais.handy"
HANDY_RECORDINGS_DIR = HANDY_APP_SUPPORT / "recordings"
HANDY_HISTORY_DB = HANDY_APP_SUPPORT / "history.db"
ANALYSIS_DB = Path.home() / ".claude-tts" / "handy_analysis.db"


# --- Data structures ---

@dataclass
class AudioFeatures:
    """Prosodic features extracted from a voice recording."""
    duration_seconds: float
    # Energy
    rms_energy: float           # Overall RMS energy (0.0-1.0 normalized)
    energy_variance: float      # How much energy varies (animated vs monotone)
    peak_energy: float          # Maximum RMS in any window
    # Pitch
    pitch_mean_hz: float        # Average fundamental frequency
    pitch_range_hz: float       # Max - min pitch (wider = more expressive)
    pitch_variance: float       # How much pitch varies
    # Rate
    speaking_rate_wps: float    # Words per second (estimated from transcript)
    # Pauses
    pause_count: int            # Number of silent gaps > 300ms
    pause_total_seconds: float  # Total silence duration
    pause_ratio: float          # Fraction of recording that is silence
    # Summary
    energy_label: str           # "low", "medium", "high"
    expressiveness_label: str   # "flat", "moderate", "animated"
    pace_label: str             # "slow", "moderate", "fast"


@dataclass
class AnalysisResult:
    """Complete analysis of a Handy recording."""
    file_name: str
    timestamp: int
    transcript: str
    features: AudioFeatures
    analyzed_at: float


# --- WAV analysis (zero deps) ---

def _read_pcm_samples(wav_path: Path) -> tuple[list[float], int]:
    """Read a WAV file and return normalized float samples and sample rate.

    Handles 16-bit mono PCM (Handy's format: 16kHz, 16-bit, mono).
    """
    with wave.open(str(wav_path), "rb") as w:
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        frame_rate = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if sample_width != 2:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # Unpack all samples
    fmt = f"<{n_frames * n_channels}h"
    int_samples = struct.unpack(fmt, raw)

    # If stereo, take left channel only
    if n_channels == 2:
        int_samples = int_samples[::2]

    # Normalize to -1.0..1.0
    max_val = 32768.0
    samples = [s / max_val for s in int_samples]
    return samples, frame_rate


def _rms(samples: list[float]) -> float:
    """Compute RMS energy of a sample window."""
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _windowed_rms(samples: list[float], sample_rate: int, window_ms: int = 30) -> list[float]:
    """Compute RMS energy in overlapping windows."""
    window_size = int(sample_rate * window_ms / 1000)
    hop = window_size // 2
    energies = []
    for i in range(0, len(samples) - window_size, hop):
        window = samples[i:i + window_size]
        energies.append(_rms(window))
    return energies


def _detect_pauses(
    energies: list[float],
    window_ms: int = 30,
    silence_threshold: float = 0.02,
    min_pause_ms: int = 300,
) -> tuple[int, float]:
    """Detect pauses (silent gaps) from windowed energy.

    Returns (pause_count, total_pause_seconds).
    """
    hop_ms = window_ms / 2
    min_windows = int(min_pause_ms / hop_ms)

    pause_count = 0
    total_pause_windows = 0
    current_silence = 0

    for energy in energies:
        if energy < silence_threshold:
            current_silence += 1
        else:
            if current_silence >= min_windows:
                pause_count += 1
                total_pause_windows += current_silence
            current_silence = 0

    # Trailing silence
    if current_silence >= min_windows:
        pause_count += 1
        total_pause_windows += current_silence

    total_seconds = total_pause_windows * (hop_ms / 1000.0)
    return pause_count, total_seconds


def _autocorrelation_pitch(
    samples: list[float],
    sample_rate: int,
    min_hz: float = 75.0,
    max_hz: float = 500.0,
) -> float:
    """Estimate pitch (F0) using autocorrelation on a single window.

    Returns estimated frequency in Hz, or 0.0 if no clear pitch found.
    """
    min_lag = int(sample_rate / max_hz)
    max_lag = int(sample_rate / min_hz)

    if len(samples) < max_lag + 1:
        return 0.0

    # Compute autocorrelation for relevant lags
    n = len(samples)
    best_lag = 0
    best_corr = 0.0

    # Normalize energy
    energy = sum(s * s for s in samples)
    if energy < 1e-10:
        return 0.0

    for lag in range(min_lag, min(max_lag + 1, n)):
        corr = sum(samples[i] * samples[i + lag] for i in range(n - lag))
        corr /= energy
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    # Require minimum correlation for a confident pitch estimate
    if best_corr < 0.3 or best_lag == 0:
        return 0.0

    return sample_rate / best_lag


def _estimate_pitch_contour(
    samples: list[float],
    sample_rate: int,
    window_ms: int = 40,
) -> list[float]:
    """Estimate pitch contour across the recording.

    Returns list of F0 estimates (Hz), one per window. 0.0 = unvoiced.
    """
    window_size = int(sample_rate * window_ms / 1000)
    hop = window_size // 2
    pitches = []

    for i in range(0, len(samples) - window_size, hop):
        window = samples[i:i + window_size]
        # Only estimate pitch in voiced regions (has energy)
        if _rms(window) > 0.02:
            pitch = _autocorrelation_pitch(window, sample_rate)
            pitches.append(pitch)
        else:
            pitches.append(0.0)

    return pitches


def analyze_wav(wav_path: Path, transcript: str = "") -> AudioFeatures:
    """Analyze a WAV file and extract prosodic features.

    Args:
        wav_path: Path to the WAV recording.
        transcript: The transcribed text (for word count / speaking rate).

    Returns:
        AudioFeatures with all extracted metrics.
    """
    samples, sample_rate = _read_pcm_samples(wav_path)
    duration = len(samples) / sample_rate

    # Energy analysis
    energies = _windowed_rms(samples, sample_rate)
    overall_rms = _rms(samples)
    peak_energy = max(energies) if energies else 0.0
    energy_var = 0.0
    if energies:
        mean_e = sum(energies) / len(energies)
        energy_var = math.sqrt(sum((e - mean_e) ** 2 for e in energies) / len(energies))

    # Pause detection
    pause_count, pause_total = _detect_pauses(energies)
    pause_ratio = pause_total / duration if duration > 0 else 0.0

    # Pitch analysis
    pitches = _estimate_pitch_contour(samples, sample_rate)
    voiced_pitches = [p for p in pitches if p > 0]
    pitch_mean = sum(voiced_pitches) / len(voiced_pitches) if voiced_pitches else 0.0
    pitch_min = min(voiced_pitches) if voiced_pitches else 0.0
    pitch_max = max(voiced_pitches) if voiced_pitches else 0.0
    pitch_range = pitch_max - pitch_min
    pitch_var = 0.0
    if voiced_pitches:
        pitch_var = math.sqrt(
            sum((p - pitch_mean) ** 2 for p in voiced_pitches) / len(voiced_pitches)
        )

    # Speaking rate
    word_count = len(transcript.split()) if transcript else 0
    speaking_time = duration - pause_total
    speaking_rate = word_count / speaking_time if speaking_time > 0 and word_count > 0 else 0.0

    # Labels
    if overall_rms > 0.12:
        energy_label = "high"
    elif overall_rms > 0.05:
        energy_label = "medium"
    else:
        energy_label = "low"

    # Expressiveness from pitch range + energy variance
    expressiveness_score = (pitch_range / 100.0) + (energy_var * 10.0)
    if expressiveness_score > 1.5:
        expressiveness_label = "animated"
    elif expressiveness_score > 0.7:
        expressiveness_label = "moderate"
    else:
        expressiveness_label = "flat"

    if speaking_rate > 3.5:
        pace_label = "fast"
    elif speaking_rate > 2.0:
        pace_label = "moderate"
    else:
        pace_label = "slow"

    return AudioFeatures(
        duration_seconds=round(duration, 2),
        rms_energy=round(overall_rms, 4),
        energy_variance=round(energy_var, 4),
        peak_energy=round(peak_energy, 4),
        pitch_mean_hz=round(pitch_mean, 1),
        pitch_range_hz=round(pitch_range, 1),
        pitch_variance=round(pitch_var, 2),
        speaking_rate_wps=round(speaking_rate, 2),
        pause_count=pause_count,
        pause_total_seconds=round(pause_total, 2),
        pause_ratio=round(pause_ratio, 3),
        energy_label=energy_label,
        expressiveness_label=expressiveness_label,
        pace_label=pace_label,
    )


def summarize_tone(features: AudioFeatures) -> str:
    """Generate a natural language summary of the speaker's tone.

    This is what gets injected into Claude sessions as context.
    """
    parts = []

    # Energy
    if features.energy_label == "high":
        parts.append("speaking with high energy")
    elif features.energy_label == "low":
        parts.append("speaking quietly")

    # Expressiveness
    if features.expressiveness_label == "animated":
        parts.append("animated and expressive")
    elif features.expressiveness_label == "flat":
        parts.append("steady and deliberate")

    # Pace
    if features.pace_label == "fast":
        parts.append("at a fast pace")
    elif features.pace_label == "slow":
        parts.append("at a measured pace")

    # Pauses
    if features.pause_ratio > 0.3:
        parts.append("with frequent pauses (thinking)")
    elif features.pause_count == 0 and features.duration_seconds > 5:
        parts.append("speaking without pause")

    if not parts:
        return "neutral tone"

    return ", ".join(parts)


# --- SQLite storage ---

def _init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize the analysis database."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS voice_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL UNIQUE,
            timestamp INTEGER NOT NULL,
            transcript TEXT NOT NULL,
            duration_seconds REAL,
            rms_energy REAL,
            energy_variance REAL,
            peak_energy REAL,
            pitch_mean_hz REAL,
            pitch_range_hz REAL,
            pitch_variance REAL,
            speaking_rate_wps REAL,
            pause_count INTEGER,
            pause_total_seconds REAL,
            pause_ratio REAL,
            energy_label TEXT,
            expressiveness_label TEXT,
            pace_label TEXT,
            tone_summary TEXT,
            analyzed_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_voice_analysis_timestamp
        ON voice_analysis(timestamp DESC)
    """)
    conn.commit()
    return conn


def store_analysis(result: AnalysisResult, db_path: Path = ANALYSIS_DB) -> None:
    """Store an analysis result in the database."""
    conn = _init_db(db_path)
    try:
        features = result.features
        tone_summary = summarize_tone(features)
        conn.execute(
            """INSERT OR REPLACE INTO voice_analysis (
                file_name, timestamp, transcript,
                duration_seconds, rms_energy, energy_variance, peak_energy,
                pitch_mean_hz, pitch_range_hz, pitch_variance,
                speaking_rate_wps, pause_count, pause_total_seconds, pause_ratio,
                energy_label, expressiveness_label, pace_label,
                tone_summary, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.file_name, result.timestamp, result.transcript,
                features.duration_seconds, features.rms_energy,
                features.energy_variance, features.peak_energy,
                features.pitch_mean_hz, features.pitch_range_hz, features.pitch_variance,
                features.speaking_rate_wps, features.pause_count,
                features.pause_total_seconds, features.pause_ratio,
                features.energy_label, features.expressiveness_label, features.pace_label,
                tone_summary, result.analyzed_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_tone(
    max_age_seconds: float = 30.0,
    db_path: Path = ANALYSIS_DB,
) -> Optional[str]:
    """Get the tone summary for the most recent analysis within max_age.

    Returns a natural language string like "speaking with high energy,
    animated and expressive, at a fast pace" or None if no recent analysis.
    """
    if not db_path.exists():
        return None
    conn = _init_db(db_path)
    try:
        cutoff = time.time() - max_age_seconds
        row = conn.execute(
            """SELECT tone_summary, analyzed_at FROM voice_analysis
               WHERE analyzed_at > ? ORDER BY analyzed_at DESC LIMIT 1""",
            (cutoff,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_recent_analysis(
    max_age_seconds: float = 60.0,
    db_path: Path = ANALYSIS_DB,
) -> Optional[AnalysisResult]:
    """Get the full analysis for the most recent recording within max_age."""
    if not db_path.exists():
        return None
    conn = _init_db(db_path)
    try:
        cutoff = time.time() - max_age_seconds
        row = conn.execute(
            """SELECT file_name, timestamp, transcript,
                      duration_seconds, rms_energy, energy_variance, peak_energy,
                      pitch_mean_hz, pitch_range_hz, pitch_variance,
                      speaking_rate_wps, pause_count, pause_total_seconds, pause_ratio,
                      energy_label, expressiveness_label, pace_label,
                      analyzed_at
               FROM voice_analysis
               WHERE analyzed_at > ? ORDER BY analyzed_at DESC LIMIT 1""",
            (cutoff,),
        ).fetchone()
        if not row:
            return None
        return AnalysisResult(
            file_name=row[0],
            timestamp=row[1],
            transcript=row[2],
            features=AudioFeatures(
                duration_seconds=row[3], rms_energy=row[4],
                energy_variance=row[5], peak_energy=row[6],
                pitch_mean_hz=row[7], pitch_range_hz=row[8],
                pitch_variance=row[9], speaking_rate_wps=row[10],
                pause_count=row[11], pause_total_seconds=row[12],
                pause_ratio=row[13], energy_label=row[14],
                expressiveness_label=row[15], pace_label=row[16],
            ),
            analyzed_at=row[17],
        )
    finally:
        conn.close()


def get_aggregated_tone(
    max_age_seconds: float = 60.0,
    db_path: Path = ANALYSIS_DB,
) -> Optional[str]:
    """Aggregate tone across all recent recordings.

    When the user speaks multiple Handy blocks before hitting enter,
    this averages the features across all of them for a single summary.
    Returns None if no recent analyses.
    """
    if not db_path.exists():
        return None
    conn = _init_db(db_path)
    try:
        cutoff = time.time() - max_age_seconds
        rows = conn.execute(
            """SELECT rms_energy, energy_variance, pitch_mean_hz, pitch_range_hz,
                      speaking_rate_wps, pause_count, pause_total_seconds,
                      duration_seconds
               FROM voice_analysis
               WHERE analyzed_at > ? ORDER BY analyzed_at DESC""",
            (cutoff,),
        ).fetchall()
        if not rows:
            return None

        n = len(rows)
        avg_energy = sum(r[0] for r in rows) / n
        avg_energy_var = sum(r[1] for r in rows) / n
        avg_pitch = sum(r[2] for r in rows) / n
        avg_pitch_range = sum(r[3] for r in rows) / n
        avg_rate = sum(r[4] for r in rows) / n
        total_pauses = sum(r[5] for r in rows)
        total_pause_secs = sum(r[6] for r in rows)
        total_duration = sum(r[7] for r in rows)

        # Build aggregate labels
        energy_label = "high" if avg_energy > 0.12 else "medium" if avg_energy > 0.05 else "low"
        expr_score = (avg_pitch_range / 100.0) + (avg_energy_var * 10.0)
        expr_label = "animated" if expr_score > 1.5 else "moderate" if expr_score > 0.7 else "flat"
        pace_label = "fast" if avg_rate > 3.5 else "moderate" if avg_rate > 2.0 else "slow"
        pause_ratio = total_pause_secs / total_duration if total_duration > 0 else 0

        features = AudioFeatures(
            duration_seconds=total_duration,
            rms_energy=round(avg_energy, 4),
            energy_variance=round(avg_energy_var, 4),
            peak_energy=0.0,
            pitch_mean_hz=round(avg_pitch, 1),
            pitch_range_hz=round(avg_pitch_range, 1),
            pitch_variance=0.0,
            speaking_rate_wps=round(avg_rate, 2),
            pause_count=total_pauses,
            pause_total_seconds=round(total_pause_secs, 2),
            pause_ratio=round(pause_ratio, 3),
            energy_label=energy_label,
            expressiveness_label=expr_label,
            pace_label=pace_label,
        )
        summary = summarize_tone(features)
        if n > 1:
            summary += f" (across {n} recordings)"
        return summary
    finally:
        conn.close()


# --- Handy integration ---

def get_handy_transcript(file_name: str) -> Optional[str]:
    """Look up the transcript for a recording in Handy's history DB."""
    if not HANDY_HISTORY_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(HANDY_HISTORY_DB))
        row = conn.execute(
            "SELECT transcription_text FROM transcription_history WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def get_handy_timestamp(file_name: str) -> int:
    """Look up the timestamp for a recording in Handy's history DB."""
    if not HANDY_HISTORY_DB.exists():
        return 0
    try:
        conn = sqlite3.connect(str(HANDY_HISTORY_DB))
        row = conn.execute(
            "SELECT timestamp FROM transcription_history WHERE file_name = ?",
            (file_name,),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def analyze_recording(wav_path: Path) -> Optional[AnalysisResult]:
    """Analyze a single Handy recording.

    Looks up the transcript from Handy's history DB, analyzes the WAV,
    and returns the complete result.
    """
    file_name = wav_path.name
    transcript = get_handy_transcript(file_name) or ""
    timestamp = get_handy_timestamp(file_name)

    try:
        features = analyze_wav(wav_path, transcript)
    except (ValueError, wave.Error, struct.error):
        return None

    return AnalysisResult(
        file_name=file_name,
        timestamp=timestamp,
        transcript=transcript,
        features=features,
        analyzed_at=time.time(),
    )


def analyze_all_recordings(
    recordings_dir: Path = HANDY_RECORDINGS_DIR,
    db_path: Path = ANALYSIS_DB,
) -> list[AnalysisResult]:
    """Analyze all WAV files in Handy's recordings directory.

    Skips files already analyzed (by file_name).
    """
    if not recordings_dir.exists():
        return []

    # Check what's already analyzed
    analyzed_names: set[str] = set()
    if db_path.exists():
        conn = _init_db(db_path)
        rows = conn.execute("SELECT file_name FROM voice_analysis").fetchall()
        analyzed_names = {row[0] for row in rows}
        conn.close()

    results = []
    for wav_file in sorted(recordings_dir.glob("*.wav")):
        if wav_file.name in analyzed_names:
            continue
        result = analyze_recording(wav_file)
        if result:
            store_analysis(result, db_path)
            results.append(result)

    return results


# --- Sidecar watcher ---

class HandyWatcher:
    """Watches Handy's recordings directory for new WAVs and analyzes them.

    Runs as a foreground process (designed for launchd or manual use).
    """

    def __init__(
        self,
        recordings_dir: Path = HANDY_RECORDINGS_DIR,
        db_path: Path = ANALYSIS_DB,
        poll_interval: float = 1.0,
    ) -> None:
        self.recordings_dir = recordings_dir
        self.db_path = db_path
        self.poll_interval = poll_interval
        self._known_files: set[str] = set()
        self._running = False

    def _scan_existing(self) -> None:
        """Record all currently-existing WAV files so we don't re-analyze."""
        if self.recordings_dir.exists():
            self._known_files = {f.name for f in self.recordings_dir.glob("*.wav")}

        # Also include already-analyzed files from DB
        if self.db_path.exists():
            conn = _init_db(self.db_path)
            rows = conn.execute("SELECT file_name FROM voice_analysis").fetchall()
            self._known_files.update(row[0] for row in rows)
            conn.close()

    def run(self) -> None:
        """Run the watcher loop. Blocks until interrupted."""
        self._running = True
        self._scan_existing()

        # Analyze any existing files that haven't been processed yet
        new_results = analyze_all_recordings(self.recordings_dir, self.db_path)
        if new_results:
            for r in new_results:
                tone = summarize_tone(r.features)
                print(f"[analyzed] {r.file_name}: {tone}")
                self._known_files.add(r.file_name)

        print(f"Watching {self.recordings_dir} for new recordings...")
        print(f"Analysis DB: {self.db_path}")
        print(f"Known files: {len(self._known_files)}")

        while self._running:
            try:
                if self.recordings_dir.exists():
                    current_files = {f.name for f in self.recordings_dir.glob("*.wav")}
                    new_files = current_files - self._known_files

                    for file_name in sorted(new_files):
                        wav_path = self.recordings_dir / file_name
                        # Wait briefly for Handy to finish writing the file
                        # and updating its history DB
                        time.sleep(0.5)
                        result = analyze_recording(wav_path)
                        if result:
                            store_analysis(result, self.db_path)
                            tone = summarize_tone(result.features)
                            print(f"[new] {file_name}: {tone}")
                            if result.transcript:
                                print(f"      \"{result.transcript[:80]}...\"")
                        self._known_files.add(file_name)

                time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                break

        self._running = False
        print("Watcher stopped.")

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False


class AnalyzerThread:
    """Background thread that watches Handy recordings and analyzes them.

    Designed to run inside the TTS daemon as a daemon thread, similar
    to MicWatcher. Polls the recordings directory for new WAVs.
    """

    def __init__(
        self,
        log_fn: Callable[..., Any],
        recordings_dir: Path = HANDY_RECORDINGS_DIR,
        db_path: Path = ANALYSIS_DB,
        poll_interval: float = 2.0,
    ) -> None:
        self._log = log_fn
        self._recordings_dir = recordings_dir
        self._db_path = db_path
        self._poll_interval = poll_interval
        self._known_files: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> bool:
        """Start the analyzer thread. Returns False if recordings dir not found."""
        if not self._recordings_dir.exists():
            self._log(f"Handy analyzer: recordings dir not found at {self._recordings_dir}", "WARN")
            return False

        # Seed known files from DB + directory
        self._known_files = {f.name for f in self._recordings_dir.glob("*.wav")}
        if self._db_path.exists():
            conn = _init_db(self._db_path)
            rows = conn.execute("SELECT file_name FROM voice_analysis").fetchall()
            self._known_files.update(row[0] for row in rows)
            conn.close()

        # Analyze any unprocessed files on startup
        new_results = analyze_all_recordings(self._recordings_dir, self._db_path)
        for r in new_results:
            self._known_files.add(r.file_name)
            self._log(f"Handy analyzer: {r.file_name} -> {summarize_tone(r.features)}")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="handy-analyzer",
            daemon=True,
        )
        self._thread.start()
        self._log(f"Handy analyzer started (watching {self._recordings_dir})")
        return True

    def stop(self) -> None:
        """Stop the analyzer thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._log("Handy analyzer stopped")

    def _watch_loop(self) -> None:
        """Poll for new recordings and analyze them."""
        while not self._stop_event.is_set():
            try:
                if self._recordings_dir.exists():
                    current = {f.name for f in self._recordings_dir.glob("*.wav")}
                    new_files = current - self._known_files
                    for file_name in sorted(new_files):
                        # Brief delay for Handy to finish writing
                        self._stop_event.wait(0.5)
                        if self._stop_event.is_set():
                            return
                        wav_path = self._recordings_dir / file_name
                        result = analyze_recording(wav_path)
                        if result:
                            store_analysis(result, self._db_path)
                            tone = summarize_tone(result.features)
                            self._log(f"Handy analyzer: {file_name} -> {tone}")
                        self._known_files.add(file_name)
            except Exception as e:
                self._log(f"Handy analyzer error: {e}", "ERROR")

            self._stop_event.wait(self._poll_interval)


# --- Speech history (TTS output recordings) ---

SPEECH_HISTORY_DIR = Path.home() / ".claude-tts" / "speech_history"
SPEECH_HISTORY_DB = Path.home() / ".claude-tts" / "handy_analysis.db"  # Same DB
DEFAULT_SPEECH_HISTORY_LIMIT = 50


def _init_speech_history_db(db_path: Path = SPEECH_HISTORY_DB) -> sqlite3.Connection:
    """Initialize the speech history table (shares DB with voice_analysis)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS speech_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            created_at REAL NOT NULL,
            session_id TEXT,
            project TEXT,
            persona TEXT,
            text TEXT,
            speed REAL,
            tone TEXT,
            duration_seconds REAL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_speech_history_created
        ON speech_history(created_at DESC)
    """)
    conn.commit()
    return conn


def save_speech_wav(
    wav_path: Path,
    session_id: str = "",
    project: str = "",
    persona: str = "",
    text: str = "",
    speed: float = 0.0,
    tone: str = "neutral",
    history_limit: int = DEFAULT_SPEECH_HISTORY_LIMIT,
    history_dir: Path = SPEECH_HISTORY_DIR,
    db_path: Path = SPEECH_HISTORY_DB,
) -> Optional[Path]:
    """Copy a generated WAV to speech history before playback.

    Returns the path to the history copy, or None on failure.
    Enforces history_limit by removing oldest files.
    """
    if not wav_path.exists():
        return None

    history_dir.mkdir(parents=True, exist_ok=True)

    # Name: timestamp_session.wav
    ts = time.time()
    safe_session = session_id.replace("/", "_")[:40] if session_id else "unknown"
    hist_name = f"tts-{ts:.3f}-{safe_session}.wav"
    hist_path = history_dir / hist_name

    try:
        shutil.copy2(wav_path, hist_path)
    except OSError:
        return None

    # Get duration
    duration = 0.0
    try:
        with wave.open(str(hist_path), "rb") as w:
            duration = w.getnframes() / w.getframerate()
    except (wave.Error, OSError):
        pass

    # Store metadata
    conn = _init_speech_history_db(db_path)
    try:
        conn.execute(
            """INSERT INTO speech_history
               (file_name, created_at, session_id, project, persona, text, speed, tone, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (hist_name, ts, session_id, project, persona, text[:500], speed, tone, duration),
        )
        conn.commit()

        # Enforce limit: delete oldest beyond threshold
        rows = conn.execute(
            "SELECT id, file_name FROM speech_history ORDER BY created_at DESC"
        ).fetchall()
        if len(rows) > history_limit:
            for row_id, old_name in rows[history_limit:]:
                old_path = history_dir / old_name
                old_path.unlink(missing_ok=True)
                conn.execute("DELETE FROM speech_history WHERE id = ?", (row_id,))
            conn.commit()
    finally:
        conn.close()

    return hist_path


def get_speech_history(
    limit: int = 20,
    db_path: Path = SPEECH_HISTORY_DB,
) -> list[dict]:
    """Get recent speech history entries."""
    if not db_path.exists():
        return []
    conn = _init_speech_history_db(db_path)
    try:
        rows = conn.execute(
            """SELECT file_name, created_at, session_id, project, persona,
                      text, speed, tone, duration_seconds
               FROM speech_history ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "file_name": r[0], "created_at": r[1], "session_id": r[2],
                "project": r[3], "persona": r[4], "text": r[5],
                "speed": r[6], "tone": r[7], "duration_seconds": r[8],
            }
            for r in rows
        ]
    finally:
        conn.close()
