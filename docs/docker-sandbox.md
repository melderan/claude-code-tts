# Voice from a Docker Sandbox

Run Claude Code inside a [Docker Sandbox](https://docs.docker.com/ai/sandboxes/) and still hear it.
The sandbox never plays audio itself. It writes small queue files into a directory shared with the
host, and the `claude-tts` daemon on the host turns them into speech. If the mount or the daemon is
missing, the hooks exit quietly and Claude Code carries on.

```
sandbox                                    host
Claude Code ── Stop / PostToolUse hook ──▶ ~/.claude-tts/queue/*.json ──▶ claude-tts daemon ──▶ speaker
                                    (mounted directory)
```

## Host, once

Install the daemon and a voice on the machine with the speaker:

```bash
uv tool install --build git+https://github.com/melderan/claude-code-tts
claude-tts-install                 # Piper, one voice, hooks for host sessions
claude-tts mode queue              # the daemon owns playback
claude-tts daemon install          # service (launchd on macOS, systemd on Linux)
claude-tts daemon status           # heartbeat must be fresh
```

Want normal speed rather than the default 2x? Set it once for every persona:

```bash
claude-tts speed --default 1.0
```

or pass `--default-speed 1.0` to `claude-tts-install` on a fresh install.

## Each sandbox

The kit in this repo, `kits/claude-tts`, extends the stock `claude` sandbox kit. It installs the
`claude-tts` queue writer, places the two hook shims, and registers them through an extra Claude
Code settings file, so the sandbox-managed `~/.claude/settings.json` is left alone.

```bash
# 1. Create the sandbox from the kit (pin the ref to a release tag)
sbx run "git+https://github.com/melderan/claude-code-tts.git#ref=v9.11.0&dir=kits/claude-tts" ~/code/my-project

# 2. Mount the host's tts directory into it (the whole directory, not only queue/)
sbx mount <sandbox-name> ~/.claude-tts:/home/agent/.claude-tts

# 3. Inside Claude Code, in that project, once:
/tts-unmute
```

The mount is added after creation and persists across stop and start. Mount the whole directory:
the hooks check `daemon.heartbeat` before writing, and personas and mute state live in
`config.json` and `sessions.d/`.

To pin a different release of the writer inside the sandbox:

```bash
sbx run "git+...&dir=kits/claude-tts" --kit-arg claude-tts.tts_version=v9.11.0 ~/code/my-project
```

Validate the kit locally before using it: `sbx kit validate kits/claude-tts`.

## Check it

| Symptom | Meaning | Fix |
|---|---|---|
| `Daemon not healthy, skipping speech` in `~/.claude-tts/debug.log` | no mount, or stale heartbeat | `sbx mount` the directory; `claude-tts daemon status` on the host |
| `muted, skipping` | the project is muted (default) | `/tts-unmute` in that project |
| a queue file sits in `~/.claude-tts/queue/` and never disappears | the daemon is not reading | `claude-tts daemon restart` on the host |
| `Voice ... is not installed` WARN in `daemon.log` | persona model missing on the host | `claude-tts-install --voice <name>` on the host |

The hook log is `~/.claude-tts/debug.log`, shared by the host and every sandbox; each line carries
the writer's hostname. `claude-tts status` inside the sandbox shows mute, persona and daemon state.

## Several sandboxes, one speaker

Each project can have its own persona, so different sandboxes sound different. On the host, map
the project's transcript folder name to a persona under `project_personas` in `~/.claude-tts/config.json`,
or run `/tts-persona <name>` inside the project. The daemon plays a short chime between speakers.

## What the kit does not do

It does not forward audio into the sandbox, install Piper there, or touch `~/.claude/settings.json`.
Everything that makes sound lives on the host.
