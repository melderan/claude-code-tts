---
description: Push a release and upgrade the local installation
argument-hint: [patch|minor|major|--check]
disable-model-invocation: true
---

This is a two-step process:

## Step 1: Release (maintainer)

Run the release workflow to bump version, commit, tag, and push:

```bash
claude-tts release $ARGUMENTS
```

Common usage:
- `claude-tts release patch` - Bug fix release
- `claude-tts release minor` - New feature release
- `claude-tts release major` - Breaking change release
- `claude-tts release --check` - Verify without releasing

## Step 2: Upgrade (install from GitHub)

After pushing, install the published version from GitHub and run the upgrade:

```bash
uv tool install git+https://github.com/melderan/claude-code-tts --force
claude-tts-install --upgrade
```

This is the same path real users follow. Never install from local source for production use — always install from the GitHub remote to prove the real upgrade path works.

## Verify

After upgrading, confirm everything is working:

```bash
claude-tts --version
claude-tts status
claude-tts daemon status
```

Summarize the results to the user at each step. Do not run Step 2 automatically after Step 1 — confirm with the user first.
