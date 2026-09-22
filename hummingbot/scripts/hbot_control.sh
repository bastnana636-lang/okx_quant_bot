#!/bin/sh
# Keep make stop idempotent without hiding Docker or graceful-shutdown failures.
set -eu

action=${1:?Expected stop or status}
case "$action" in
    stop|status) ;;
    *) echo "Expected stop or status" >&2; exit 1 ;;
esac

# Unlike a failed inspect, an empty successful listing means the container is absent.
state=$(docker container ls -a --filter 'name=^/hummingbot$' --format '{{.State}}')
case "$state" in
    running) ;;
    ''|created|exited|dead)
        echo "Hummingbot is stopped (container: ${state:-not created})."
        exit 0
        ;;
    *) echo "Hummingbot container is $state; check Docker before retrying." >&2; exit 1 ;;
esac

if [ "$action" = status ]; then
    exec docker exec hummingbot hbot status
fi

if output=$(docker exec hummingbot hbot stop 2>&1); then
    printf '%s\n' "$output"
else
    code=$?
    case "$code" in
        # hbot's stable NOT_FOUND / NOT_RUNNING exit codes.
        2|3) echo "Hummingbot is already stopped." ;;
        *) printf '%s\n' "$output" >&2; exit "$code" ;;
    esac
fi
