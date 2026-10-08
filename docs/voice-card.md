# Voice card: what a session sounds like, in one file a status line can read

A status line renders in a few milliseconds and must not shell out, so it cannot ask
`claude-tts` what this room sounds like. It also should not read `config.json` and
`sessions.d/` and redo the resolution chain, because the shape of those files belongs to
this project and changes. The voice card is the file it reads instead, and the only one.

## Where

```
~/.claude-tts/voice.d/<session>.json
```

`<session>` is the session id claude-tts uses everywhere: `$CLAUDE_TTS_SESSION` when that is set
(the kits set it to the room's name), else the Claude Code project folder name, which is the
project directory with every character that is not a letter or digit replaced by a dash
(`/Users/me/code/app` becomes `-Users-me-code-app`). A reader that has neither can read
`~/.claude-tts/active/<host>-<claude pid>.session`, the pin a hook writes for the `claude`
process it ran under; `<host>` is the machine's short host name with the same dash rule.

## What

```json
{
  "schema": 1,
  "session": "-Users-me-code-app",
  "persona": "claude-connery",
  "backend": "mlx",
  "voice": "mlx-community/Kokoro-82M-bf16:bm_george",
  "speed": 1.8,
  "muted": false,
  "intermediate": true,
  "mode": "queue",
  "written_at": "2026-10-05T22:50:12Z",
  "claude_tts": "9.48.0"
}
```

- `schema`: the shape of this file. A field may be added under the same number; a field is
  renamed, removed or changes meaning only with a new number. A reader checks it and shows
  nothing it does not understand.
- `name`: the name the session set with `claude-tts name`, spoken when it takes the floor; `""`
  when it has none (added in 9.51.0 under schema 1, so an older reader sees one extra key).
- `persona`: the persona name after the whole chain (session file, then `project_personas`,
  then the global default).
- `backend`: which engine the persona asks for, in the order the player chooses them:
  `kokoro` when a Kokoro voice or blend is set, else `mlx`, else `sherpa`, else `piper`. It
  says what is configured, not what played: a machine without swift-kokoro falls through at
  playback, and the card does not know that.
- `voice`: the engine's own name for the voice. Kokoro: the voice or blend string. mlx: the
  model id, then `:` and the speaker preset when one is set. Sherpa: the model directory name,
  then `:` and the speaker number when one is set. Piper: the model name.
- `speed`: the effective speed after session and environment overrides.
- `muted`, `intermediate`, `mode`: the session's effective values.
- `written_at`: UTC, second precision. `claude_tts`: the version that wrote it.

## When

The card is rewritten every time claude-tts resolves the session's config: at every hook
(so after every reply the room speaks or declines to), and at every `/tts-*` command that
changes the session file (`/tts-speed`, `/tts-persona`, `/tts-mute`, `/tts-unmute`,
`/tts-intermediate`). It is written in one step (a temp file, then rename), so a reader never
sees a half-written card. `claude-tts mute --all` and a global persona change rewrite every
session's card at that session's next hook, not at once; `written_at` says how old a card is.

A session that has never spoken and never been configured has no card. A reader shows nothing
in that case, never a guess.

## What it is not

It is not an input. Nothing reads the card back into claude-tts; editing it changes no voice.
Change a voice with `/tts-persona` or `/tts-speed`, and the card follows.

It carries no text, no transcript, no queue state. Who is speaking right now and how far
behind the queue is live in the daemon's `playback.json` and the bridge's `GET /queue`
(docs/http-bridge.md); the card is the quiet half, what this session would sound like.
