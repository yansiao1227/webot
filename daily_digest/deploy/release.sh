#!/bin/sh
# Two-phase publication: do not retire prior code before anonymous readback.
set -eu
cd "$(dirname "$0")"
release=$(basename "$PWD")
case "$release" in d????????-????????????????????????????????) ;; *) exit 2;; esac
case "$release" in *[!a-z0-9-]*) exit 2;; esac
action=${1:-status}
old=${2:-daily-digest}
case "$old" in *[!a-zA-Z0-9_.-]*|'') exit 2;; esac
case "$action" in status|activate|commit|rollback) ;; *) exit 2;; esac
image="daily-digest:$release"
candidate="daily-digest-check-$release"
previous="daily-digest-previous-$release"
exec 9>/opt/daily-digest-deploy.lock
flock -w 5 9 || { echo deployment_busy >&2; exit 1; }
phase() { if [ -f .phase ]; then cat .phase; else printf prepared; fi; }
set_phase() { printf '%s\n' "$1" > .phase.tmp; mv .phase.tmp .phase; }
current_release() { docker inspect -f '{{index .Config.Labels "daily_digest.release"}}' daily-digest 2>/dev/null || true; }
status() {
 current=$(current_release)
 case "$current" in *[!a-z0-9-]*|'') current=legacy;; esac
 printf '{"phase":"%s","release_id":"%s","current_release":"%s"}\n' "$(phase)" "$release" "$current"
}
if [ "$action" = status ]; then status; exit 0; fi
cleanup() { docker rm -f "$candidate" >/dev/null 2>&1 || true; }
restoring=false
finish() {
 result=$?
 trap - EXIT HUP INT TERM
 set +e
 cleanup
 if [ "$result" -ne 0 ] && [ "$(phase)" = activating ] && [ "$restoring" = false ] && [ -s previous-image ]; then
  restore_previous_code || echo rollback_unconfirmed >&2
 fi
 cleanup
 exit "$result"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
run_app() {
 restart=unless-stopped
 [ "$2" != "$candidate" ] || restart=no
 docker run -d --name "$2" --restart "$restart" --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m --cap-drop ALL --security-opt no-new-privileges \
  --label "daily_digest.release=$release" -v "$4/editions.sqlite:/app/editions.sqlite:ro" -p "$3:8080" "$1"
}
verify_http() {
 attempts=0
 until curl --max-time 2 -fsS "http://127.0.0.1:$1/" >/dev/null; do
  attempts=$((attempts + 1)); [ "$attempts" -lt 15 ] || return 1; sleep 1
 done
 docker run --rm --read-only --network host --cap-drop ALL --security-opt no-new-privileges \
  -v "$2/manifest.json:/app/manifest.json:ro" "$image" node scripts/verify-release.mjs http "http://127.0.0.1:$1"
}
capacity() {
 bytes=$(wc -c < "$PWD/editions.sqlite")
 [ "$bytes" -le 201326592 ] || { echo snapshot_capacity_exceeded >&2; return 1; }
 available=$(df -Pk /opt/daily-digest/releases | awk 'NR==2 {print $4}')
 required=$((bytes / 1024 * 5 + 524288))
 [ "$available" -ge "$required" ] || { echo disk_capacity_exceeded >&2; return 1; }
 memory=$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)
 [ "$memory" -ge "$((bytes / 1024 * 2 + 131072))" ] || { echo memory_capacity_exceeded >&2; return 1; }
}
restore_previous_code() {
 [ -s previous-image ] || { echo no_previous_image >&2; return 1; }
 restoring=true
 previous_name=$(cat previous-name)
 if [ "$previous_name" != daily-digest ]; then
  [ "$(phase)" != confirmed ] || { echo previous_application_not_digest >&2; return 1; }
  docker rm -f daily-digest >/dev/null 2>&1 || true
  docker rename "$previous" "$previous_name" || return 1
  docker start "$previous_name" || return 1
  set_phase rolled_back
  return
 fi
 previous_image=$(cat previous-image)
 snapshot_dir=$PWD
 if [ -s /opt/daily-digest-current ]; then
  latest=$(cat /opt/daily-digest-current)
  case "$latest" in d????????-????????????????????????????????)
   [ ! -f "/opt/daily-digest/releases/$latest/manifest.json" ] || snapshot_dir="/opt/daily-digest/releases/$latest";;
  esac
 fi
 # A newer uncommitted but already serving release also owns authoritative history.
 running=$(current_release)
 case "$running" in d????????-????????????????????????????????)
  [ ! -f "/opt/daily-digest/releases/$running/manifest.json" ] || snapshot_dir="/opt/daily-digest/releases/$running";;
 esac
 if [ "$running" = "$release" ]; then snapshot_dir=$PWD; fi
 case "$(phase)" in activating|activated)
  [ -n "$running" ] || snapshot_dir=$PWD;;
 esac
 cleanup
 run_app "$previous_image" "$candidate" '127.0.0.1:18081' "$snapshot_dir" || return 1
 verify_http 18081 "$snapshot_dir" || return 1
 docker rm -f daily-digest >/dev/null 2>&1 || true
 if ! run_app "$previous_image" daily-digest 80 "$snapshot_dir"; then echo rollback_start_failed >&2; return 1; fi
 verify_http 80 "$snapshot_dir" || return 1
 set_phase rolled_back
}
if [ "$action" = rollback ]; then
 if [ "$(phase)" = rolled_back ]; then status; exit 0; fi
 restore_previous_code; status; exit 0
fi
if [ "$action" = commit ]; then
 if [ "$(phase)" = confirmed ]; then status; exit 0; fi
 case "$(phase)" in activating|activated) ;; *) echo release_not_activated >&2; exit 1;; esac
 [ "$(current_release)" = "$release" ] || { echo release_not_current >&2; exit 1; }
 verify_http 80 "$PWD"
 printf '%s\n' "$release" > /opt/daily-digest-current.tmp
 mv /opt/daily-digest-current.tmp /opt/daily-digest-current
 set_phase confirmed
 # Retain stopped previous container, image and snapshot for recovery.
 status; exit 0
fi
case "$(phase)" in activated|confirmed) status; exit 0;; rolled_back|candidate_failed) echo release_needs_review >&2; exit 1;; esac
if [ "$old" != daily-digest ] && docker container inspect daily-digest >/dev/null 2>&1; then
 echo use_normal_update_command >&2
 exit 1
fi
if [ "$(phase)" = activating ] && [ "$(current_release)" = "$release" ]; then
 if verify_http 80 "$PWD"; then set_phase activated; status; exit 0; fi
 restore_previous_code; exit 1
fi
capacity
docker build -t "$image" -f Dockerfile .
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges "$image" node scripts/verify-release.mjs snapshot
cleanup
run_app "$image" "$candidate" '127.0.0.1:18081' "$PWD"
if ! verify_http 18081 "$PWD"; then set_phase candidate_failed; exit 1; fi
# The remote lock is held: a concurrently published date cannot disappear between check and switch.
if [ "$old" = daily-digest ] && docker container inspect daily-digest >/dev/null 2>&1; then
 docker run --rm --read-only --network host --cap-drop ALL --security-opt no-new-privileges \
  "$image" node scripts/verify-release.mjs history http://127.0.0.1:80
fi
if [ ! -f previous-id ] && docker container inspect "$old" >/dev/null 2>&1; then
 docker inspect -f '{{.Id}}' "$old" > previous-id
 docker inspect -f '{{.Config.Image}}' "$old" > previous-image
 printf '%s\n' "$old" > previous-name
fi
set_phase activating
if docker container inspect "$old" >/dev/null 2>&1; then
 [ -f previous-id ] && [ "$(docker inspect -f '{{.Id}}' "$old")" = "$(cat previous-id)" ] || { echo current_service_changed >&2; exit 1; }
 docker stop "$old"
 if ! docker rename "$old" "$previous"; then docker start "$old"; exit 1; fi
fi
if ! run_app "$image" daily-digest 80 "$PWD"; then restore_previous_code; exit 1; fi
if ! verify_http 80 "$PWD"; then restore_previous_code; exit 1; fi
set_phase activated
status
