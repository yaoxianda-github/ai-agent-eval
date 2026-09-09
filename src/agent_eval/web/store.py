"""run 历史索引（V2.1）：SQLite 轻量索引。

权威数据仍是 results/runs/<run_id>/run.json（引擎写入）；
SQLite 仅做可筛选的历史查询索引，启动/运行后自动重建，避免每次全盘解析 JSON。

V2.7：新增 batches 表（多 Agent 对比批次），runs 表加 batch_id 列关联批次。
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
    batch_id   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_task   ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_agent  ON runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    agents     TEXT NOT NULL DEFAULT '[]',
    task_ids   TEXT NOT NULL DEFAULT '[]',
    scope      TEXT NOT NULL DEFAULT '',
    runs       INTEGER NOT NULL DEFAULT 1,
    status     TEXT NOT NULL DEFAULT 'running',
    total_runs INTEGER NOT NULL DEFAULT 0,
    done_runs  INTEGER NOT NULL DEFAULT 0,
    summary    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """老库平滑升级：runs 缺 batch_id 列时补上（SQLite 不支持 IF NOT EXISTS 加列）。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "batch_id" not in cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
        # batch_id 列就绪后再建其索引（新库老库统一在此建，避免 executescript 时序问题）
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id)")
        # batches 表加 last_heartbeat 字段（V2.7.1：僵尸批次检测用心跳而非创建时间）
        bcols = {r[1] for r in self._conn.execute("PRAGMA table_info(batches)").fetchall()}
        if "last_heartbeat" not in bcols:
            self._conn.execute("ALTER TABLE batches ADD COLUMN last_heartbeat TEXT NOT NULL DEFAULT ''")

    def insert_run(self, rec: dict, batch_id: str = "") -> None:
        m = rec.get("metrics", {})
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, agent_id, agent_ver, task_id, task_level, status,
                    score, weight, pass_rate, duration_s, steps, batch_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    batch_id or "",
                    _now(),
                ),
            )
            self._conn.commit()

    def list_runs(
        self,
        limit: int = 20,
        offset: int = 0,
        task_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict], int]:
        """分页查询运行记录，返回 (记录列表, 满足筛选条件的总数)。"""
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
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM runs{where}", args).fetchone()[0]
            rows = self._conn.execute(
                sql + where + " ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)],
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM runs LIMIT 1").description]
        return [dict(zip(cols, r)) for r in rows], int(total)

    # ---------- 批次（V2.7 多 Agent 对比） ----------
    def insert_batch(self, b: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO batches
                   (batch_id,label,agents,task_ids,scope,runs,status,
                    total_runs,done_runs,summary,created_at,finished_at,last_heartbeat)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    b["batch_id"],
                    b.get("label", ""),
                    json.dumps(b.get("agents", []), ensure_ascii=False),
                    json.dumps(b.get("task_ids", []), ensure_ascii=False),
                    b.get("scope", ""),
                    int(b.get("runs", 1)),
                    b.get("status", "running"),
                    int(b.get("total_runs", 0)),
                    int(b.get("done_runs", 0)),
                    json.dumps(b.get("summary", {}), ensure_ascii=False),
                    b.get("created_at", _now()),
                    b.get("finished_at", ""),
                    b.get("last_heartbeat", _now()),  # 创建时初始化心跳
                ),
            )
            self._conn.commit()

    def update_batch(self, batch_id: str, **fields) -> None:
        if not fields:
            return
        if "agents" in fields:
            fields["agents"] = json.dumps(fields["agents"], ensure_ascii=False)
        if "task_ids" in fields:
            fields["task_ids"] = json.dumps(fields["task_ids"], ensure_ascii=False)
        if "summary" in fields:
            fields["summary"] = json.dumps(fields["summary"], ensure_ascii=False)
        # 自动更新心跳：除非显式传入 last_heartbeat（如启动清理时设为空）
        if "last_heartbeat" not in fields:
            fields["last_heartbeat"] = _now()
        keys = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE batches SET {keys} WHERE batch_id=?",
                [*fields.values(), batch_id],
            )
            self._conn.commit()

    @staticmethod
    def _batch_row_to_dict(row: tuple, cols: list) -> dict:
        d = dict(zip(cols, row))
        for k in ("agents", "task_ids"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (ValueError, TypeError):
                d[k] = []
        try:
            d["summary"] = json.loads(d.get("summary") or "{}")
        except (ValueError, TypeError):
            d["summary"] = {}
        return d

    def get_batch(self, batch_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.execute("SELECT * FROM batches LIMIT 1").description]
        return self._batch_row_to_dict(row, cols)

    def list_batches(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC, batch_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM batches LIMIT 1").description]
        return [self._batch_row_to_dict(r, cols) for r in rows]

    def list_run_ids_by_batch(self, batch_id: str) -> list[str]:
        """返回批次下全部 run_id（按 agent、task、创建顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE batch_id=? ORDER BY agent_id, task_id, created_at",
                (batch_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def delete_batch(self, batch_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM batches WHERE batch_id=?", (batch_id,))
            self._conn.execute("UPDATE runs SET batch_id='' WHERE batch_id=?", (batch_id,))
            self._conn.commit()

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
