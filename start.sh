#!/usr/bin/env bash
# 启动网页版文件共享与收集服务（含同步看课件）
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
PASS="${TEACHER_PASS:-}"

PY="$(command -v python3 || command -v python)"
echo "使用解释器: $PY"

mkdir -p share submit

ARGS=(--host "$HOST" --port "$PORT")
[ -n "$PASS" ] && ARGS+=(--teacher-pass "$PASS")
[ -n "$PPT" ] && ARGS+=(--ppt "$PPT")

exec "$PY" fileserver.py "${ARGS[@]}" "$@"
