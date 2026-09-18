#!/bin/bash
# 双击运行：增量抓取（有变化才重抓并重写页面），然后打开简报
cd "$(dirname "$0")" || exit 1
PY=/Users/pzw/.workbuddy-ai/binaries/python/versions/3.13.12/bin/python3
[ -x "$PY" ] || PY=python3
"$PY" fetch.py && open index.html
