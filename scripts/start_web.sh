#!/usr/bin/env bash
# scripts/start_web.sh —— 容器内启动 web 服务（Gunicorn 多 worker）
set -e

# Prometheus 多进程目录：必须在这里设（不能在 Python 里设，否则不传给子进程），且启动前清空
export PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc
rm -rf "$PROMETHEUS_MULTIPROC_DIR"
mkdir -p "$PROMETHEUS_MULTIPROC_DIR"

# Gunicorn 管多个 Uvicorn worker（多核 + 高并发 + 容错）
# pip 把依赖装在系统环境，直接用 gunicorn（不是 uv run gunicorn）
exec gunicorn api.app:app \
    -k uvicorn.workers.UvicornWorker \
    --workers 4 \
    --bind 0.0.0.0:8000 \
    --timeout 120 \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 50 \
    --config scripts/gunicorn_conf.py