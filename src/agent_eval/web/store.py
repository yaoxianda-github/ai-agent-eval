"""run 历史索引（V2.1）：SQLite 轻量索引。

权威数据仍是 results/runs/<run_id>/run.json（引擎写入）；
SQLite 仅做可筛选的历史查询索引，启动/运行后自动重建，避免每次全盘解析 JSON。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    agent_ver  TEXT NOT NULL DEFAULT '',
    task_id    TEXT NOT NULL,
    task_level TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0,
    weight     REAL NOT NULL DEFAULT 0,
    pass_rate  REAL NOT NULL DEFAULT 0,
    duration_s REAL NOT NULL DEFAULT 0,
    steps      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task   ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_agent  ON runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
"""


class RunStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：run 由后台线程写入、API 请求线程读取；
        # 用锁保证同一时刻只有一个线程访问连接。
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def insert_run(self, rec: dict) -> None:
        m = rec.get("metrics", {})
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, agent_id, agent_ver, task_id, task_level, status,
                    score, weight, pass_rate, duration_s, steps, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["run_id"],
                    rec.get("agent_id", ""),
                    rec.get("agent_ver", ""),
                    rec.get("task_id", ""),
                    rec.get("task_level", ""),
                    rec.get("status", ""),
                    float(m.get("score", 0.0)),
                    float(m.get("weight", 0.0)),
                    float(m.get("pass_rate", 0.0)),
                    float(rec.get("duration_s", 0.0)),
                    len(rec.get("steps", [])),
                    _now(),
                ),
            )
            self._conn.commit()

    def list_runs(
        self,
        limit: int = 100,
        task_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM runs"
        conds: list[str] = []
        args: list = []
        if task_id:
            conds.append("task_id=?")
            args.append(task_id)
        if agent_id:
            conds.append("agent_id=?")
            args.append(agent_id)
        if status:
            conds.append("status=?")
            args.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM runs LIMIT 1").description]
        return [dict(zip(cols, r)) for r in rows]

    def rebuild(self, results_dir: Path) -> int:
        """扫描 results_dir/*/run.json 重建索引，返回已索引 run 数。"""
        n = 0
        for p in sorted(Path(results_dir).glob("*/run.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                self.insert_run(rec)
                n += 1
            except Exception:  # noqa: BLE001 - 单条损坏不影响整体
                continue
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
