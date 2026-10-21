# Speaker lines: what a session says when it takes the floor

With `queue.speaker_transition` set to `"announce"` or `"chime+announce"`, the daemon speaks a
short line in the new speaker's own voice when the speaking session changes. Out of the box that
line is "<project> says:", or "<name> says:" once the session has a name (`claude-tts name Pat`).

If you name your sessions yourself, you can make that line say what your names mean. Set
`CLAUDE_TTS_SESSION` in a session's environment to an id of your own, then add rules to
`~/.claude-tts/config.json` that read the id and say a sentence:

```json
"speaker_lines": [
  {"match": "(?P<team>\\w+)--(?P<service>\\w+)",
   "say": "{name} from team {team} on {service}",
   "default_name": "A teammate"}
]
```

A session started with `CLAUDE_TTS_SESSION=infra--api` now says "A teammate from team infra on
api", or "Pat from team infra on api" after `claude-tts name Pat`. Any session your rules do not
match keeps "<project> says:". The daemon reads the rules at each speaker change, so you hear an
edit the next time the speaker changes, with no restart.

`claude-tts name` (or `/tts-name`) shows what this session would say, which rule made it, the
values the rule pulled out, and every rule it skipped with the reason.

## Shipped rule sets

A plain string in the list names a set shipped with claude-tts, expanded in place:

- `"rooms"`: ids shaped `owner--house--room` say "<name> of House <house>, in room <room>";
  `house--room` says "<name>, in room <room>" (the comma is a pause, 9.65.0). A `claude-code-` prefix on the room is not spoken,
  and "Friend" stands in until the session has a name. With more than three parts, the last two
  are the house and the room.

```json
"speaker_lines": [{"match": "ops--.*", "say": "{name}, on call"}, "rooms"]
```

A room or a name an engine says wrong ("k8s" read letter by letter) is fixed once for every
voice and every line, replies included: `claude-tts pronounce k8s kubernetes` writes
`"pronounce": {"k8s": "kubernetes"}` to config.json. A word matches whole and in any case,
the daemon reads the map at each synthesis, and `claude-tts pronounce` lists it (9.65.0).

Rules are tried in order and the first match wins, so put your own rules before a shipped set to
override it for the ids they match.

<details>
<summary>For agents: the exact contract</summary>

- Key: top-level `speaker_lines` in `~/.claude-tts/config.json`; a list. Absent or `[]`: no rules.
  Not a list: no rules, reported by `claude-tts name` as skipped.
- Entry: a string naming a shipped set (unknown name: skipped), or an object with string `match`
  and string `say`, optional string `default_name`. Anything else: skipped.
- `match`: a Python regular expression that must match the whole session id (`re.fullmatch`). A
  pattern that does not compile is skipped.
- `say`: literal text with `{field}` placeholders, where a field is a word (`\w+`). Fields: every
  named group that took part in the match, plus `name`, `project` and `session` (these three win
  over a group of the same name). A rule that names a field it does not have is skipped. Values
  are inserted as they are and never read as placeholders themselves. No other formatting.
- `name`: the session's stored name, else the rule's `default_name`, else the project, else
  `unknown`.
- No rule matches: `"<name or project or 'unknown'> says:"`. A page that sent no session (`session_id` "browser") says its label, not its source: project `codex-tts:Codex` is "Codex says:" (9.61.0).
- Session ids: Claude Code project folder ids start with `-` (an absolute path with `/` and `.`
  replaced). Ids you set through `CLAUDE_TTS_SESSION` are yours to shape. The `rooms` set never
  matches an id that starts with `-`.
- Messages from the HTTP bridge use the `session` field of `POST /speak` as their id
  (docs/http-bridge.md), so the same rules apply to them.
- Upgrade: an install that already announced before 9.55.0 and has no `speaker_lines` key gets
  `["rooms"]` written once by `claude-tts-install --upgrade`, so it keeps the lines it had.
- Source: `src/claude_code_tts/speakerline.py`.

</details>
