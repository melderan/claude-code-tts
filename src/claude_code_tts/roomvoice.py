"""One room's voice: on or off, one truth, two ways in.

JMO, 2026-11-10: "There shouldn't be two mechanisms. Just one toggle with two interfaces to
reach please." He had muted a room with `claude-tts mute`; later he turned it on in hear,
which only added it to his hold's let-through list, and the room stayed silent while both
said it was on.

The truth is the room's session mute in sessions.d, the flag `claude-tts mute` has always
written. On also puts the room through a person's hold that is live, so on means heard; off
also takes it off that hold's let-through list, so the list never names a silent room.
Nothing here starts or ends a hold: a hold over everyone stays the person's own switch
(`claude-tts hold`, `claude-tts kraken`). The CLI (`claude-tts voice`, `mute --room`,
`unmute --room`) and the bridge (`GET /voice`, `POST /voice`) both come here; neither keeps a
copy. room_voice names every other reason an "on" room is not heard right now.
"""

from __future__ import annotations

from claude_code_tts import config as config_mod
from claude_code_tts.state import (
    heartbeat_fresh,
    in_meeting,
    lets_through,
    read_playback_state,
    room_tag,
    write_playback_state,
)


def room_of(session_id: str) -> str:
    """The room name a session id is reached by: its tag (tts), or a folder id whole."""
    return session_id if session_id.startswith("-") else room_tag(session_id)


def room_sessions(room: str) -> list[str]:
    """The session ids in sessions.d that belong to a room, matched as `hold --let` matches.

    A full id, a room tag (tts) or the id's last segment; a Claude Code folder id
    (-Users-...-claude-code-tts) belongs to the room its path ends in, since one room's
    checkout is spoken under both its folder id and its owner--house--room id.
    """
    sessions_dir = config_mod.TTS_SESSIONS_DIR
    if not sessions_dir.is_dir():
        return []
    found = []
    for sf in sorted(sessions_dir.glob("*.json")):
        sid = sf.stem
        if lets_through(sid, [room]) and not sid.startswith("-"):
            found.append(sid)
        elif sid.startswith("-") and (sid == room or sid.endswith(f"-{room}")):
            found.append(sid)
    return found


def _muted(session_id: str, raw: dict) -> bool:
    """A listed session's mute as load_config resolves it: its own flag, else the global mute.

    default_muted applies only to a session with no file, and every listed session has one.
    """
    flag = config_mod.session_read(session_id).get("muted")
    return bool(flag) if flag is not None else bool(raw.get("muted", False))


def room_voice(room: str, *, daemon_up: bool | None = None) -> dict | None:
    """The room's switch and whether it is heard right now; None when no session is the room's.

    `on` is the switch. `audible` is on with nothing else in the way; `reasons` names each
    thing in the way, a short key and the words for a person: muted (the switch is off),
    meeting (a call holds every voice), recording (the person is dictating), held (a person's
    hold that does not let this room through), daemon (no daemon is speaking). daemon_up None
    reads the heartbeat; the bridge, which lives in the daemon, passes True.
    """
    sessions = room_sessions(room)
    if not sessions:
        return None
    raw = config_mod.load_raw_config()
    muted = [s for s in sessions if _muted(s, raw)]
    state = read_playback_state()
    reasons: list[dict] = []
    if muted:
        reasons.append({"reason": "muted", "detail": f"switched off ({', '.join(muted)} muted)"})
    if in_meeting(state):
        apps = ", ".join(str(a) for a in state["meeting"].get("apps") or [])
        reasons.append({"reason": "meeting", "detail": f"a call holds every voice ({apps})"})
    elif state.get("paused"):
        if state.get("paused_by") != "user" or state.get("mic_held"):
            reasons.append({"reason": "recording", "detail": "a recording holds every voice until it ends"})
        if state.get("paused_by") == "user" and not any(lets_through(s, state.get("let_through")) for s in sessions):
            reasons.append({"reason": "held", "detail": "a person's hold does not let this room through"})
    if daemon_up is None:
        daemon_up = heartbeat_fresh()
    if not daemon_up:
        reasons.append({"reason": "daemon", "detail": "no daemon is speaking (heartbeat missing or old)"})
    return {
        "room": room,
        "on": not muted,
        "audible": not reasons,
        "reasons": reasons,
        "sessions": [{"session": s, "muted": s in muted} for s in sessions],
    }


def set_room_voice(room: str, on: bool, *, daemon_up: bool | None = None) -> dict | None:
    """Turn the room on or off; the room's voice afterwards, or None when no session is the room's.

    On: every session of the room unmuted, and under a person's hold the room let through.
    Off: every session muted, and every entry of the hold's list that lets the room through
    removed. A hold is never started or ended here.
    """
    sessions = room_sessions(room)
    if not sessions:
        return None
    for sid in sessions:
        config_mod.session_set(sid, "muted", not on)
    state = read_playback_state()
    if state.get("paused") and state.get("paused_by") == "user":
        listed = [str(r) for r in state.get("let_through") or []]
        if on and not any(lets_through(s, listed) for s in sessions):
            write_playback_state(let_through=listed + [room])
        elif not on:
            kept = [e for e in listed if not any(lets_through(s, [e]) for s in sessions)]
            if kept != listed:
                write_playback_state(let_through=kept)
    return room_voice(room, daemon_up=daemon_up)


def describe(view: dict) -> str:
    """One line for a person: "Room tts: on, heard" or "Room tts: on, not heard: <why>; <why>"."""
    head = f"Room {view['room']}: {'on' if view['on'] else 'off'}"
    others = [r["detail"] for r in view["reasons"] if r["reason"] != "muted"]
    if view["audible"]:
        return f"{head}, heard"
    if not others:
        return head
    return f"{head}, not heard: {'; '.join(others)}"
