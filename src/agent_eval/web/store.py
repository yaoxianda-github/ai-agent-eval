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

CREATE TABLE IF NOT EXISTS badcases (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL DEFAULT '',
    task_id     TEXT NOT NULL DEFAULT '',
    agent_id    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT 'other',
    severity    TEXT NOT NULL DEFAULT 'P2',
    status      TEXT NOT NULL DEFAULT 'pending',
    root_cause  TEXT NOT NULL DEFAULT '',
    fix_plan    TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    regression_task_id TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_badcases_task    ON badcases(task_id);
CREATE INDEX IF NOT EXISTS idx_badcases_agent   ON badcases(agent_id);
CREATE INDEX IF NOT EXISTS idx_badcases_status  ON badcases(status);
CREATE INDEX IF NOT EXISTS idx_badcases_severity ON badcases(severity);
CREATE INDEX IF NOT EXISTS idx_badcases_category ON badcases(category);

-- 经验记忆表（V2.9）：从 badcase 沉淀的可复用经验，运行时自动召回注入
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    trigger_keywords TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：触发关键词
    task_tags       TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：适用的任务标签（如 file, text, code）
    source_badcase_id TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.5,  -- 置信度 0-1
    status          TEXT NOT NULL DEFAULT 'active',  -- active/inactive
    usage_count     INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_source ON memories(source_badcase_id);
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
        # badcases 表加 regression_task_id 字段（V2.8.1：badcase 转化为回归评测用例）
        bccols = {r[1] for r in self._conn.execute("PRAGMA table_info(badcases)").fetchall()}
        if "regression_task_id" not in bccols:
            self._conn.execute("ALTER TABLE badcases ADD COLUMN regression_task_id TEXT NOT NULL DEFAULT ''")

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

    # ---------- Badcase 管理（V2.8 评测 badcase 积累） ----------
    BADCASE_CATEGORIES = [
        "reasoning",      # 推理错误
        "tool_use",       # 工具使用错误
        "format",         # 输出格式错误
        "timeout",        # 超时
        "crash",          # 异常崩溃
        "hallucination",  # 幻觉/编造
        "planning",       # 规划错误
        "context",        # 上下文理解错误
        "other",          # 其他
    ]
    BADCASE_SEVERITIES = ["P0", "P1", "P2", "P3"]
    BADCASE_STATUSES = ["pending", "analyzing", "fixed", "ignored"]

    def insert_badcase(self, b: dict) -> str:
        """插入一条 badcase，返回 badcase id。"""
        import uuid as _uuid
        bid = b.get("id") or _uuid.uuid4().hex[:12]
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO badcases
                   (id,run_id,task_id,agent_id,title,description,category,severity,
                    status,root_cause,fix_plan,tags,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    bid,
                    b.get("run_id", ""),
                    b.get("task_id", ""),
                    b.get("agent_id", ""),
                    b.get("title", ""),
                    b.get("description", ""),
                    b.get("category", "other"),
                    b.get("severity", "P2"),
                    b.get("status", "pending"),
                    b.get("root_cause", ""),
                    b.get("fix_plan", ""),
                    json.dumps(b.get("tags", []), ensure_ascii=False),
                    b.get("created_at", now),
                    now,
                ),
            )
            self._conn.commit()
        return bid

    def update_badcase(self, bid: str, **fields) -> None:
        """更新 badcase 字段。"""
        if not fields:
            return
        if "tags" in fields:
            fields["tags"] = json.dumps(fields["tags"], ensure_ascii=False)
        fields["updated_at"] = _now()
        keys = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE badcases SET {keys} WHERE id=?", [*fields.values(), bid])
            self._conn.commit()

    def delete_badcase(self, bid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM badcases WHERE id=?", (bid,))
            self._conn.commit()

    def get_badcase(self, bid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM badcases WHERE id=?", (bid,)).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.execute("SELECT * FROM badcases LIMIT 1").description]
        d = dict(zip(cols, row))
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except (ValueError, TypeError):
            d["tags"] = []
        return d

    def list_badcases(
        self,
        limit: int = 20,
        offset: int = 0,
        task_id: str | None = None,
        agent_id: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        status: str | None = None,
    ) -> tuple[list[dict], int]:
        """分页查询 badcase，返回 (记录列表, 总数)。"""
        sql = "SELECT * FROM badcases"
        conds: list[str] = []
        args: list = []
        if task_id:
            conds.append("task_id=?")
            args.append(task_id)
        if agent_id:
            conds.append("agent_id=?")
            args.append(agent_id)
        if category:
            conds.append("category=?")
            args.append(category)
        if severity:
            conds.append("severity=?")
            args.append(severity)
        if status:
            conds.append("status=?")
            args.append(status)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM badcases{where}", args).fetchone()[0]
            rows = self._conn.execute(
                sql + where + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                args + [int(limit), int(offset)],
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM badcases LIMIT 1").description]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            result.append(d)
        return result, int(total)

    def badcase_stats(self) -> dict:
        """badcase 统计：按状态/严重程度/分类分组计数。"""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM badcases").fetchone()[0]
            by_status = dict(self._conn.execute("SELECT status, COUNT(*) FROM badcases GROUP BY status").fetchall())
            by_severity = dict(self._conn.execute("SELECT severity, COUNT(*) FROM badcases GROUP BY severity").fetchall())
            by_category = dict(self._conn.execute("SELECT category, COUNT(*) FROM badcases GROUP BY category").fetchall())
        return {
            "total": total,
            "by_status": by_status,
            "by_severity": by_severity,
            "by_category": by_category,
        }

    def list_regression_badcases(self) -> list[dict]:
        """获取所有已转化为回归评测用例的 badcase（regression_task_id 不为空）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM badcases WHERE regression_task_id != '' ORDER BY created_at DESC"
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM badcases LIMIT 1").description]
        result = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                d["tags"] = json.loads(d.get("tags") or "[]")
            except (ValueError, TypeError):
                d["tags"] = []
            result.append(d)
        return result

    # ---------- 经验记忆（V2.9 从 badcase 沉淀可复用经验） ----------
    def insert_memory(self, rec: dict) -> str:
        """创建经验记忆，返回 memory_id。"""
        import uuid
        mid = rec.get("id") or uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                """INSERT INTO memories
                   (id, title, content, trigger_keywords, task_tags, source_badcase_id,
                    confidence, status, usage_count, success_count, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    mid,
                    rec.get("title", ""),
                    rec.get("content", ""),
                    json.dumps(rec.get("trigger_keywords", []), ensure_ascii=False),
                    json.dumps(rec.get("task_tags", []), ensure_ascii=False),
                    rec.get("source_badcase_id", ""),
                    float(rec.get("confidence", 0.5)),
                    rec.get("status", "active"),
                    int(rec.get("usage_count", 0)),
                    int(rec.get("success_count", 0)),
                    now, now,
                ),
            )
            self._conn.commit()
        return mid

    def update_memory(self, mid: str, **fields) -> bool:
        """更新记忆字段，返回是否成功。"""
        allowed = {"title", "content", "trigger_keywords", "task_tags", "confidence", "status"}
        sets = []
        args = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("trigger_keywords", "task_tags"):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=?")
            args.append(v)
        if not sets:
            return False
        sets.append("updated_at=?")
        args.append(datetime.now().isoformat(timespec="seconds"))
        args.append(mid)
        with self._lock:
            cur = self._conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id=?", args)
            self._conn.commit()
        return cur.rowcount > 0

    def delete_memory(self, mid: str) -> bool:
        """删除记忆。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM memories WHERE id=?", (mid,))
            self._conn.commit()
        return cur.rowcount > 0

    def get_memory(self, mid: str) -> dict | None:
        """获取记忆详情。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
            if not row:
                return None
            cols = [d[0] for d in self._conn.execute("SELECT * FROM memories LIMIT 1").description]
        d = dict(zip(cols, row))
        try:
            d["trigger_keywords"] = json.loads(d.get("trigger_keywords") or "[]")
        except (ValueError, TypeError):
            d["trigger_keywords"] = []
        try:
            d["task_tags"] = json.loads(d.get("task_tags") or "[]")
        except (ValueError, TypeError):
            d["task_tags"] = []
        return d

    def list_memories(self, page: int = 1, page_size: int = 20, status: str = "",
                       keyword: str = "", task_tag: str = "") -> dict:
        """记忆列表（分页+筛选）。"""
        where = []
        args = []
        if status:
            where.append("status=?")
            args.append(status)
        if keyword:
            where.append("(title LIKE ? OR content LIKE ?)")
            args.extend([f"%{keyword}%", f"%{keyword}%"])
        if task_tag:
            where.append("task_tags LIKE ?")
            args.append(f"%{task_tag}%")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(f"SELECT COUNT(*) FROM memories{where_sql}", args).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT * FROM memories{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM memories LIMIT 1").description]
        items = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                d["trigger_keywords"] = json.loads(d.get("trigger_keywords") or "[]")
            except (ValueError, TypeError):
                d["trigger_keywords"] = []
            try:
                d["task_tags"] = json.loads(d.get("task_tags") or "[]")
            except (ValueError, TypeError):
                d["task_tags"] = []
            items.append(d)
        return {"items": items, "total": total, "page": page, "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size}

    def recall_memories(self, task_tags: list[str] | None = None,
                        keywords: list[str] | None = None, limit: int = 5) -> list[dict]:
        """召回记忆：根据任务标签和关键词匹配，返回最相关的 active 记忆。

        匹配策略：
        1. task_tags 交集匹配（任务标签重叠越多，相关性越高）
        2. trigger_keywords 交集匹配（触发关键词重叠越多，相关性越高）
        3. 按 confidence 和 success_rate 加权排序

        V2.9.1：返回匹配可解释性信息（match_reasons），包括匹配的标签、关键词和分数计算过程。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE status='active' ORDER BY confidence DESC"
            ).fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM memories LIMIT 1").description]
        items = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                d["trigger_keywords"] = json.loads(d.get("trigger_keywords") or "[]")
            except (ValueError, TypeError):
                d["trigger_keywords"] = []
            try:
                d["task_tags"] = json.loads(d.get("task_tags") or "[]")
            except (ValueError, TypeError):
                d["task_tags"] = []

            # V2.9.1：匹配可解释性——记录匹配原因和分数计算过程
            match_reasons = []
            matched_tags = []
            matched_keywords = []

            # 计算相关性分数
            confidence = float(d.get("confidence", 0.5))
            score = confidence
            match_reasons.append(f"基础置信度: {confidence:.2f}")

            # 成功率加权
            usage = int(d.get("usage_count", 0))
            success = int(d.get("success_count", 0))
            if usage > 0:
                success_rate = success / usage
                score *= (0.5 + 0.5 * success_rate)  # 成功率 0-1，加权到 0.5-1.0
                match_reasons.append(f"成功率加权: ×{0.5 + 0.5 * success_rate:.2f} (使用{usage}次, 成功{success}次, 成功率{success_rate:.0%})")
            else:
                match_reasons.append("成功率加权: ×1.00 (未使用过)")

            # 任务标签匹配
            if task_tags and d["task_tags"]:
                overlap = len(set(task_tags) & set(d["task_tags"]))
                matched_tags = sorted(set(task_tags) & set(d["task_tags"]))
                if overlap > 0:
                    score *= (1 + 0.3 * overlap)
                    match_reasons.append(f"任务标签匹配: ×{1 + 0.3 * overlap:.2f} (匹配{overlap}个: {', '.join(matched_tags)})")
                else:
                    match_reasons.append("任务标签匹配: ×1.00 (无匹配)")
            else:
                match_reasons.append("任务标签匹配: ×1.00 (无标签)")

            # 关键词匹配
            if keywords and d["trigger_keywords"]:
                overlap = len(set(keywords) & set(d["trigger_keywords"]))
                matched_keywords = sorted(set(keywords) & set(d["trigger_keywords"]))
                if overlap > 0:
                    score *= (1 + 0.5 * overlap)
                    match_reasons.append(f"关键词匹配: ×{1 + 0.5 * overlap:.2f} (匹配{overlap}个: {', '.join(matched_keywords)})")
                else:
                    match_reasons.append("关键词匹配: ×1.00 (无匹配)")
            else:
                match_reasons.append("关键词匹配: ×1.00 (无关键词)")

            d["_relevance_score"] = round(score, 4)
            d["match_reasons"] = match_reasons
            d["matched_tags"] = matched_tags
            d["matched_keywords"] = matched_keywords
            items.append(d)
        # 按相关性排序
        items.sort(key=lambda x: x["_relevance_score"], reverse=True)
        return items[:limit]

    def increment_memory_usage(self, mid: str, success: bool) -> None:
        """记录记忆使用结果，用于后续质量评估。"""
        with self._lock:
            if success:
                self._conn.execute(
                    "UPDATE memories SET usage_count = usage_count + 1, success_count = success_count + 1, updated_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), mid),
                )
            else:
                self._conn.execute(
                    "UPDATE memories SET usage_count = usage_count + 1, updated_at=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), mid),
                )
            self._conn.commit()

    def auto_manage_memories(self, min_usage_for_eval: int = 3, low_success_threshold: float = 0.3,
                              high_success_threshold: float = 0.8, unused_days: int = 30) -> dict:
        """V2.9.1：记忆质量自动评估与主动遗忘。

        治理策略（借鉴文章"记忆是治理问题不是存储问题"）：
        1. 低成功率自动停用：usage_count >= min_usage 且 success_rate < low_success_threshold → status=inactive
        2. 高成功率自动提升：usage_count >= 5 且 success_rate >= high_success_threshold → confidence=0.9
        3. 长期未使用降级：created_at > unused_days 且 usage_count == 0 → confidence=0.3
        4. 极低置信度清理：confidence < 0.2 且 usage_count == 0 → （仅标记，不自动删除，需人工确认）

        Returns:
            治理结果统计：{deactivated, promoted, demoted, total_evaluated}
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=unused_days)).isoformat(timespec="seconds")

        with self._lock:
            rows = self._conn.execute("SELECT * FROM memories WHERE status='active'").fetchall()
            cols = [d[0] for d in self._conn.execute("SELECT * FROM memories LIMIT 1").description]

        deactivated = 0
        promoted = 0
        demoted = 0

        for r in rows:
            d = dict(zip(cols, r))
            mid = d["id"]
            usage = int(d.get("usage_count", 0))
            success = int(d.get("success_count", 0))
            confidence = float(d.get("confidence", 0.5))
            created_at = d.get("created_at", "")

            # 1. 低成功率自动停用
            if usage >= min_usage_for_eval:
                success_rate = success / usage if usage > 0 else 0
                if success_rate < low_success_threshold:
                    self.update_memory(mid, status="inactive")
                    deactivated += 1
                    continue

            # 2. 高成功率自动提升置信度
            if usage >= 5 and success / usage >= high_success_threshold and confidence < 0.9:
                self.update_memory(mid, confidence=0.9)
                promoted += 1
                continue

            # 3. 长期未使用降级
            if usage == 0 and created_at and created_at < cutoff and confidence > 0.3:
                self.update_memory(mid, confidence=0.3)
                demoted += 1

        return {
            "total_evaluated": len(rows),
            "deactivated": deactivated,
            "promoted": promoted,
            "demoted": demoted,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }

    def get_memory_quality_stats(self) -> dict:
        """获取记忆质量统计，用于前端展示治理效果。"""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            active = self._conn.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
            inactive = total - active
            avg_confidence = self._conn.execute("SELECT AVG(confidence) FROM memories WHERE status='active'").fetchone()[0] or 0
            total_usage = self._conn.execute("SELECT SUM(usage_count) FROM memories").fetchone()[0] or 0
            total_success = self._conn.execute("SELECT SUM(success_count) FROM memories").fetchone()[0] or 0
            avg_success_rate = (total_success / total_usage) if total_usage > 0 else 0
            # 低质量记忆（成功率<30%且使用>=3次）
            low_quality = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE usage_count >= 3 AND success_count * 1.0 / usage_count < 0.3"
            ).fetchone()[0]
            # 未使用记忆（创建超过7天但使用次数为0）
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
            unused = self._conn.execute(
                "SELECT COUNT(*) FROM memories WHERE usage_count = 0 AND created_at < ?", (cutoff,)
            ).fetchone()[0]

        return {
            "total": total,
            "active": active,
            "inactive": inactive,
            "avg_confidence": round(avg_confidence, 3),
            "total_usage": total_usage,
            "total_success": total_success,
            "avg_success_rate": round(avg_success_rate, 3),
            "low_quality": low_quality,
            "unused_over_7d": unused,
        }

    def rebuild(self, results_dir: Path) -> int:
        """扫描 results_dir/*/run.json 重建索引，返回已索引 run 数。

        注意：rebuild 时保留已有的 batch_id 关联，避免清空批次与 run 的关联关系。
        """
        n = 0
        for p in sorted(Path(results_dir).glob("*/run.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                # 保留已有的 batch_id 关联（rebuild 不应清空批次关联）
                existing_batch = ""
                with self._lock:
                    row = self._conn.execute(
                        "SELECT batch_id FROM runs WHERE run_id=?", (rec.get("run_id"),)
                    ).fetchone()
                    if row and row[0]:
                        existing_batch = row[0]
                self.insert_run(rec, batch_id=existing_batch)
                n += 1
            except Exception:  # noqa: BLE001 - 单条损坏不影响整体
                continue
        return n

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
