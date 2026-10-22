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
- To check a tag yourself before installing it: `gpg --import .github/maintainer-key.asc`, then
  `git verify-tag <tag>` in a clone. A tag that does not verify is not ours.

## Downloads

A valid HTTPS certificate is not proof that a file is the one we meant. Every model the installer
fetches is checked against a SHA-256 written in this repository, and a file that differs is deleted
and refused:

- Piper voices: `src/claude_code_tts/piper_pins.py`, all from one commit of `rhasspy/piper-voices`
  (not a tag, which can move). `scripts/pin-piper-voices.py` regenerates it and cross-checks each
  small file against the Git blob id Hugging Face lists.
- sherpa-onnx models: `src/claude_code_tts/sherpa_catalog.py`.

Not yet pinned: the Python packages installed into the optional mlx and sherpa venvs, the spaCy model
wheel the mlx venv uses, and the mlx models `huggingface_hub` fetches. These still rely on HTTPS.

`claude-tts verify` (or `/tts-verify`) checks a machine locally and sends nothing: it re-hashes
installed voices against the pins, and looks for a distrusted CA root, by fingerprint, in the CA
bundles curl and Python read and in certifi inside the engine venvs. Exit 0 means nothing is
affected, 1 means something is (each line names its fix), 2 means a check could not run.

## Advisories

### eMudhra (emSign) intermediate CA keys, October 2026

Eight intermediate CA certificates of eMudhra, an Indian certificate authority, had private keys
small enough to recover from the public certificates; one was the number 4. Reported on 2026-10-17,
the keys had been recoverable for about 17 months, and anyone holding one could issue a certificate
for any host. Browsers blocked the intermediates within days, and Chrome removes the root that chains
them, emSign ECC Root CA - G3 (SHA-256 `86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b`),
on 2026-10-27. Related fixes: CVE-2026-111943 (Go `crypto/x509`), CVE-2026-113470 (Firefox), certifi
2026.10.18 (the first certifi without the root).

What it means here: no misuse is known. Our downloads trust the machine's CA bundle, and an OpenSSL
bundle (curl on Linux, Homebrew's Python) or an old certifi does not see a browser's block. Voices
installed before the pins existed can now be checked against them. Run `claude-tts verify`. If it
reports:

- a voice as affected: delete that file and run `claude-tts-install --voice NAME` again.
- a store as affected: update your CA certificates (`brew upgrade ca-certificates`, or your
  distribution's `ca-certificates` package).
- a venv as affected: run the `uv pip install ... --upgrade 'certifi>=2026.10.18'` line it prints. The
  floor matters: an index that holds back new releases would otherwise install an older certifi that
  still carries the root, and the install would report success.
- a macOS keychain note: the root is still in the system keychain; Apple announced its block of the
  intermediates on 2026-10-19, so keep macOS current.
