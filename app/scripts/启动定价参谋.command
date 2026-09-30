#!/bin/zsh
# 双击启动定价参谋：打开浏览器 http://127.0.0.1:8321 。关掉这个窗口就停止。数据在 data/advisor.db，重启电脑后还在。
cd "$(dirname "$0")/.." || exit 1
UV="$(command -v uv || echo /opt/homebrew/bin/uv)"
exec "$UV" run advisor
