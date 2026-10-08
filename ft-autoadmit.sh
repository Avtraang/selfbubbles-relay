#!/bin/bash
# Unattended FaceTime auto-admit.
#   Outbound: ft-autoadmit.sh "<facetime-link-url>"
#   Incoming: ft-autoadmit.sh --incoming ["<link>"]   (Mac is already in the call
#             because BlueBubbles answered it; skip the open+Join steps)
#
# Snaps the FaceTime window to a FIXED rect so coordinates are deterministic,
# then: (outbound) open link -> Join; (both) poll for a waiting joiner ->
# open the accept/decline popover -> color-detect the green check (red-X to its
# left, inside window) -> System Events click to admit -> leave.
#
# Paths: this script and its Python helpers (facetime/) live in the relay
# checkout; the log, lock, trigger files and shots/ go under RELAY_DATA_DIR
# (the relay passes it; default = the checkout, i.e. where they always were).
# The AppleScript side (ft-admit-logic.scpt inside FaceTimeAdmit.app) reads the
# trigger files from ~/imsg-relay, so a different RELAY_DATA_DIR also needs
# that script's paths changed and recompiled with osacompile.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="${RELAY_DATA_DIR:-$HERE}"
HELPERS="$HERE/facetime"
PY="${RELAY_PYTHON:-$HERE/venv/bin/python}"
LOG="$BASE/ft-auto.log"

MODE="outbound"
if [ "${1:-}" = "--incoming" ]; then MODE="incoming"; shift; fi
LINK="${1:-}"

# Fixed window rect (screen points). Fully on-screen (display 3360x945).
FX=1800; FY=120; FW=984; FH=615
RIGHT=$((FX+FW)); BOTTOM=$((FY+FH))
JOINX=$((RIGHT-66)); JOINY=$((BOTTOM-42))     # Join button (pre-join)
PILLX=$((FX+189));   PILLY=$((FY+26))         # "N Person Waiting" pill
ZONEX=$((PILLX+30)); ZONEY=$((PILLY+120))     # popover button zone (to hold open)

say(){ printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" | tee -a "$LOG"; }
snap(){ printf '%s,%s,%s,%s' "$FX" "$FY" "$FW" "$FH" > "$BASE/ft-snap"; sleep 1.2; }
shot(){ rm -f "$BASE/ft-shot"; touch "$BASE/ft-shot"; sleep 2; }
cgclick(){ python3 "$HELPERS/clickcg.py" "$1" "$2" 1 >/dev/null 2>&1; }
seclick(){ printf '%s,%s' "$1" "$2" > "$BASE/ft-clickxy"; sleep 1.4; }   # System Events click
jig(){ python3 "$HELPERS/jiggle.py" "$1" "$2" "${3:-1}" >/dev/null 2>&1; }
haswin(){ [ -n "$(cat "$BASE/ft-frame-out.txt" 2>/dev/null)" ]; }

# Single-instance lock (mkdir is atomic): the app can double-fire /ft_link, and
# two runs racing on the shared trigger files corrupt each other.
LOCK="$BASE/ft-auto.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # stale lock older than 3 min? take it over.
  if [ -n "$(find "$LOCK" -mmin +3 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 0
  else say "another auto-admit already running; skip"; exit 0; fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

say "=== auto-admit ($MODE): ${LINK:0:44}..."
# FaceTime must be running (do NOT close its windows -- that quits it and kills
# BlueBubbles' injected instance). `open -a FaceTime` also raises it to front.
open -a FaceTime >/dev/null 2>&1; sleep 2

if [ "$MODE" = "outbound" ]; then
  # Force the link into the FaceTime app (plain `open` may hand it to the browser).
  for t in 1 2 3 4; do
    if open -a FaceTime "$LINK" 2>>"$LOG"; then break; fi
    say "open retry $t"; sleep 1.5
  done
  sleep 4
  rm -f "$BASE/ft-frame-out.txt"; snap
  if ! haswin; then sleep 1; snap; fi        # retry once (helper may have been slow)
  if ! haswin; then say "no FaceTime window; abort"; exit 1; fi
  # If the Mac was already in a call, opening a link shows an "End Current Call?"
  # dialog with a blue "Join New Call" button -- detect + click it, then we're
  # heading straight into the new call (skip the pre-join Join click).
  shot
  DLG=$("$PY" "$HELPERS/findblue.py" "$BASE/shots/full.png" 0.5 "$FX" "$FY" "$FW" "$FH" 2>/dev/null)
  if [ "$DLG" != "NONE" ] && [ -n "$DLG" ]; then
    seclick "${DLG%,*}" "${DLG#*,}"; say "dismissed End-Current-Call -> Join New Call"
    sleep 4; snap
  else
    jig $((FX+FW/2)) $((FY+FH/2)) 1
    seclick $JOINX $JOINY; say "clicked Join"
    sleep 4
  fi
else
  # Incoming: the Mac is already in the call (BB answered). Just fix the window.
  sleep 2
  rm -f "$BASE/ft-frame-out.txt"; snap
  if ! haswin; then sleep 1; snap; fi        # retry once (helper may have been slow)
  if ! haswin; then say "no FaceTime window; abort"; exit 1; fi
  say "incoming: already in call, going to admit loop"
fi

# Multi-admit window: keep admitting everyone who requests to join. Stop once
# someone's been admitted AND no new joiner appears for IDLE_STOP rounds (so a
# 2-person call admits both), or time out if nobody ever joins.
ADMITTED=0; IDLE=0; IDLE_STOP=3
for i in $(seq 1 40); do
  snap                                    # re-fix window each round
  jig $((FX+200)) $((FY+120)) 1           # wake controls
  cgclick $PILLX $PILLY                    # open the waiting popover (raw click works here)
  ( python3 "$HELPERS/hold.py" $ZONEX $ZONEY 6 >/dev/null 2>&1 ) & HP=$!
  sleep 1.5; shot                          # capture while popover held open
  XY=$("$PY" "$HELPERS/findadmit.py" "$BASE/shots/full.png" 0.5 "$FX" "$FY" "$FW" "$FH" 80 2>/dev/null)
  kill $HP 2>/dev/null; sleep 0.2
  if [ "$XY" != "NONE" ] && [ -n "$XY" ]; then
    CX=${XY%,*}; CY=${XY#*,}
    ( python3 "$HELPERS/hold.py" $CX $CY 4 >/dev/null 2>&1 ) & HP2=$!
    sleep 0.4
    seclick "$CX" "$CY"                    # System Events click on green check = admits
    sleep 0.6; kill $HP2 2>/dev/null
    ADMITTED=$((ADMITTED+1)); IDLE=0
    say "*** admitted #$ADMITTED at $XY"
    sleep 1                                # quick re-check for more waiting joiners
  else
    say "round $i: none waiting (admitted=$ADMITTED)"
    IDLE=$((IDLE+1))
    if [ "$ADMITTED" -gt 0 ] && [ "$IDLE" -ge "$IDLE_STOP" ]; then break; fi
    if [ "$ADMITTED" -eq 0 ] && [ "$i" -ge 12 ]; then break; fi
    sleep 2
  fi
done

if [ "$ADMITTED" -gt 0 ]; then
  say "=== admitted $ADMITTED total"
  # No more joiners coming -> leave (everyone's in). LEAVE_DELAY=0 keeps Mac in.
  LD="${LEAVE_DELAY:-4}"
  if [ "$LD" != "0" ]; then
    sleep "$LD"; snap
    seclick $((RIGHT-42)) $((BOTTOM-47))  # red Leave (X), bottom-right
    say "*** Mac left the call"
  fi
  exit 0
fi
say "=== no waiting joiner found (timeout)"; exit 2
