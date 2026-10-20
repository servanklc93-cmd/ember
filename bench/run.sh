#!/usr/bin/env bash
# Runs redis-benchmark against a real redis-server and ember, side by side.
# Needs redis installed (brew install redis / apt install redis-server).
set -euo pipefail

N=${N:-50000}
CLIENTS=${CLIENTS:-50}
TESTS=${TESTS:-set,get,incr,lpush,lpop,sadd,hset}

redis-server --port 6391 --save '' --appendonly no >/dev/null &
REDIS_PID=$!
python -m ember --port 6390 >/dev/null 2>&1 &
EMBER_PID=$!
trap 'kill $REDIS_PID $EMBER_PID 2>/dev/null' EXIT
sleep 1

for target in "redis 6391" "ember 6390"; do
  set -- $target
  echo "## $1"
  redis-benchmark -p "$2" -n "$N" -c "$CLIENTS" -t "$TESTS" -q 2>/dev/null | tr '\r' '\n' | grep "requests per second"
  echo "## $1 (pipeline 16)"
  redis-benchmark -p "$2" -n $((N * 4)) -c "$CLIENTS" -P 16 -t set,get -q 2>/dev/null | tr '\r' '\n' | grep "requests per second"
  echo
done
