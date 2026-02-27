#!/bin/bash
#
# tts-audition.sh - Broadway auditions for TTS voices
#
# Cycles through voices or speakers, playing a test phrase for each.
# Press Enter to hear the next voice, 'k' to keep/save, 'q' to quit.
#
# Usage:
#   tts-audition.sh                           # Audition all installed Piper voices
#   tts-audition.sh --kokoro                  # Audition all Kokoro voices
#   tts-audition.sh --kokoro --filter am_     # Audition Kokoro voices matching prefix
#   tts-audition.sh --kokoro --queue          # Audition via daemon queue (no stomping)
#   tts-audition.sh --voice en_US-libritts_r-medium --speakers 10
#   tts-audition.sh --voice en_US-libritts_r-medium --range 100-150
#   tts-audition.sh --text "Custom audition line"
#   tts-audition.sh --speed 1.8               # Set speed for all auditions
#
# Options:
#   --voice NAME     Audition speakers within a specific Piper voice model
#   --kokoro         Audition Kokoro voices (via swift-kokoro)
#   --filter PREFIX  Filter Kokoro voices by prefix (e.g., am_, bf_)
#   --queue          Play through daemon queue instead of raw afplay
#   --speakers N     Number of random speakers to audition (default: 10)
#   --range N-M      Audition specific speaker ID range
#   --text "..."     Custom audition text
#   --speed N        Speed multiplier (default: 1.5)
#   --method METHOD  Speed method: playback or length_scale (default: playback)
#   --help           Show this help
#

set -euo pipefail

# --- Configuration ---
VOICES_DIR="$HOME/.local/share/piper-voices"
TEMP_FILE="/tmp/tts_audition_$$.wav"

# --- Defaults ---
VOICE=""
KOKORO_MODE=false
KOKORO_FILTER=""
QUEUE_MODE=false
NUM_SPEAKERS=10
SPEAKER_RANGE=""
SPEED="1.5"
METHOD="playback"
TEXT="Three hidden keys open three secret gates, wherein the errant will be tested for worthy traits. And those with the skill to survive these straits will reach the end where the prize awaits. I've been searching for the copper key for five years now, and I'm starting to think that maybe Halliday hid it somewhere nobody would ever think to look. But that's the thing about Easter eggs. They're never where you expect them to be."
QUEUE_DIR="$HOME/.claude-tts/queue"

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# --- Parse arguments ---
show_help() {
    sed -n '2,/^$/p' "$0" | sed 's/^# *//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --voice)
            VOICE="$2"
            shift 2
            ;;
        --kokoro)
            KOKORO_MODE=true
            shift
            ;;
        --filter)
            KOKORO_FILTER="$2"
            shift 2
            ;;
        --queue)
            QUEUE_MODE=true
            shift
            ;;
        --speakers)
            NUM_SPEAKERS="$2"
            shift 2
            ;;
        --range)
            SPEAKER_RANGE="$2"
            shift 2
            ;;
        --text)
            TEXT="$2"
            shift 2
            ;;
        --speed)
            SPEED="$2"
            shift 2
            ;;
        --method)
            METHOD="$2"
            shift 2
            ;;
        --help|-h)
            show_help
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# --- Cleanup ---
cleanup() {
    rm -f "$TEMP_FILE"
}
trap cleanup EXIT

# --- Helper: speak with given voice/speaker ---
speak() {
    local voice="$1"
    local speaker="${2:-}"
    local voice_path="$VOICES_DIR/${voice}.onnx"

    local speaker_flag=""
    [[ -n "$speaker" ]] && speaker_flag="--speaker $speaker"

    case "$METHOD" in
        length_scale)
            local length_scale=$(awk "BEGIN {printf \"%.2f\", 1.0 / $SPEED}")
            echo "$TEXT" | piper --model "$voice_path" --length_scale "$length_scale" $speaker_flag --output_file "$TEMP_FILE" 2>/dev/null
            afplay "$TEMP_FILE" 2>/dev/null
            ;;
        playback|*)
            echo "$TEXT" | piper --model "$voice_path" $speaker_flag --output_file "$TEMP_FILE" 2>/dev/null
            afplay -r "$SPEED" "$TEMP_FILE" 2>/dev/null
            ;;
    esac
}

# --- Helper: generate and play a Kokoro clip ---
_kokoro_play() {
    local voice="$1"
    local text="$2"
    local speed="${3:-1.0}"

    echo "$text" | swift-kokoro --voice "$voice" --output "$TEMP_FILE" 2>/dev/null
    afplay -r "$speed" "$TEMP_FILE" 2>/dev/null
}

# --- Helper: speak with Kokoro voice (full audition sequence) ---
# 1. Name at 1x  2. Text at 1x  3. Text at 2x  4. Name at 1x
speak_kokoro() {
    local voice="$1"
    # Extract human name: am_adam -> Adam, bf_emma -> Emma
    local raw_name="${voice#*_}"  # strip prefix (am_, bf_, etc.)
    raw_name="${raw_name//_/ }"   # remaining underscores to spaces
    # Capitalize first letter of each word
    local display_name
    display_name=$(echo "$raw_name" | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')

    if [[ "$QUEUE_MODE" == "true" ]]; then
        # Queue mode: send all four parts as separate queue messages
        write_audition_queue "$voice" "Hi there, my name is ${display_name}, and I'm auditioning for the role of your AI assistant today. I hope you enjoy listening to my voice." "1.0"
        write_audition_queue "$voice" "$TEXT" "1.0"
        write_audition_queue "$voice" "$TEXT" "2.0"
        write_audition_queue "$voice" "That was me, ${display_name}. Thanks for listening, and I hope to work with you soon." "1.0"
        # Wait for all audition messages to drain
        local waited=0
        while [[ $(ls "$QUEUE_DIR"/*audition_*.json 2>/dev/null | wc -l) -gt 0 ]]; do
            sleep 0.2
            waited=$((waited + 1))
            if (( waited > 150 )); then
                echo -e "${RED}Timed out waiting for daemon${NC}" >&2
                break
            fi
        done
    else
        local intro_text="Hi there, my name is ${display_name}, and I'm auditioning for the role of your AI assistant today. I hope you enjoy listening to my voice."
        local outro_text="That was me, ${display_name}. Thanks for listening, and I hope to work with you soon."

        echo ""
        echo -e "  ${CYAN}[1x intro]${NC}"
        echo -e "  ${YELLOW}\"${intro_text}\"${NC}"
        _kokoro_play "$voice" "$intro_text" "1.0"
        echo ""
        echo -e "  ${CYAN}[1x text]${NC}"
        echo -e "  ${YELLOW}\"${TEXT}\"${NC}"
        _kokoro_play "$voice" "$TEXT" "1.0"
        echo ""
        echo -e "  ${CYAN}[2x text]${NC}"
        echo -e "  ${YELLOW}(same text at 2x speed)${NC}"
        _kokoro_play "$voice" "$TEXT" "2.0"
        echo ""
        echo -e "  ${CYAN}[1x outro]${NC}"
        echo -e "  ${YELLOW}\"${outro_text}\"${NC}"
        _kokoro_play "$voice" "$outro_text" "1.0"
    fi
}

# --- Helper: write audition message to daemon queue ---
write_audition_queue() {
    local kokoro_voice="$1"
    local text="${2:-$TEXT}"
    local speed="${3:-$SPEED}"
    mkdir -p "$QUEUE_DIR"

    local timestamp=$(date +%s.%N)
    local msg_id="audition_$(head -c 4 /dev/urandom | xxd -p)"
    local queue_file="$QUEUE_DIR/${timestamp}_${msg_id}.json"

    cat > "$queue_file" << EOF
{
  "id": "$msg_id",
  "timestamp": $timestamp,
  "session_id": "audition",
  "project": "audition",
  "text": $(echo "$text" | jq -Rs .),
  "persona": "claude-prime",
  "speed": ${speed},
  "speed_method": "playback",
  "voice_kokoro": "${kokoro_voice}"
}
EOF
}

# --- Helper: prompt for action ---
prompt_action() {
    local voice="$1"
    local speaker="${2:-}"
    local is_kokoro="${3:-false}"

    echo ""
    echo -e "${CYAN}[Enter]${NC} Next voice  ${GREEN}[k]${NC} Keep this one  ${YELLOW}[r]${NC} Replay  ${RED}[q]${NC} Quit"

    while true; do
        read -rsn1 key
        case "$key" in
            ""|$'\n')
                return 0  # Next
                ;;
            k|K)
                echo ""
                echo -e "${GREEN}Keeping: ${voice}${speaker:+ (speaker $speaker)}${NC}"
                echo -n "Save as persona name (or Enter to skip): "
                read persona_name
                if [[ -n "$persona_name" ]]; then
                    save_persona "$persona_name" "$voice" "$speaker" "$is_kokoro"
                fi
                return 0
                ;;
            r|R)
                echo -e "${BLUE}Replaying...${NC}"
                if [[ "$is_kokoro" == "true" ]]; then
                    speak_kokoro "$voice"
                else
                    speak "$voice" "$speaker"
                fi
                ;;
            q|Q)
                echo ""
                echo -e "${YELLOW}Auditions complete!${NC}"
                exit 0
                ;;
        esac
    done
}

# --- Helper: save as persona ---
save_persona() {
    local name="$1"
    local voice="$2"
    local speaker="${3:-}"
    local is_kokoro="${4:-false}"

    local config_file="$HOME/.claude-tts/config.json"

    if [[ ! -f "$config_file" ]]; then
        echo -e "${RED}Config file not found${NC}"
        return
    fi

    # Build persona JSON
    local persona_json
    if [[ "$is_kokoro" == "true" ]]; then
        persona_json=$(cat <<EOF
{
    "description": "Created from audition - Kokoro ${voice}",
    "voice_kokoro": "${voice}",
    "speed": ${SPEED},
    "speed_method": "playback",
    "max_chars": 10000,
    "ai_type": "claude"
}
EOF
)
    elif [[ -n "$speaker" ]]; then
        persona_json=$(cat <<EOF
{
    "description": "Created from audition - ${voice} speaker ${speaker}",
    "voice": "${voice}",
    "speaker": ${speaker},
    "speed": ${SPEED},
    "speed_method": "${METHOD}",
    "max_chars": 10000,
    "ai_type": "claude"
}
EOF
)
    else
        persona_json=$(cat <<EOF
{
    "description": "Created from audition - ${voice}",
    "voice": "${voice}",
    "speed": ${SPEED},
    "speed_method": "${METHOD}",
    "max_chars": 10000,
    "ai_type": "claude"
}
EOF
)
    fi

    # Add to config
    jq --arg name "$name" --argjson persona "$persona_json" \
        '.personas[$name] = $persona' "$config_file" > /tmp/config_tmp.json \
        && mv /tmp/config_tmp.json "$config_file"

    echo -e "${GREEN}Saved persona: $name${NC}"
}

# --- Main ---
echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}    TTS Broadway Auditions${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""
echo -e "Speed: ${SPEED}x (${METHOD})"
echo -e "Text: \"${TEXT:0:50}...\""
echo ""

if [[ "$KOKORO_MODE" == "true" ]]; then
    # Kokoro voice auditions
    if ! command -v swift-kokoro &>/dev/null; then
        echo -e "${RED}swift-kokoro not found${NC}" >&2
        exit 1
    fi

    # Get voice list from swift-kokoro
    all_voices=()
    while IFS= read -r v; do
        [[ -n "$v" ]] && all_voices+=("$v")
    done < <(swift-kokoro --list-voices 2>/dev/null)

    if [[ ${#all_voices[@]} -eq 0 ]]; then
        echo -e "${RED}No Kokoro voices found${NC}" >&2
        exit 1
    fi

    # Apply filter if specified (comma-separated prefixes, e.g., am_,bm_)
    voices=()
    if [[ -z "$KOKORO_FILTER" ]]; then
        voices=("${all_voices[@]}")
    else
        IFS=',' read -ra filters <<< "$KOKORO_FILTER"
        for v in "${all_voices[@]}"; do
            for f in "${filters[@]}"; do
                if [[ "$v" == "$f"* ]]; then
                    voices+=("$v")
                    break
                fi
            done
        done
    fi

    if [[ ${#voices[@]} -eq 0 ]]; then
        echo -e "${RED}No voices matching filter: $KOKORO_FILTER${NC}" >&2
        exit 1
    fi

    echo -e "Backend: ${CYAN}Kokoro (swift-kokoro)${NC}"
    [[ -n "$KOKORO_FILTER" ]] && echo -e "Filter: ${KOKORO_FILTER}*"
    [[ "$QUEUE_MODE" == "true" ]] && echo -e "Playback: ${CYAN}via daemon queue${NC}"
    echo -e "Found ${#voices[@]} voices"
    echo -e "${YELLOW}Press Enter to begin${NC}"
    read

    for voice in "${voices[@]}"; do
        echo ""
        echo -e "${BLUE}>>> $voice <<<${NC}"
        speak_kokoro "$voice"
        prompt_action "$voice" "" "true"
    done

elif [[ -n "$VOICE" ]]; then
    # Audition speakers within a single multi-speaker model
    voice_path="$VOICES_DIR/${VOICE}.onnx"
    voice_json="$VOICES_DIR/${VOICE}.onnx.json"

    if [[ ! -f "$voice_path" ]]; then
        echo -e "${RED}Voice not found: $VOICE${NC}" >&2
        exit 1
    fi

    num_available=1
    if [[ -f "$voice_json" ]]; then
        num_available=$(jq -r '.num_speakers // 1' "$voice_json")
    fi

    if [[ "$num_available" -le 1 ]]; then
        echo -e "${YELLOW}$VOICE is not a multi-speaker model${NC}"
        echo "Auditioning single voice..."
        echo ""
        echo -e "${BLUE}>>> $VOICE <<<${NC}"
        speak "$VOICE" ""
        prompt_action "$VOICE" ""
        exit 0
    fi

    echo -e "Model: ${VOICE} (${num_available} speakers available)"
    echo ""

    # Generate speaker list
    speakers=()
    if [[ -n "$SPEAKER_RANGE" ]]; then
        # Parse range like "100-150"
        start="${SPEAKER_RANGE%-*}"
        end="${SPEAKER_RANGE#*-}"
        for ((i=start; i<=end && i<num_available; i++)); do
            speakers+=("$i")
        done
    else
        # Random speakers
        for ((i=0; i<NUM_SPEAKERS; i++)); do
            speakers+=("$((RANDOM % num_available))")
        done
    fi

    echo -e "Auditioning ${#speakers[@]} speakers..."
    echo -e "${YELLOW}Press Enter to begin${NC}"
    read

    for speaker in "${speakers[@]}"; do
        echo ""
        echo -e "${BLUE}>>> Speaker #${speaker} <<<${NC}"
        speak "$VOICE" "$speaker"
        prompt_action "$VOICE" "$speaker"
    done

else
    # Audition all installed voices
    voices=()
    for f in "$VOICES_DIR"/*.onnx; do
        [[ -f "$f" ]] || continue
        voices+=("$(basename "$f" .onnx)")
    done

    if [[ ${#voices[@]} -eq 0 ]]; then
        echo -e "${RED}No voices installed${NC}" >&2
        exit 1
    fi

    echo -e "Found ${#voices[@]} installed voices"
    echo -e "${YELLOW}Press Enter to begin${NC}"
    read

    for voice in "${voices[@]}"; do
        echo ""
        echo -e "${BLUE}>>> $voice <<<${NC}"
        speak "$voice" ""
        prompt_action "$voice" ""
    done
fi

echo ""
echo -e "${GREEN}Auditions complete!${NC}"
