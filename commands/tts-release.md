---
description: Push a release and upgrade the local installation
argument-hint: [--check|--dry-run|--no-wait|--notes TEXT]
disable-model-invocation: true
---

This is a two-step process:

## Step 1: Release (maintainer)

Every commit already bumps the version, so a release is a signed tag on HEAD, pushed, then
published by GitHub once the signature verifies. Run:

```bash
claude-tts release $ARGUMENTS
```

Common usage:
- `claude-tts release` - gate, signed tag `v<version>` with notes from the HEAD commit, push, wait
- `claude-tts release --check` - preflight and gate only; prints the plan
- `claude-tts release --notes "summary\nbody"` - release notes by hand
- `claude-tts release --no-wait` - push and return without waiting for GitHub

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
