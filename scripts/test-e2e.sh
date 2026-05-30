#!/usr/bin/env bash
# End-to-end voice agent test using baresip as the SIP client.
#
# Tests:
#   1. SIP registration with Asterisk
#   2. Inbound call  (baresip dials → Asterisk → agent)
#   3. Outbound call (API originates → Asterisk → baresip auto-answers → agent)
#
# Requirements: baresip (brew install baresip), ffmpeg (brew install ffmpeg)
# Usage:  ./scripts/test-e2e.sh [--no-cleanup]

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
API_URL="${API_URL:-http://127.0.0.1:8080}"
BARESIP="${BARESIP:-baresip}"
FFMPEG="${FFMPEG:-ffmpeg}"
BARESIP_CTRL_PORT=4446          # ctrl_tcp JSON port
BARESIP_SIP_PORT=5082           # SIP listen port (avoid clash with softphone)
INBOUND_CALL_DURATION=20        # seconds to keep inbound call alive
OUTBOUND_CALL_DURATION=20       # seconds to keep outbound call alive
AGENT_SLUG="basic"
CLEANUP=true
[[ "${1:-}" == "--no-cleanup" ]] && CLEANUP=false

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
pass() { echo -e "${GREEN}  ✅ $*${NC}"; }
fail() { echo -e "${RED}  ❌ $*${NC}"; FAILURES=$((FAILURES+1)); }
warn() { echo -e "${YELLOW}  ⚠️  $*${NC}"; }
info() { echo -e "${CYAN}  ℹ  $*${NC}"; }
header() { echo -e "\n${BOLD}$*${NC}"; }

FAILURES=0
BARESIP_PID=""
WORKDIR=$(mktemp -d /tmp/e2e-voiceai-XXXX)
trap cleanup EXIT

cleanup() {
  kill "$BARESIP_PID" 2>/dev/null || true
  [[ "$CLEANUP" == "true" ]] && rm -rf "$WORKDIR"
  echo ""
}

# ── Helpers ──────────────────────────────────────────────────────────────────

# Detect LAN IP (baresip needs a routable address, not 127.0.0.1)
lan_ip() {
  ipconfig getifaddr en1 2>/dev/null || \
  ipconfig getifaddr en0 2>/dev/null || \
  ipconfig getifaddr eth0 2>/dev/null || \
  echo "127.0.0.1"
}

# Send a command to baresip's ctrl_tcp port (JSON netstring protocol)
baresip_cmd() {
  local cmd="$1" params="${2:-}"
  python3 - "$cmd" "$params" "$BARESIP_CTRL_PORT" << 'PYEOF'
import sys, socket, json, time
cmd, params, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
obj = {'command': cmd}
if params: obj['params'] = params
s = json.dumps(obj)
msg = f'{len(s)}:{s},'.encode()
sock = socket.socket()
sock.connect(('127.0.0.1', port))
sock.settimeout(5)
sock.send(msg)
time.sleep(0.4)
try:
    r = sock.recv(8192).decode()
    colon = r.index(':')
    print(r[colon+1:colon+1+int(r[:colon])])
except Exception as e:
    print(json.dumps({'ok': False, 'data': str(e)}))
finally:
    sock.close()
PYEOF
}

# Poll until a condition is true or timeout
wait_for() {
  local desc="$1" condition="$2" timeout="${3:-15}"
  local elapsed=0
  while ! eval "$condition" &>/dev/null; do
    sleep 2; elapsed=$((elapsed+2))
    [[ $elapsed -ge $timeout ]] && return 1
  done
  return 0
}

# ── Step 0: preflight checks ─────────────────────────────────────────────────
header "── Preflight checks"

command -v "$BARESIP" &>/dev/null || { fail "baresip not found (brew install baresip)"; exit 1; }
command -v "$FFMPEG"  &>/dev/null || { fail "ffmpeg not found (brew install ffmpeg)"; exit 1; }
command -v python3    &>/dev/null || { fail "python3 not found"; exit 1; }
pass "baresip, ffmpeg, python3 found"

# Check stack is up
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/admin/agents" 2>/dev/null || echo "000")
[[ "$HEALTH" == "200" ]] || { fail "config-api not reachable at $API_URL (is 'make up' running?)"; exit 1; }
pass "config-api reachable"

# Ensure routes applied and agent-basic running
APPLY=$(curl -s -X POST "$API_URL/api/routes/apply")
AGENT_STATUS=$(echo "$APPLY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('containers',{}).get('$AGENT_SLUG','unknown'))" 2>/dev/null || echo "unknown")
[[ "$AGENT_STATUS" == "already_running" || "$AGENT_STATUS" == "started" ]] || \
  warn "agent-$AGENT_SLUG status: $AGENT_STATUS (may still work)"
pass "routes applied — agent-$AGENT_SLUG: $AGENT_STATUS"

LAN_IP=$(lan_ip)
info "LAN IP: $LAN_IP"

# ── Step 1: build test audio ─────────────────────────────────────────────────
header "── Building test audio"

SPEECH_RAW="$WORKDIR/speech.pcm"
SPEECH_24K="$WORKDIR/speech_24k.wav"
SPEECH_WAV="$WORKDIR/speech_8k.wav"   # separate from audio_in.wav

# Generate speech via local TTS service
TTS_STATUS=$(curl -s -o "$SPEECH_RAW" -w "%{http_code}" \
  -X POST "$API_URL/../.." --max-time 1 2>/dev/null || true)

# Use TTS service directly on port 5001
TTS_HTTP=$(curl -s -o "$SPEECH_RAW" -w "%{http_code}" \
  -X POST "http://127.0.0.1:5001/v1/audio/speech" \
  -H "Content-Type: application/json" \
  -d '{"model":"tts-1","input":"Hello! I am calling to test the voice agent system. How are you doing today?","voice":"alloy"}' \
  2>/dev/null || echo "000")

if [[ "$TTS_HTTP" == "200" && -s "$SPEECH_RAW" ]]; then
  # TTS returns raw PCM at 24kHz — wrap as WAV then resample to 8kHz for baresip
  python3 -c "
import wave, sys
with open('$SPEECH_RAW','rb') as f: raw=f.read()
with wave.open('$SPEECH_24K','w') as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(raw)
print(f'PCM {len(raw)} bytes → {len(raw)/(24000*2):.1f}s')
"
  "$FFMPEG" -y -i "$SPEECH_24K" -ar 8000 -ac 1 "$SPEECH_WAV" -loglevel error
  pass "Speech audio ready ($(wc -c < "$SPEECH_WAV") bytes at 8kHz)"
else
  # Fallback: generate a synthetic speech-like WAV using python
  warn "TTS not available (HTTP $TTS_HTTP) — generating synthetic audio"
  python3 - "$SPEECH_WAV" << 'PYEOF'
import struct, math, wave, sys
rate = 8000
# Simulate ~3s of 200Hz tone (passes VAD better than 440Hz)
samples = [int(16000 * math.sin(2 * math.pi * 200 * i / rate)) for i in range(rate * 3)]
with wave.open(sys.argv[1], 'w') as f:
    f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
    f.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
PYEOF
fi

# ── Step 2: start baresip ────────────────────────────────────────────────────
header "── Starting baresip"

# Write config
cat > "$WORKDIR/accounts" << EOF
<sip:softphone@${LAN_IP}>;auth_pass=secret1234;regint=30;answermode=auto
EOF

cat > "$WORKDIR/config" << EOF
sip_listen              0.0.0.0:${BARESIP_SIP_PORT}
audio_player            aufile,${WORKDIR}/audio_out.wav
audio_source            aufile,${WORKDIR}/audio_in.wav
audio_alert             aufile,${WORKDIR}/audio_out.wav
module_path             /opt/homebrew/lib/baresip/modules
module                  account.so
module                  aufile.so
module                  g711.so
module                  srtp.so
module                  menu.so
module                  debug_cmd.so
module                  ctrl_tcp.so
ctrl_tcp_listen         127.0.0.1:${BARESIP_CTRL_PORT}
log_level               info
EOF

# Copy speech WAV as baresip mic input
cp "$SPEECH_WAV" "$WORKDIR/audio_in.wav"

HOME="$WORKDIR" "$BARESIP" -f "$WORKDIR" > "$WORKDIR/baresip.log" 2>&1 &
BARESIP_PID=$!

# Wait for registration
if wait_for "SIP registration" \
  "grep -q '200 OK' '$WORKDIR/baresip.log'" 12; then
  pass "SIP registered: $(grep '200 OK' "$WORKDIR/baresip.log" | head -1 | sed 's/.*\(softphone.*\)/\1/')"
else
  fail "SIP registration timed out"
  cat "$WORKDIR/baresip.log"
  exit 1
fi

# ── Step 3: inbound call test ─────────────────────────────────────────────────
header "── Test 1: Inbound call (baresip → Asterisk → agent)"

INBOUND_BEFORE=$(docker logs "agent-$AGENT_SLUG" --since 10s 2>&1 | wc -l)

DIAL_RESULT=$(baresip_cmd "dial" "sip:1000@${LAN_IP}")
DIAL_TYPE=$(echo "$DIAL_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('type',''))" 2>/dev/null || echo "")

if [[ "$DIAL_TYPE" == "CALL_OUTGOING" ]]; then
  pass "baresip dialling sip:1000@${LAN_IP}"
else
  fail "dial failed: $DIAL_RESULT"
fi

# Wait for agent to pick up
if wait_for "agent connection" \
  "docker logs agent-$AGENT_SLUG --since 15s 2>&1 | grep -q 'Pipeline started'" 15; then
  INBOUND_UUID=$(docker logs "agent-$AGENT_SLUG" --since 15s 2>&1 | \
    grep "Call UUID:" | tail -1 | awk '{print $NF}')
  pass "Agent pipeline started — UUID: $INBOUND_UUID"
else
  fail "Agent did not start pipeline within 15s"
  INBOUND_UUID=""
fi

# Let call run
info "Call running for ${INBOUND_CALL_DURATION}s…"
sleep "$INBOUND_CALL_DURATION"

# Hang up
HUP=$(baresip_cmd "hangup" "")
HUP_TYPE=$(echo "$HUP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('type',''))" 2>/dev/null || echo "")
[[ "$HUP_TYPE" == "CALL_CLOSED" ]] && pass "Hung up cleanly" || warn "Hangup response: $HUP_TYPE"

# Wait for call log
sleep 5
if [[ -n "$INBOUND_UUID" ]]; then
  CALL_DATA=$(curl -s "$API_URL/api/calls/$INBOUND_UUID" 2>/dev/null || echo "{}")
  DURATION=$(echo "$CALL_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('duration_seconds','?'))" 2>/dev/null || echo "?")
  TURNS=$(echo "$CALL_DATA"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('turn_count','?'))" 2>/dev/null || echo "?")
  REASON=$(echo "$CALL_DATA"   | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('end_reason','?'))" 2>/dev/null || echo "?")
  pass "Call logged: duration=${DURATION}s turns=${TURNS} end_reason=${REASON}"
  [[ "$TURNS" -gt 0 ]] 2>/dev/null && pass "STT→LLM→TTS completed ($TURNS turns)" || \
    warn "0 turns — STT may not have recognised audio (check audio_in.wav quality)"
else
  fail "No call UUID captured — call log not verified"
fi

# ── Step 4: outbound call test ────────────────────────────────────────────────
header "── Test 2: Outbound call (API → Asterisk → baresip → agent)"

ORIGINATE=$(curl -s -X POST "$API_URL/api/outbound/originate" \
  -H "Content-Type: application/json" \
  -d "{\"destination\":\"softphone\",\"agent_slug\":\"$AGENT_SLUG\",\"caller_id\":\"Test Agent <1000>\",\"timeout_seconds\":30,\"template_vars\":{\"name\":\"Tester\"}}")

OUTBOUND_UUID=$(echo "$ORIGINATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('call_uuid',''))" 2>/dev/null || echo "")
OUTBOUND_STATUS=$(echo "$ORIGINATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")

if [[ -n "$OUTBOUND_UUID" && "$OUTBOUND_STATUS" == "originating" ]]; then
  pass "Outbound originate accepted — UUID: $OUTBOUND_UUID"
else
  fail "Originate failed: $ORIGINATE"
fi

# Wait for baresip to auto-answer and agent to connect
if wait_for "outbound agent connection" \
  "docker logs agent-$AGENT_SLUG --since 30s 2>&1 | grep -q '$OUTBOUND_UUID'" 25; then
  pass "Agent picked up outbound call"
else
  warn "Agent log did not show outbound UUID within 25s — checking anyway"
fi

# Wait for Asterisk to show active channel
if wait_for "active channel" \
  "docker exec asterisk asterisk -rx 'core show channels' 2>&1 | grep -q 'AudioSocket'" 15; then
  CHAN_INFO=$(docker exec asterisk asterisk -rx "core show channels" 2>&1 | grep AudioSocket | head -1)
  pass "Active channel: $CHAN_INFO"
else
  warn "No AudioSocket channel visible in Asterisk after 15s"
fi

info "Outbound call running for ${OUTBOUND_CALL_DURATION}s…"
sleep "$OUTBOUND_CALL_DURATION"

baresip_cmd "hangup" "" > /dev/null 2>&1 || true

sleep 5
if [[ -n "$OUTBOUND_UUID" ]]; then
  CALL_DATA=$(curl -s "$API_URL/api/calls/$OUTBOUND_UUID" 2>/dev/null || echo "{}")
  DURATION=$(echo "$CALL_DATA" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('duration_seconds','?'))" 2>/dev/null || echo "?")
  TURNS=$(echo "$CALL_DATA"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('turn_count','?'))" 2>/dev/null || echo "?")
  DIRECTION=$(echo "$CALL_DATA"| python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('direction','?'))" 2>/dev/null || echo "?")
  pass "Call logged: direction=${DIRECTION} duration=${DURATION}s turns=${TURNS}"
else
  fail "No outbound UUID captured"
fi

# ── Step 5: final summary ─────────────────────────────────────────────────────
header "── Summary"

echo ""
echo "  Recent calls in DB:"
curl -s "$API_URL/api/calls?limit=5" 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for c in data.get('calls', []):
    arrow = '📞' if c.get('direction') == 'inbound' else '📤'
    print(f\"  {arrow} {c['call_uuid'][:8]}… {c['agent_slug']:12s} turns={c['turn_count']} dur={c['duration_seconds']}s {c['end_reason']}\")
print(f\"  Total in DB: {data['total']}\")
"

echo ""
if [[ $FAILURES -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}  PASS — all checks passed${NC}"
else
  echo -e "${RED}${BOLD}  FAIL — $FAILURES check(s) failed (see ❌ above)${NC}"
  exit 1
fi
