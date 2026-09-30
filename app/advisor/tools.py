"""命令行小工具：备份与恢复（PT-12）。

uv run advisor-backup                 把 data/advisor.db 备份到 data/backups/定价参谋-年月日-时分秒.db
uv run advisor-restore <备份文件>     先把当前库另存一份，再用备份文件覆盖当前库
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .db import db_path


def _copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)  # SQLite 在线备份：服务开着也能备份，得到一致的快照


def backup(dest_dir: Path | None = None) -> Path:
    src = db_path()
    if not src.exists():
        raise SystemExit(f"没有找到数据库：{src}")
    out = (dest_dir or src.parent / "backups") / f"定价参谋-{datetime.now():%Y%m%d-%H%M%S}.db"
    _copy(src, out)
    return out


def backup_main():
    print(f"已备份到 {backup()}")


def restore_main():
    if len(sys.argv) < 2:
        raise SystemExit("用法：uv run advisor-restore <备份文件>")
    src = Path(sys.argv[1]).resolve()
    if not src.exists():
        raise SystemExit(f"没有找到备份文件：{src}")
    target = db_path()
    if target.exists():
        safety = backup(target.parent / "backups")
        print(f"恢复前先把当前库另存到 {safety}")
    _copy(src, target)
    print(f"已用 {src.name} 恢复到 {target}")
