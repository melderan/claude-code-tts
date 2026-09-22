# HTTP bridge: reading a web page aloud in the house voice

The daemon is fed by files. Hooks write JSON into `~/.claude-tts/queue/` and the daemon plays
them in order. A page in a browser cannot write files, so the daemon can open a small HTTP
surface on loopback that does the same thing a hook does, and reports back where each message
is in its life so the page can highlight the words as they are spoken.

It is off by default. It is a listening socket on your machine, even if only on `127.0.0.1`,
so it stays off until you turn it on, and every request needs a bearer token.

## Turn it on

```bash
claude-tts bridge enable            # writes "http": {"enabled": true, ...} to config.json
claude-tts bridge token             # prints the token; give it to your userscript
claude-tts daemon restart
claude-tts bridge status
```

Config block, with defaults:

```json
"http": {
  "enabled": false,
  "port": 7457,
  "bind": "127.0.0.1",
  "allowed_origins": []
}
```

The token lives in `~/.claude-tts/http-token`, mode 600, created on first enable.

## Who can call it

A page loaded from a normal website is subject to two gates before any request lands here:

- **Its own Content Security Policy.** The claude.ai artifact viewer blocks every `fetch`, XHR
  and WebSocket to any host outside the page's origin, loopback included. A script inline in an
  artifact cannot reach the bridge at all. A userscript (Violentmonkey, Tampermonkey) can, because
  `GM_xmlhttpRequest` runs in the extension and is not bound by the page's CSP.
- **CORS.** If a page does call directly, the bridge answers `OPTIONS` and echoes the `Origin`
  only when it is in `allowed_origins` (`claude-tts bridge allow-origin https://example.com`).
  Requests carrying an `Origin` not on the list get `403`. A userscript sends no `Origin`, so the
  list is empty by default.

Either way the token is required. Missing or wrong: `401`.

## Routes

All bodies and responses are JSON. Header on every request:
`Authorization: Bearer <token>`.

### `GET /health`

```json
{"ok": true, "version": "9.12.0"}
```

If the daemon is not running the port is closed and the connection is refused; there is no
separate "daemon down" answer because the bridge lives inside the daemon.

### `GET /voices`

```json
{"active": "claude-connery",
 "personas": {"claude-connery": {"description": "Northern English, warm and dry", "speed": 1.8}}}
```

### `POST /speak`

```json
{"text": "One block of prose. Usually a paragraph, a heading, or a list item.",
 "persona": "claude-connery",
 "source": "artifact",
 "label": "PR 1223 cascade",
 "want_marks": true}
```

- `text` is required. Longer than the persona's `max_chars`: `413`.
- `persona` is optional and falls back to the active persona. Unknown: `400`.
- `source` is a short label for who is speaking. It becomes the speaker key
  (`session_id` "browser", `project` "<source>:<label>"), so the daemon's speaker-change chime
  fires when a room and the page interleave, exactly as it does between two rooms. Default
  `browser`.
- `want_marks` asks for timing. Costs one synthesis per sentence instead of one per block.

Response `202`:

```json
{"id": "3f9c2b7e1a04d5c6", "state": "queued"}
```

### `GET /jobs/<id>`

```json
{"id": "3f9c2b7e1a04d5c6",
 "state": "playing",
 "source": "artifact", "project": "artifact:PR 1223 cascade", "persona": "claude-connery",
 "speed": 1.8,
 "duration_ms": 8420,
 "started_at": 1790099395.068,
 "offset_ms": 0,
 "marks": {
   "sentence_timing": "exact",
   "word_timing": "estimated",
   "sentences": [{"i": 0, "c": 0, "start_ms": 0, "end_ms": 1840, "text": "One block of prose."}],
   "words":     [{"s": 0, "c": 0, "start_ms": 0, "end_ms": 310, "text": "One"}]}}
```

States: `queued`, `synthesizing`, `playing`, `paused`, `done`, `cancelled`, `failed`.

- `marks` appears once synthesis has finished and stays for the life of the job. Times are
  milliseconds of listening time, playback speed already applied, relative to the start of the
  block.
- `c` on every sentence and word is its character offset in the text you submitted. If your page
  spoke a transformed copy (abbreviations expanded, "#339" read as "issue 339"), map back by
  offset, not by counting words.
- `started_at` (epoch seconds) and `offset_ms` are set every time playback starts or resumes.
  After a pause the daemon rewinds a little, so `offset_ms` is where in the block the audio
  restarted. Highlight the word whose span contains
  `offset_ms + (now - started_at) * 1000`, and re-read both fields whenever `state` changes.
- `position_ms` is present in `paused` and `cancelled`: how far the listener got.
- `failed` carries `error`.
- Finished jobs are readable for five minutes, then `404`.

Poll at a few hertz. There is no streaming endpoint on purpose: `GM_xmlhttpRequest` streams
poorly and polling on loopback is free.

### `POST /stop`

```json
{"source": "artifact"}
```

Response:

```json
{"flushed": 3, "stopped_current": true}
```

Deletes every queued message from that source. If the message playing right now is from that
source, the player is killed and the message is cleared with no replay. Messages from rooms are
untouched, queued or playing.

## How timing is measured

With `want_marks`, the daemon splits the block into sentences on terminal punctuation, runs the
persona's engine once per sentence, measures each WAV, concatenates them, and divides by the
playback speed. Sentence boundaries are therefore exact for any engine. Words inside a sentence
are placed by character count, which is why `word_timing` says `estimated`. If any sentence fails
to synthesize the daemon falls back to one-piece synthesis and reports `sentence_timing:
"estimated"` with a single span.

Known cost: Piper loads its model per process, so a six-sentence paragraph pays six short loads
before the first word. Batching sentences through one Piper process is the planned follow-up if
that gap is audible. Real word boundaries from an engine that reports token timing (Kokoro) is
the follow-up after that.

## Threat model, plainly

Before this the daemon's inputs were files on your disk. With the bridge enabled, anything on
your machine that can reach loopback and read `~/.claude-tts/http-token` can make it speak.
That is the same set of things that could already write into `~/.claude-tts/queue/`, and the
bridge cannot change config, personas, or anything but the queue. Keep it disabled if you do not
use it.
