"""What a session says for itself when it takes the floor: rules from config, first match wins.

With `queue.speaker_transition` "announce" or "chime+announce", the daemon speaks a short line
in the new speaker's voice before its message. Which line is decided by the `speaker_lines`
list in config.json. Each entry is either a rule or the name of a rule set shipped here:

    "speaker_lines": [
        {"match": "(?P<team>\\w+)--(?P<project>\\w+)", "say": "{name} from team {team}",
         "default_name": "A teammate"},
        "rooms"
    ]

A rule's `match` is a regular expression the whole session id must match (Python syntax).
`say` is the line, with `{field}` filled from the regex's named groups and from `name` (the
session's own name, `claude-tts name`, else the rule's `default_name`, else the project),
`project` and `session`. A rule whose regex does not compile, or whose `say` names a field it
does not have, is skipped and reported by `claude-tts name`. With no rule matching, or no
`speaker_lines` at all, the line is "<name or project> says:".

Session ids come from the Claude Code project folder ("-Users-alice-code-app") unless
CLAUDE_TTS_SESSION names one, so a rule set for your own ids only matches sessions you named.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Rule sets shipped by name. "rooms": ids of the shape owner--house--room (or house--room),
# set through CLAUDE_TTS_SESSION; a "claude-code-" prefix on the room is not spoken. An id
# that starts with "-" is a Claude Code folder, never a room.
_WORD = r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*"
PRESETS: dict[str, list[dict]] = {
    "rooms": [
        {
            "match": rf"[A-Za-z0-9_][A-Za-z0-9_-]*?--(?P<house>{_WORD})--(?:claude-code-)?(?P<room>{_WORD})",
            "say": "{name} of House {house}, in room {room}",
            "default_name": "Friend",
        },
        {
            "match": rf"{_WORD}--(?:claude-code-)?(?P<room>{_WORD})",
            "say": "{name}, in room {room}",
            "default_name": "Friend",
        },
    ],
}

_FIELD = re.compile(r"\{(\w+)\}")


@dataclass
class Spoken:
    """The line, and which rule made it (empty for the plain fallback), with its fields."""

    line: str
    rule: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def expand(entries: object) -> tuple[list[tuple[str, dict]], list[str]]:
    """The rules a `speaker_lines` value names, labelled for a reader; and what was refused."""
    rules: list[tuple[str, dict]] = []
    refused: list[str] = []
    if not isinstance(entries, list):
        return rules, ([] if entries is None else ["speaker_lines is not a list"])
    for i, entry in enumerate(entries):
        if isinstance(entry, str):
            if entry in PRESETS:
                rules += [(f"{entry}[{j}]", r) for j, r in enumerate(PRESETS[entry])]
            else:
                refused.append(f"#{i}: no rule set named {entry!r} (have: {', '.join(sorted(PRESETS))})")
        elif isinstance(entry, dict) and isinstance(entry.get("match"), str) and isinstance(entry.get("say"), str):
            rules.append((f"#{i}", entry))
        else:
            refused.append(f"#{i}: a rule needs string keys match and say")
    return rules, refused


def _fill(say: str, values: dict[str, str]) -> str:
    """`say` with each {field} replaced in one pass; a value is never read as a placeholder."""

    def one(m: re.Match[str]) -> str:
        return values[m.group(1)]

    return _FIELD.sub(one, say)


def speak_line(msg: dict, entries: object) -> Spoken:
    """The line for this message under these `speaker_lines` entries."""
    session_id = str(msg.get("session_id") or "")
    own_name = str(msg.get("name") or "").strip()
    project = str(msg.get("project") or "")
    rules, skipped = expand(entries)
    for label, rule in rules:
        try:
            m = re.fullmatch(rule["match"], session_id)
        except re.error as e:
            skipped.append(f"{label}: match does not compile ({e})")
            continue
        if not m:
            continue
        name = own_name or str(rule.get("default_name") or "") or project or "unknown"
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        values = {**groups, "name": name, "project": project, "session": session_id}
        missing = [f for f in _FIELD.findall(rule["say"]) if f not in values]
        if missing:
            skipped.append(f"{label}: say names {', '.join(missing)}, which the match does not give")
            continue
        return Spoken(_fill(rule["say"], values), label, groups, skipped)
    return Spoken(f"{own_name or _page_label(msg) or project or 'unknown'} says:", "", {}, skipped)


def _page_label(msg: dict) -> str:
    """A page's own label when it spoke for no session: "Codex" of project "codex-tts:Codex".

    The bridge names such a message "<source>:<label>"; the source is the page's plumbing, the
    label is who is talking, so "codex-tts:Codex says" is spoken as "Codex says".
    """
    source = str(msg.get("source") or "")
    project = str(msg.get("project") or "")
    if str(msg.get("session_id") or "") != "browser" or not source:
        return ""
    return project.removeprefix(f"{source}:") if project.startswith(f"{source}:") else ""
