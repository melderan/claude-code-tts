# 10.0: the same voice, reworked inside

This is the plan for the 10.x line. The interfaces people use stay fixed; the inside is
cut along seams the code has been pointing at for a while. Read docs/what-and-why.md first:
its "What Must Stay True" section is the spec, and this document records where the code and
that spec disagreed when the work started.

## Fixed: what 10.0 must not change

- The `claude-tts` subcommands and flags, and the `/tts-*` slash commands in commands/.
- The hook shims in hooks/ and their contract (`claude-tts speak --from-hook --hook-type ...`).
- `~/.claude-tts/config.json` and `sessions.d/`: migrations allowed, silently and once.
- The queue directory: messages already queued by an older version still play.
- The HTTP bridge contract in docs/http-bridge.md.
- Everything in what-and-why.md, with the corrections listed below.

## How the plan was made

Three reviews of the code were run blind to each other, each asked for a module cut, the three
highest-value simplifications, what must not be rewritten, a migration order, and one thing the
maintainer had not noticed. All three found the same bug (the pause hotkey losing its message),
two reproduced it, and their module cuts agree on every line below. Where they disagreed the
disagreement is recorded as an open decision, not settled by fiat.

Two measuring tools were built before the first cut, so every step can be checked instead of
felt: `just test-audit` (which tests would notice a one-line change to the source) and voice
signatures (`just voices-verify` on the machine with the engines; committed pipeline
signatures in tests/signatures/ for CI). A step ships only when the audit shows no test lost
its teeth and the signatures show the sound unchanged.

## Found on the way in, fixed in 9.33.x

- `claude-tts pause` killed the player itself; the daemon saw a finished process and dropped
  the message. Manual pause never resumed. Reproduced five of five. (9.33.1)
- The one-piece playback path saved the interrupted position with the persona's speed method,
  not the effective one, so a sherpa persona at 2x saved twice the real position. (9.33.1)
- playback.json was read with a 10 000 byte buffer: a long interrupted message made it
  unreadable and the next write reset it. (9.33.2)
- Every writer of playback.json, config.json and sessions used one fixed temp name; four
  concurrent writers gave 375 failures in 1200 writes and a corrupt file. (9.33.2)
- The mic watcher reopened its log by recursion, one stack frame per rotation. (9.33.2)
- The Handy analyzer worked through its backlog on the daemon's thread before the daemon
  could start, while hooks read a stale heartbeat as a dead daemon. (9.33.2)

## The module cut

| Module | Responsibility | Comes from |
|---|---|---|
| `state.py` | The only reader and writer of playback.json, heartbeat, pid and lock, under one lock | daemon.py state functions, cli.py pause and status |
| `queue.py` | One message schema and one writer; scan, order, age, `PauseLedger`, control messages | audio.py write_queue_message, bridge.py write_bridge_message, daemon.py queue functions |
| `voice.py` | Resolve persona, message and session into one immutable voice, effective speed method computed once | daemon.py prepare_message and the interrupted-resume copy of it, audio.py defaults |
| `engines/` | One synthesize call per engine (piper, kokoro, sherpa, mlx), workers, levelling | audio.py generation, level.py |
| `player.py` | Pause-aware playback of a list of parts; position, rewind, trim, chime | daemon.py daemon_play_audio, play_sentences |
| `daemon.py` | The loop: startup reconciliation, poll, dispatch, control | daemon_loop, stream_message, Prefetch |
| `service.py` | start, stop, restart, status, launchd and systemd | daemon.py service functions |
| `bridge.py` | HTTP and the job registry only | unchanged |
| `cli/` | One file per command family; `hook.py` and `audition.py` with importable helpers | cli.py |
| `install/` | Deploy manifest, voices, service, migrations | install.py |
| `extras/` | Handy analysis and voice context, imported only when enabled | handy.py, voice_context.py |

## Simplifications, in the order they ship

1. **One state writer** (`state.py`). Four writers across processes and threads today.
   Shipped in 9.36.2 as a move: every read and write of the six files goes through
   `state.py`, daemon re-exports the functions. The paths themselves are not re-exported:
   a module constant imported into another module is a copy, so a test that patched
   `daemon.PLAYBACK_STATE_FILE` would have redirected nothing. Tests patch `state.X` and a
   patch aimed at the old name fails loudly. The hook's liveness check in
   `audio.daemon_healthy` stays separate on purpose: it resolves HOME on every call.
2. **One queue writer** (`queue.py`). Three today, each with its own field set; the hook
   writer sends fields the daemon never reads. An additive `"v": 1` field; a missing field
   reads as the old shape, so queued messages still play.
3. **Resolve the voice once** (`voice.py`). The interrupted-resume branch re-derives every
   voice field by hand, a copy of prepare_message; the speed-method slip fixed in 9.33.1 is
   the kind of drift two copies produce. `speed_method` has five different defaults today.
4. **One engine seam** (`engines/`). Eight per-engine voice fields in config and in every
   queue message collapse to one voice value; `generate_speech` stops being an if-chain over
   which field is non-empty.
5. **Move-only splits** of player, service, cli and install, behind re-exporting facades so
   the tests that patch names on `claude_code_tts.daemon` stay green until 10.0.
6. **Handy analysis out of the default path** (`extras/`): a macOS-only third-party
   integration whose output nothing in the daemon reads back; the CLI subcommands stay.
7. **One playback path.** The one-piece path (rewind by trimming the WAV) and the sentence
   path (resume at the cut sentence) both run in production today. Sentence becomes the
   default; one-piece stays behind a flag through 10.0 and goes in 10.1.

## Not rewritten, moved verbatim

Each of these was paid for once and is covered by a test that should be treated as the
specification: startup reconciliation (respawn marker, orphan player kill, resume window,
stale mic pause, bridge job re-registration); `PauseLedger`; graceful `stop_daemon`; restart
under a supervisor versus `execv`; the `paused_by` precedence between mic and person; the
50 ms play poll with the heartbeat on the clock; `play_sentences` slots and pass token;
prefetch compare-and-set; the hook watermark; heartbeat before pid for sandboxes.

## Open decisions

- **what-and-why.md said new sessions start muted.** They have spoken by default since
  9.23.0, and `claude-tts mute --all` flips the default. The document now says so.
- **what-and-why.md says speech degrades to direct mode when the daemon is down.** In queue
  mode the hook skips the text instead, after the watermark has already moved past it, so
  that text is gone. Proposed: queue the message anyway and let the daemon catch up when it
  returns (queue ageing already bounds how stale a message may play). To decide.
- **The bridge speaks unredacted text.** Hook and file paths redact secrets; `/speak` does
  not. Redaction changes character offsets, and marks carry offsets for page highlighting, so
  redaction has to happen where marks are built. To decide together with the bridge users.
- **Handy analysis default.** On by default today; proposed off, with the CLI unchanged.

## Release plan

Every step is one release, shippable and reversible on its own; the on-disk formats do not
change until 10.0, so going back to 9.x stays safe throughout. Version numbers follow the
usual rule (refactor and fix are patches, feat is minor) and 10.0.0 is the release that
removes the facades and the one-piece flag.
