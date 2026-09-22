# Security

## Reporting

Open a private security advisory on GitHub (Security tab, "Report a vulnerability") or email the
maintainer listed on the repository. Please do not open a public issue for something exploitable.

## What runs where

- The hooks run inside your Claude Code session and only write small JSON files under `~/.claude-tts/`.
- The daemon reads those files and runs a local speech engine (Piper, or sherpa-onnx if you enable it).
  Nothing leaves your machine; there is no telemetry and no network call after voice models download.
- The installer edits `~/.claude/settings.json` only to add or remove its own hook entries, backs the
  file up first, and writes atomically.

## Supply chain

- Every release tag is signed with the maintainer's GPG key (`.github/maintainer-key.asc`). The release
  workflow verifies the signature before it builds or publishes anything.
- GitHub Actions are pinned to full commit SHAs and run with a read-only token; the release job has
  `contents: write` only. No third-party actions beyond GitHub's own and `astral-sh/setup-uv`.
- Nothing is published to PyPI. Install from a tag:
  `uv tool install --build git+https://github.com/melderan/claude-code-tts@<tag>`.
- Runtime dependencies: none beyond the Python standard library.
