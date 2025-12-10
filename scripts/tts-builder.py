#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.50.0"]
# ///
"""
tts-builder.py - Interactive Persona Builder TUI

A beautiful terminal UI for creating TTS personas. Browse voices,
adjust speed in real-time, toggle methods, and save when satisfied.

Usage:
    tts-builder.py                    # Start the persona builder
    tts-builder.py --voice NAME       # Start with a specific voice
    tts-builder.py --multi            # Browse multi-speaker models only

Note: Uses uv's inline script metadata - dependencies auto-installed on first run.
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Optional

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal, Vertical, VerticalScroll
    from textual.reactive import reactive
    from textual.widgets import (
        Button,
        Footer,
        Header,
        Input,
        Label,
        ListItem,
        ListView,
        OptionList,
        Select,
        Static,
    )
    from textual.widgets.option_list import Option
except ImportError:
    print("Error: textual is required but not installed.")
    print("")
    print("Run this script with uv to auto-install dependencies:")
    print("  uv run tts-builder.py")
    print("")
    print("Or install manually:")
    print("  uv tool install textual")
    sys.exit(1)


# --- Configuration ---
HOME = Path.home()
VOICES_DIR = HOME / ".local" / "share" / "piper-voices"
CONFIG_FILE = HOME / ".claude-tts" / "config.json"
TEMP_FILE = Path("/tmp/tts_builder_preview.wav")

# Default test phrases
TEST_PHRASES = [
    "Hello! I'm auditioning for the role of your AI assistant. I hope you find my voice pleasant and easy to understand.",
    "The quick brown fox jumps over the lazy dog. This sentence contains every letter of the alphabet.",
    "Let me help you debug that tricky issue you've been working on. I can read your code and explain what's happening.",
    "According to my analysis, there are three potential approaches we could take here.",
]

SPEEDS = ["0.8", "1.0", "1.2", "1.4", "1.6", "1.8", "2.0", "2.2", "2.4", "2.6", "2.8", "3.0"]


def get_installed_voices() -> list[tuple[str, int]]:
    """Get list of installed voices with speaker counts."""
    voices = []
    if not VOICES_DIR.exists():
        return voices

    for f in VOICES_DIR.glob("*.onnx"):
        voice_name = f.stem
        json_file = VOICES_DIR / f"{voice_name}.onnx.json"
        num_speakers = 1

        if json_file.exists():
            try:
                with open(json_file) as jf:
                    data = json.load(jf)
                    num_speakers = data.get("num_speakers", 1)
            except (json.JSONDecodeError, IOError):
                pass

        voices.append((voice_name, num_speakers))

    return sorted(voices, key=lambda x: x[0])


def load_config() -> dict:
    """Load TTS config."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"personas": {}}


def save_config(config: dict) -> None:
    """Save TTS config."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


class VoiceInfo(Static):
    """Widget displaying current voice info."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.voice = ""
        self.speaker = None
        self.num_speakers = 1

    def update_voice(self, voice: str, speaker: Optional[int], num_speakers: int):
        self.voice = voice
        self.speaker = speaker
        self.num_speakers = num_speakers
        self._refresh()

    def _refresh(self):
        if not self.voice:
            self.update("[dim]No voice selected[/dim]")
            return

        text = f"[bold cyan]{self.voice}[/bold cyan]"
        if self.num_speakers > 1:
            speaker_text = str(self.speaker) if self.speaker is not None else "random"
            text += f" [dim](speaker {speaker_text} of {self.num_speakers})[/dim]"
        self.update(text)


class SettingsPanel(Static):
    """Widget displaying current settings."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.speed = "1.8"
        self.method = "playback"

    def update_settings(self, speed: str, method: str):
        self.speed = speed
        self.method = method
        self._refresh()

    def _refresh(self):
        method_desc = "faster playback" if self.method == "playback" else "natural pitch"
        self.update(
            f"[bold]Speed:[/bold] {self.speed}x  "
            f"[bold]Method:[/bold] {self.method} [dim]({method_desc})[/dim]"
        )


class PersonaBuilder(App):
    """TTS Persona Builder TUI."""

    CSS = """
    Screen {
        layout: grid;
        grid-size: 1;
        grid-rows: auto 1fr auto auto;
    }

    #title-bar {
        dock: top;
        height: 3;
        background: $primary;
        color: $text;
        text-align: center;
        padding: 1;
    }

    #main-container {
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        padding: 1;
    }

    #voice-panel {
        border: solid $primary;
        padding: 1;
        margin-right: 1;
    }

    #settings-panel {
        border: solid $secondary;
        padding: 1;
    }

    #voice-list {
        height: 100%;
        min-height: 10;
    }

    #status-bar {
        dock: bottom;
        height: 3;
        background: $surface;
        padding: 1;
    }

    #button-bar {
        dock: bottom;
        height: 3;
        layout: horizontal;
        align: center middle;
        padding: 1;
        background: $surface;
    }

    #button-bar Button {
        margin: 0 1;
    }

    .section-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }

    #speed-select, #method-select {
        width: 100%;
        margin-bottom: 1;
    }

    #speaker-container {
        margin-top: 1;
    }

    #speaker-input {
        width: 100%;
    }

    #test-text {
        width: 100%;
        height: 3;
    }

    .playing {
        background: $success 30%;
    }
    """

    BINDINGS = [
        Binding("p", "play", "Play"),
        Binding("n", "next_voice", "Next Voice"),
        Binding("r", "random_speaker", "Random Speaker"),
        Binding("s", "save", "Save Persona"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit"),
    ]

    # Reactive state
    current_voice: reactive[str] = reactive("")
    current_speaker: reactive[Optional[int]] = reactive(None)
    current_speed: reactive[str] = reactive("1.8")
    current_method: reactive[str] = reactive("playback")
    num_speakers: reactive[int] = reactive(1)
    is_playing: reactive[bool] = reactive(False)

    def __init__(self, initial_voice: str = "", multi_only: bool = False):
        super().__init__()
        self.initial_voice = initial_voice
        self.multi_only = multi_only
        self.voices = get_installed_voices()
        self.test_text = random.choice(TEST_PHRASES)
        self.play_process: Optional[subprocess.Popen] = None

        # Filter to multi-speaker only if requested
        if multi_only:
            self.voices = [(v, n) for v, n in self.voices if n > 1]

    def compose(self) -> ComposeResult:
        yield Static("TTS Persona Builder", id="title-bar")

        with Container(id="main-container"):
            # Left panel - voice selection
            with Vertical(id="voice-panel"):
                yield Label("Select Voice", classes="section-title")
                voice_options = []
                for voice, speakers in self.voices:
                    if speakers > 1:
                        voice_options.append(Option(f"{voice} ({speakers} speakers)", id=voice))
                    else:
                        voice_options.append(Option(voice, id=voice))
                yield OptionList(*voice_options, id="voice-list")

            # Right panel - settings
            with Vertical(id="settings-panel"):
                yield Label("Settings", classes="section-title")

                yield Label("Speed:")
                yield Select(
                    [(f"{s}x", s) for s in SPEEDS],
                    value="1.8",
                    id="speed-select",
                )

                yield Label("Method:")
                yield Select(
                    [
                        ("playback (faster, pitch shifts)", "playback"),
                        ("length_scale (natural pitch)", "length_scale"),
                    ],
                    value="playback",
                    id="method-select",
                )

                with Container(id="speaker-container"):
                    yield Label("Speaker ID:", id="speaker-label")
                    yield Input(placeholder="Enter speaker # or leave empty for random", id="speaker-input")

                yield Label("Test Text:", id="text-label")
                yield Input(value=self.test_text, id="test-text")

        # Status bar
        with Horizontal(id="status-bar"):
            yield VoiceInfo(id="voice-info")
            yield SettingsPanel(id="settings-info")

        # Button bar
        with Horizontal(id="button-bar"):
            yield Button("Play [P]", id="play-btn", variant="primary")
            yield Button("Next [N]", id="next-btn")
            yield Button("Random Speaker [R]", id="random-btn")
            yield Button("Save [S]", id="save-btn", variant="success")
            yield Button("Quit [Q]", id="quit-btn", variant="error")

        yield Footer()

    def on_mount(self) -> None:
        """Set up initial state."""
        # Hide speaker controls initially
        self.query_one("#speaker-container").display = False

        # Select initial voice if provided
        if self.initial_voice:
            for i, (voice, _) in enumerate(self.voices):
                if voice == self.initial_voice:
                    self.query_one("#voice-list", OptionList).highlighted = i
                    self._select_voice(voice)
                    break
        elif self.voices:
            # Select first voice
            voice, _ = self.voices[0]
            self._select_voice(voice)

    def _select_voice(self, voice: str) -> None:
        """Select a voice and update UI."""
        self.current_voice = voice

        # Find speaker count
        for v, speakers in self.voices:
            if v == voice:
                self.num_speakers = speakers
                break

        # Show/hide speaker controls
        speaker_container = self.query_one("#speaker-container")
        if self.num_speakers > 1:
            speaker_container.display = True
            self.current_speaker = random.randint(0, self.num_speakers - 1)
            self.query_one("#speaker-input", Input).value = str(self.current_speaker)
        else:
            speaker_container.display = False
            self.current_speaker = None

        # Update display
        self.query_one("#voice-info", VoiceInfo).update_voice(
            voice, self.current_speaker, self.num_speakers
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle voice selection."""
        voice = event.option.id
        if voice:
            self._select_voice(voice)

    def on_select_changed(self, event: Select.Changed) -> None:
        """Handle select changes."""
        if event.select.id == "speed-select":
            self.current_speed = str(event.value)
        elif event.select.id == "method-select":
            self.current_method = str(event.value)

        self.query_one("#settings-info", SettingsPanel).update_settings(
            self.current_speed, self.current_method
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Handle input changes."""
        if event.input.id == "speaker-input":
            try:
                speaker = int(event.value)
                if 0 <= speaker < self.num_speakers:
                    self.current_speaker = speaker
                    self.query_one("#voice-info", VoiceInfo).update_voice(
                        self.current_voice, self.current_speaker, self.num_speakers
                    )
            except ValueError:
                pass
        elif event.input.id == "test-text":
            self.test_text = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id
        if button_id == "play-btn":
            self.action_play()
        elif button_id == "next-btn":
            self.action_next_voice()
        elif button_id == "random-btn":
            self.action_random_speaker()
        elif button_id == "save-btn":
            self.action_save()
        elif button_id == "quit-btn":
            self.action_quit()

    def action_play(self) -> None:
        """Play current voice settings."""
        if not self.current_voice:
            self.notify("No voice selected", severity="warning")
            return

        # Kill any existing playback
        self._stop_playback()

        # Generate and play audio
        asyncio.create_task(self._play_voice())

    async def _play_voice(self) -> None:
        """Generate and play voice async."""
        voice_path = VOICES_DIR / f"{self.current_voice}.onnx"
        if not voice_path.exists():
            self.notify(f"Voice file not found: {voice_path}", severity="error")
            return

        self.is_playing = True
        self.query_one("#play-btn").add_class("playing")

        try:
            # Build piper command
            cmd = ["piper", "--model", str(voice_path)]

            if self.current_speaker is not None:
                cmd.extend(["--speaker", str(self.current_speaker)])

            if self.current_method == "length_scale":
                length_scale = 1.0 / float(self.current_speed)
                cmd.extend(["--length_scale", f"{length_scale:.2f}"])

            cmd.extend(["--output_file", str(TEMP_FILE)])

            # Generate audio
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate(input=self.test_text.encode())

            if not TEMP_FILE.exists():
                self.notify("Failed to generate audio", severity="error")
                return

            # Play audio
            if self.current_method == "playback":
                play_cmd = ["afplay", "-r", self.current_speed, str(TEMP_FILE)]
            else:
                play_cmd = ["afplay", str(TEMP_FILE)]

            self.play_process = await asyncio.create_subprocess_exec(*play_cmd)
            await self.play_process.wait()

        except Exception as e:
            self.notify(f"Playback error: {e}", severity="error")
        finally:
            self.is_playing = False
            self.query_one("#play-btn").remove_class("playing")
            if TEMP_FILE.exists():
                TEMP_FILE.unlink()

    def _stop_playback(self) -> None:
        """Stop any current playback."""
        if self.play_process:
            try:
                self.play_process.terminate()
            except ProcessLookupError:
                pass
            self.play_process = None

        # Also kill any afplay processes for our temp file
        try:
            subprocess.run(["pkill", "-f", "afplay.*tts_builder"], capture_output=True)
        except Exception:
            pass

    def action_next_voice(self) -> None:
        """Move to next voice in list."""
        voice_list = self.query_one("#voice-list", OptionList)
        current = voice_list.highlighted
        if current is not None and current < len(self.voices) - 1:
            voice_list.highlighted = current + 1
            voice, _ = self.voices[current + 1]
            self._select_voice(voice)
        elif self.voices:
            # Wrap to first
            voice_list.highlighted = 0
            voice, _ = self.voices[0]
            self._select_voice(voice)

    def action_random_speaker(self) -> None:
        """Select random speaker for multi-speaker models."""
        if self.num_speakers <= 1:
            self.notify("Not a multi-speaker model", severity="warning")
            return

        self.current_speaker = random.randint(0, self.num_speakers - 1)
        self.query_one("#speaker-input", Input).value = str(self.current_speaker)
        self.query_one("#voice-info", VoiceInfo).update_voice(
            self.current_voice, self.current_speaker, self.num_speakers
        )
        self.notify(f"Selected speaker #{self.current_speaker}")

    def action_save(self) -> None:
        """Save current settings as a persona."""
        if not self.current_voice:
            self.notify("No voice selected", severity="warning")
            return

        # Push a save screen
        self.push_screen(SavePersonaScreen(
            voice=self.current_voice,
            speaker=self.current_speaker,
            speed=self.current_speed,
            method=self.current_method,
        ))

    def action_quit(self) -> None:
        """Quit the application."""
        self._stop_playback()
        self.exit()


class SavePersonaScreen(App):
    """Screen for saving a persona."""

    CSS = """
    Screen {
        align: center middle;
    }

    #save-dialog {
        width: 60;
        height: auto;
        border: solid $primary;
        background: $surface;
        padding: 2;
    }

    #save-dialog Label {
        margin-bottom: 1;
    }

    #save-dialog Input {
        width: 100%;
        margin-bottom: 1;
    }

    #save-buttons {
        layout: horizontal;
        align: center middle;
        margin-top: 1;
    }

    #save-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, voice: str, speaker: Optional[int], speed: str, method: str):
        super().__init__()
        self.voice = voice
        self.speaker = speaker
        self.speed = speed
        self.method = method

    def compose(self) -> ComposeResult:
        with Container(id="save-dialog"):
            yield Label("Save as Persona")
            yield Label(f"Voice: {self.voice}" + (f" (speaker #{self.speaker})" if self.speaker else ""))
            yield Label(f"Speed: {self.speed}x, Method: {self.method}")
            yield Label("Persona name:")
            yield Input(placeholder="e.g., my-voice", id="persona-name")
            with Horizontal(id="save-buttons"):
                yield Button("Save", id="save-confirm", variant="success")
                yield Button("Cancel", id="save-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-confirm":
            name_input = self.query_one("#persona-name", Input)
            name = name_input.value.strip()

            if not name:
                self.notify("Please enter a name", severity="warning")
                return

            # Save to config
            config = load_config()

            persona = {
                "description": f"Created with persona builder - {self.voice}",
                "voice": self.voice,
                "speed": float(self.speed),
                "speed_method": self.method,
                "max_chars": 10000,
                "ai_type": "claude",
            }

            if self.speaker is not None:
                persona["speaker"] = self.speaker
                persona["description"] += f" speaker {self.speaker}"

            config.setdefault("personas", {})[name] = persona
            save_config(config)

            self.notify(f"Saved persona: {name}", severity="information")
            self.exit()

        elif event.button.id == "save-cancel":
            self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key in input."""
        if event.input.id == "persona-name":
            # Trigger save
            self.query_one("#save-confirm", Button).press()


def main():
    parser = argparse.ArgumentParser(description="TTS Persona Builder")
    parser.add_argument("--voice", type=str, help="Start with specific voice")
    parser.add_argument("--multi", action="store_true", help="Only show multi-speaker models")
    args = parser.parse_args()

    app = PersonaBuilder(
        initial_voice=args.voice or "",
        multi_only=args.multi,
    )
    app.run()


if __name__ == "__main__":
    main()
