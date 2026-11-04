#!/bin/bash
# Orbtile Adapter Protocol v1.2 — reference adapter in shell + curl.
# Its registration intentionally omits the optional v1.2 icon and palette.states fields.
# Shows one "build" session on the keypad while a command runs:
#   ref-shell.sh make test
# Transport: HTTP over the Unix-domain socket named in the discovery file (curl --unix-socket).
# The token never appears in argv: curl reads it from a process-substitution config
# written by the printf builtin (review finding P8).
set -u
API_FILE="${ORBTILE_API_FILE:-$HOME/Library/Application Support/Orbtile/adapter-api.json}"
# Without ORBTILE_API_FILE: Orbtile's file, or Orbtile Enterprise's when only that one exists.
ENTERPRISE_API_FILE="$HOME/Library/Application Support/Orbtile Enterprise/adapter-api.json"
if [ -z "${ORBTILE_API_FILE:-}" ] && [ ! -e "$API_FILE" ] && [ -e "$ENTERPRISE_API_FILE" ]; then
  API_FILE="$ENTERPRISE_API_FILE"
fi
ID="dev.example.shell"
SID="build-$$"
# Green working orb. A base hue in the calm arc needs an alarm colour of its own (PALETTE.md §3 rule 5).
REG='{"name":"Shell","palette":{"baseHue":150,"attentionHue":330},"glyph":{"symbol":"terminal","monogram":"Sh"},"version":"1.0.0"}'

load() {  # reads the discovery file; fails when Orbtile is off or not running
  [ -r "$API_FILE" ] || return 1
  SOCK=$(/usr/bin/plutil -extract socketPath raw -o - "$API_FILE") || return 1
  BASE=$(/usr/bin/plutil -extract basePath raw -o - "$API_FILE") || return 1
  TOKEN=$(/usr/bin/plutil -extract token raw -o - "$API_FILE") || return 1
  [ -S "$SOCK" ]
}

api() {  # api METHOD PATH [JSON] -> prints the HTTP status
  local body="${3:-}"; [ -n "$body" ] || body='{}'
  curl -sS -m 2 --noproxy '*' --unix-socket "$SOCK" -o /dev/null -w '%{http_code}' -X "$1" \
    -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN") \
    -H 'Content-Type: application/json' --data-binary "$body" "http://localhost$BASE$2"
}

json_str() {  # json_str TEXT MAXBYTES -> a JSON string: control characters become spaces, cut at MAXBYTES
  local s  # bytes without splitting a UTF-8 character (iconv -c drops the cut rest)
  s=$(printf '%s' "$1" | LC_ALL=C tr '\000-\037' ' ' | head -c "$2" | /usr/bin/iconv -c -f UTF-8 -t UTF-8)
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

load || { echo "Orbtile adapter API is off or Orbtile is not running" >&2; exec "$@"; }
TITLE=$(json_str "$*" 100)
RUNNING="{\"title\":$TITLE,\"activity\":\"tool\",\"toolLabel\":\"Running\",\"cwd\":$(json_str "$PWD" 1024),\"ttlSeconds\":3600}"
api PUT "/adapters/$ID" "$REG" >/dev/null
api POST "/adapters/$ID/sessions/$SID" "$RUNNING" >/dev/null

# Heartbeat while the command runs: Orbtile shows an adapter silent for 15 s as offline and ends its registration
# after 60 s. A 401 or 404 (new token, Orbtile relaunched, registration ended) re-reads the file and registers again.
# The loop also ends when this script is gone (killed by a signal), and never writes to the command's output.
HEARTBEAT="${ORBTILE_HEARTBEAT_SECONDS:-10}"
heartbeat() {
  while sleep "$HEARTBEAT" && kill -0 "$1" 2>/dev/null; do
    case $(api POST "/adapters/$ID/heartbeat") in
      401|404) load && api PUT "/adapters/$ID" "$REG" >/dev/null && api POST "/adapters/$ID/sessions/$SID" "$RUNNING" ;;
    esac
  done
}
heartbeat $$ >/dev/null 2>&1 &
HB=$!

"$@"; rc=$?
kill "$HB" 2>/dev/null
wait "$HB" 2>/dev/null

# Orbtile may have restarted while the command ran (new token, no registrations, no sessions): read the file
# again, register again and send the whole session, so the final state always arrives.
if load; then
  api PUT "/adapters/$ID" "$REG" >/dev/null
  if [ $rc -eq 0 ]; then state='"activity":"done","statusDetail":"exit 0"'; else state="\"activity\":\"error\",\"statusDetail\":\"exit $rc\""; fi
  api POST "/adapters/$ID/sessions/$SID" "{\"title\":$TITLE,$state,\"ttlSeconds\":600}" >/dev/null
fi
exit $rc
