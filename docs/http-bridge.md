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
 "want_marks": true,
 "lane": "background"}
```

- `text` is required. Longer than the persona's `max_chars`: `413`.
- `persona` is optional and falls back to the active persona. Unknown: `400`.
- `source` is a short label for who is speaking. It becomes the speaker key
  (`session_id` "browser", `project` "<source>:<label>"), so the daemon's speaker-change chime
  fires when a room and the page interleave, exactly as it does between two rooms. Default
  `browser`.
- `want_marks` asks for timing. Costs one synthesis per sentence instead of one per block.
- `lane` is optional. `"background"` lets every message without it go first: a page reading a
  long document block by block sets it on every block, so a session's one-liner arriving
  mid-read is spoken at the next block boundary instead of behind every queued block. Within a
  lane the order is arrival order. Ageing (`max_age_seconds`) and depth trimming still count
  by arrival, so a background block that waits behind a busy session can age out; keep only a
  few blocks queued and top up as they finish. Any other value: `400`.

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

### `GET /jobs?source=<name>`

```json
{"source": "page",
 "jobs": [{"id": "3f9c2b7e1a04d5c6", "state": "done", "source": "page", "project": "page:PR 1223 cascade",
           "persona": "claude-connery", "lane": "background", "created_at": 1790099390.12}]}
```

Every job the daemon still has on record for that source, oldest first, in the `/jobs/<id>`
shape plus `created_at`. A page that was reloaded finds what it queued, resumes polling the one
that is playing, or stops the lot. Same five-minute memory for finished jobs. `source` is
required: `400` without it.

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

### `GET /pause` and `POST /pause`

The daemon has one hold for everything it plays: the flag behind the pause hotkey and
`claude-tts pause`. These routes read and set that same flag, so a pause button on a page holds
the rooms too, and a hotkey press shows up on the page. There is no per-source pause; a page that
wants only its own reading to stop uses `/stop`. A person's hold can let named rooms through
(`let`, below): everyone else queues behind the scenes until the hold is released.

`GET /pause`:

```json
{"paused": true, "paused_by": "mic", "held_since": "2026-10-02T15:04:05Z",
 "held_until": "2026-10-02T15:07:05Z", "speaking": false,
 "current": {"id": "3f9c2b7e1a04d5c6", "source": "page", "project": "page:PR 1223 cascade"},
 "output_device": {"name": "MacBook Pro Speakers", "transport": "builtin", "checked_at": "2026-10-02T15:07:01Z"}}
```

- `output_device` is the Mac's default sound output as the daemon last saw it (it asks
  `system_profiler` every 15 s, off the play loop, and records the answer in
  `~/.claude-tts/output-device.json`): the device name, its transport without the profiler's
  prefix (`builtin`, `bluetooth`, `usb`), and when it was checked, RFC 3339 UTC. A round the
  profiler answers nothing leaves the last device standing with its own stamp, so `checked_at`
  is the freshness. `null` on a daemon that is not on macOS. A page shows a person whether the
  headphones are the output without a shell. Added additively in 9.49.0.

- `paused_by` is `user` for a person (hotkey, CLI, or this route) and `mic` for the mic watcher;
  `null` when not paused.
- `held_since` is when the hold began and `held_until` is when the daemon lets a mic hold go by
  itself (`held_since` plus `mic_pause_max_s`), both RFC 3339 in UTC. A person's hold has no
  bound, so `held_until` is `null` there; both are `null` when not paused. A page that must not
  resume over a recording greys its Resume button while `paused_by` is `mic`, and can show the
  bound. Added additively (`held_*` fields), nothing else in the reply changed.
- `let_through` is the list of rooms a person's hold lets play (room tags such as `tts` or
  `notes`, or full session ids); empty for a mic hold and when not paused. The key for a
  friend is the room tag: the part of the session id after the last `--`, minus a leading
  `claude-code-` (`alice--claude--claude-code-tts` is `tts`). `house-presence who` lists the
  rooms awake.
- `mic_held` is true while a recording runs under a person's hold: the rooms let through wait
  for it too, and speak again when Handy stops.
- `speaking` is true while a player process is running.
- `current` is the message on deck, playing or held; empty between messages.

`POST /pause` with `{"paused": true}` or `{"paused": false}` sets the hold; an empty body `{}`
toggles it. `{"let": ["tts", "jmo"]}` holds everyone and lets those rooms play; it replaces the
list, so send the whole list each time (up to 32 entries of `[A-Za-z0-9_.-]`). `{"paused": false}`
releases everyone, list included (the kraken); `let` with `paused: false` is `400`. Anything else
in `paused`: `400`. The response is the `GET` shape plus `changed`,
false when the hold already matched. Only the flag is written: the play loop polls it every
50 ms and stops the player itself, rewinding a little so nothing is lost on resume, exactly as
it does for the hotkey. A resume clears a mic hold as well, since a person pressing resume
knows better than the watcher.

### `GET /queue?by=room`

How far behind each friend is. One row per room with speech waiting, oldest first; a page's own
messages are grouped under their `source`; control messages are not counted.

```json
{"by": "room", "paused": true, "total": 7,
 "rooms": [{"room": "notes", "queued": 4, "oldest_queued_at": "2026-10-03T01:02:03Z",
            "oldest_age_s": 611, "held": true},
           {"room": "tts", "queued": 3, "oldest_queued_at": "2026-10-03T01:09:40Z",
            "oldest_age_s": 154, "held": false}]}
```

- `room` is the same key `let` takes, so a picker needs no mapping table.
- `oldest_age_s` is wall-clock age, hold time included: the answer to "how far behind am I".
- `held` is the hold as it applies to that room right now (a mic hold holds every room; a
  person's hold holds every room not in `let_through`).
- `by` other than `room`: `400`. The queue is read from disk on each call; poll it at a human
  pace (once a second is plenty).

## Mute does not apply here

`muted`, `default_muted` and a session's own mute are decided by the hook before it writes a
queue file: a muted session never queues. The bridge writes queue files directly for a page,
which is not a session and has no mute of its own, so a bridge message speaks even when every
session is muted. That is by design: the person asked the page to read, and the way to stop it
is the page's own control (`/stop`) or the hold (`/pause`). A page that wants to honour a
house-wide silence can read `muted` from the config file itself; the bridge does not expose it.

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
bridge cannot change config, personas, or anything but the queue and its pause flag. Keep it disabled if you do not
use it.
