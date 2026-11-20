"""Application services: table replacement, weighting jobs, result access.

Concurrency model
-----------------
A single process is assumed (docker compose runs one uvicorn worker).
``self._lock`` serialises table replacement and job bookkeeping; the heavy
LP work happens on a worker thread using its own SQLite connection.

Every source table is an immutable versioned snapshot.  Replacement only
flips a pointer row after the new snapshot is fully written, so a failed
upload cannot disturb the live table.  Results are always tagged with the
exact version pair they were computed from: as soon as either table is
replaced, the old pair becomes stale and its weights/differences stop being
served (the API reports "待配权").
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from . import database, weighting
from .errors import (
    JobNotFoundError,
    JobNotUnbalancedError,
    JobRunningError,
    MergeFailedError,
    MissingTableError,
    MissingTargetsError,
    NoResultError,
    NotReadyError,
    PlanConflictError,
    PlanNotFoundError,
    PlanNotSolvedError,
    PlanVersionMismatchError,
    ResponseMismatchError,
    SupplementSolveError,
    ValidationError,
)

STATUS_LABELS = {
    "missing": "待配权（缺少数据表）",
    "pending": "待配权",
    "running": "配权计算中",
    "balanced": "已配平",
    "unbalanced": "无法配平",
    "failed": "计算失败",
}

PLAN_STATUS_LABELS = {
    "solved": "已求得补访方案",
    "no_solution": "无可行补访方案",
    "completed": "补访应答已并入正式名单",
    "failed": "补访计算失败",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_hash(records: list[dict]) -> str:
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WeightingService:
    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or database.DEFAULT_DB_PATH
        self._lock = threading.RLock()
        with database.db_session(self._db_path) as conn:
            database.init_db(conn)
        # One background thread at a time; a new replacement supersedes it.
        self._worker: threading.Thread | None = None
        self._worker_token: uuid.UUID | None = None

    # ------------------------------------------------------------------
    # Snapshot reads
    # ------------------------------------------------------------------
    def _current_version(self, conn: sqlite3.Connection, table: str) -> int | None:
        row = conn.execute(
            "SELECT version FROM current_versions WHERE table_name = ?",
            (table,),
        ).fetchone()
        return None if row is None else row["version"]

    def _load_respondents(self, conn: sqlite3.Connection, version: int) -> list[dict]:
        rows = conn.execute(
            "SELECT household_id, district, size_band, init_weight "
            "FROM respondent_records WHERE version = ? ORDER BY household_id",
            (version,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _load_targets(self, conn: sqlite3.Connection, version: int) -> list[dict]:
        rows = conn.execute(
            "SELECT dimension, group_name, target_count "
            "FROM target_records WHERE version = ? "
            "ORDER BY dimension, group_name",
            (version,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Validation (runs before anything is written)
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_respondents(records: list[dict]) -> None:
        if not isinstance(records, list) or not records:
            raise ValidationError("应答名单不能为空")
        seen: set[str] = set()
        for idx, rec in enumerate(records):
            try:
                hid = str(rec["household_id"]).strip()
                district = str(rec["district"]).strip()
                band = str(rec["size_band"]).strip()
                weight = float(rec["init_weight"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"第 {idx + 1} 行字段不完整或类型错误: {exc}",
                    {"index": idx},
                )
            if not hid or not district or not band:
                raise ValidationError(f"第 {idx + 1} 行存在空字段", {"index": idx})
            if weight != weight or weight in (float("inf"), float("-inf")):
                raise ValidationError(f"第 {idx + 1} 行初始权重不是有限数",
                                      {"index": idx})
            if weight <= 0:
                raise ValidationError(
                    f"第 {idx + 1} 行初始权重必须为正数", {"index": idx}
                )
            if hid in seen:
                raise ValidationError(
                    f"户号重复: {hid}", {"household_id": hid}
                )
            seen.add(hid)

    @staticmethod
    def _validate_targets(records: list[dict]) -> None:
        if not isinstance(records, list) or not records:
            raise ValidationError("总体目标表不能为空")
        seen: set[tuple[str, str]] = set()
        for idx, rec in enumerate(records):
            try:
                dimension = str(rec["dimension"]).strip()
                group = str(rec["group_name"]).strip()
                target = float(rec["target_count"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValidationError(
                    f"第 {idx + 1} 行字段不完整或类型错误: {exc}",
                    {"index": idx},
                )
            if dimension not in ("district", "size_band"):
                raise ValidationError(
                    f"第 {idx + 1} 行 dimension 必须为 district 或 size_band",
                    {"index": idx, "value": dimension},
                )
            if not group:
                raise ValidationError(f"第 {idx + 1} 行 group_name 为空",
                                      {"index": idx})
            if target != target or target in (float("inf"), float("-inf")):
                raise ValidationError(f"第 {idx + 1} 行目标户数不是有限数",
                                      {"index": idx})
            if target < 0:
                raise ValidationError(
                    f"第 {idx + 1} 行目标户数必须非负", {"index": idx}
                )
            key = (dimension, group)
            if key in seen:
                raise ValidationError(
                    f"分组目标重复: {dimension}/{group}",
                    {"dimension": dimension, "group_name": group},
                )
            seen.add(key)

    # ------------------------------------------------------------------
    # Whole-table replacement
    # ------------------------------------------------------------------
    def replace_respondents(self, records: list[dict]) -> dict:
        self._validate_respondents(records)
        normalized = [
            {
                "household_id": r["household_id"].strip(),
                "district": r["district"].strip(),
                "size_band": r["size_band"].strip(),
                "init_weight": float(r["init_weight"]),
            }
            for r in records
        ]
        digest = _content_hash(normalized)
        with self._lock, database.db_session(self._db_path) as conn:
            previous = self._current_version(conn, "respondents")
            version = self._insert_respondent_snapshot(conn, normalized, digest)
            conn.execute(
                "INSERT INTO current_versions(table_name, version) "
                "VALUES ('respondents', ?) "
                "ON CONFLICT(table_name) DO UPDATE SET version = excluded.version",
                (version,),
            )
            conn.commit()
        return {
            "table": "respondents",
            "version": version,
            "row_count": len(normalized),
            "content_hash": digest,
            "replaced_version": previous,
            "replaced_at": _utc_now(),
            "message": "应答名单换交成功，旧版权重与差额已停供，请重新发起配权",
        }

    def replace_targets(self, records: list[dict]) -> dict:
        self._validate_targets(records)
        normalized = [
            {
                "dimension": r["dimension"].strip(),
                "group_name": r["group_name"].strip(),
                "target_count": float(r["target_count"]),
            }
            for r in records
        ]
        digest = _content_hash(normalized)
        with self._lock, database.db_session(self._db_path) as conn:
            previous = self._current_version(conn, "targets")
            version = self._insert_target_snapshot(conn, normalized, digest)
            conn.execute(
                "INSERT INTO current_versions(table_name, version) "
                "VALUES ('targets', ?) "
                "ON CONFLICT(table_name) DO UPDATE SET version = excluded.version",
                (version,),
            )
            conn.commit()
        return {
            "table": "targets",
            "version": version,
            "row_count": len(normalized),
            "content_hash": digest,
            "replaced_version": previous,
            "replaced_at": _utc_now(),
            "message": "总体目标表换交成功，旧版权重与差额已停供，请重新发起配权",
        }

    def _insert_respondent_snapshot(
        self, conn: sqlite3.Connection, records: list[dict], digest: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO respondent_versions(created_at, row_count, content_hash) "
            "VALUES (?, ?, ?)",
            (_utc_now(), len(records), digest),
        )
        version = cur.lastrowid
        conn.executemany(
            "INSERT INTO respondent_records"
            "(version, household_id, district, size_band, init_weight) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    version,
                    r["household_id"],
                    r["district"],
                    r["size_band"],
                    r["init_weight"],
                )
                for r in records
            ],
        )
        return version

    def _insert_target_snapshot(
        self, conn: sqlite3.Connection, records: list[dict], digest: str
    ) -> int:
        cur = conn.execute(
            "INSERT INTO target_versions(created_at, row_count, content_hash) "
            "VALUES (?, ?, ?)",
            (_utc_now(), len(records), digest),
        )
        version = cur.lastrowid
        conn.executemany(
            "INSERT INTO target_records"
            "(version, dimension, group_name, target_count) "
            "VALUES (?, ?, ?, ?)",
            [
                (version, r["dimension"], r["group_name"], r["target_count"])
                for r in records
            ],
        )
        return version

    # ------------------------------------------------------------------
    # Status + job lifecycle
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            missing = []
            if r_ver is None:
                missing.append("respondents")
            if t_ver is None:
                missing.append("targets")
            if missing:
                return {
                    "status": "missing",
                    "status_label": STATUS_LABELS["missing"],
                    "respondent_version": None,
                    "target_version": None,
                    "missing_tables": missing,
                    "message": "两表尚未齐备，无法配权",
                }

            job = self._latest_job(conn, r_ver, t_ver)
            if job is None:
                return {
                    "status": "pending",
                    "status_label": STATUS_LABELS["pending"],
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                    "message": "当前两表版本尚无配权结果，请发起配权",
                }
            return self._status_from_job(job, r_ver, t_ver)

    def _status_from_job(
        self, job: sqlite3.Row, r_ver: int, t_ver: int
    ) -> dict:
        status = job["status"]
        body = {
            "status": status,
            "status_label": STATUS_LABELS[status],
            "respondent_version": r_ver,
            "target_version": t_ver,
            "job_id": job["id"],
        }
        if status == "running":
            body["message"] = "配权计算中，请稍后查询"
        elif status == "balanced":
            body["message"] = "已配平，可领取逐户权重"
            body["objective_gap"] = job["objective_gap"]
        elif status == "unbalanced":
            body["message"] = "无法配平，请领取各组差额排查缺样"
            body["objective_gap"] = job["objective_gap"]
        else:  # failed
            body["message"] = f"计算失败: {job['error_reason']}"
            body["error_reason"] = job["error_reason"]
        return body

    def _latest_job(
        self, conn: sqlite3.Connection, r_ver: int, t_ver: int
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM weighting_jobs "
            "WHERE respondent_version = ? AND target_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (r_ver, t_ver),
        ).fetchone()

    def start_weighting(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            if r_ver is None or t_ver is None:
                missing = [
                    name
                    for name, ver in (
                        ("respondents", r_ver),
                        ("targets", t_ver),
                    )
                    if ver is None
                ]
                raise MissingTableError(
                    "缺少必需数据表，无法发起配权",
                    {"missing_tables": missing},
                )

            job = self._latest_job(conn, r_ver, t_ver)
            if job is not None and job["status"] == "running":
                raise JobRunningError(
                    "当前两表版本正在配权计算中，请勿重复发起",
                    {"job_id": job["id"]},
                )

            # Coverage check: every group occurring in the roster must have
            # a target, otherwise weighted counts for that group cannot be
            # evaluated.
            missing_groups = self._missing_groups(conn, r_ver, t_ver)
            if missing_groups:
                raise MissingTargetsError(
                    "总体目标表缺少名单中出现的分组目标",
                    {"missing_group_targets": missing_groups},
                )

            cur = conn.execute(
                "INSERT INTO weighting_jobs"
                "(respondent_version, target_version, status, created_at) "
                "VALUES (?, ?, 'running', ?)",
                (r_ver, t_ver, _utc_now()),
            )
            job_id = cur.lastrowid
            conn.commit()

        token = uuid.uuid4()
        self._worker_token = token
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, r_ver, t_ver, token),
            name=f"weighting-job-{job_id}",
            daemon=True,
        )
        self._worker = thread
        thread.start()
        return {
            "job_id": job_id,
            "status": "running",
            "status_label": STATUS_LABELS["running"],
            "respondent_version": r_ver,
            "target_version": t_ver,
            "message": "配权作业已开始",
        }

    def _missing_groups(
        self,
        conn: sqlite3.Connection,
        r_ver: int,
        t_ver: int,
    ) -> list[dict]:
        targets = self._load_targets(conn, t_ver)
        covered = {(t["dimension"], t["group_name"]) for t in targets}
        missing: list[dict] = []
        for dimension, column in (("district", "district"),
                                  ("size_band", "size_band")):
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS g FROM respondent_records "
                "WHERE version = ? ORDER BY g",
                (r_ver,),
            ).fetchall()
            for row in rows:
                if (dimension, row["g"]) not in covered:
                    missing.append(
                        {"dimension": dimension, "group_name": row["g"]}
                    )
        return missing

    def _run_job(self, job_id: int, r_ver: int, t_ver: int, token: uuid.UUID) -> None:
        try:
            with database.db_session(self._db_path) as conn:
                # Immutable snapshots: safe to read without holding the lock.
                households = self._load_respondents(conn, r_ver)
                target_rows = self._load_targets(conn, t_ver)
                target_tuples = [
                    (t["dimension"], t["group_name"], t["target_count"])
                    for t in target_rows
                ]
            result = weighting.solve(households, target_tuples)

            with self._lock:
                # Superseded by a table replacement while we were computing.
                if self._worker_token != token:
                    return
                with database.db_session(self._db_path) as conn:
                    if not self._pair_is_current(conn, r_ver, t_ver):
                        return
                    if result.failure_reason is not None:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='failed', "
                            "error_reason=?, finished_at=? WHERE id=?",
                            (result.failure_reason, _utc_now(), job_id),
                        )
                    elif result.balanced:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='balanced', "
                            "is_balanced=1, objective_gap=?, "
                            "error_reason=NULL, finished_at=? WHERE id=?",
                            (result.gap, _utc_now(), job_id),
                        )
                        conn.executemany(
                            "INSERT INTO weight_results"
                            "(job_id, household_id, weight) VALUES (?, ?, ?)",
                            [
                                (job_id, hh["household_id"], w)
                                for hh, w in zip(households, result.weights)
                            ],
                        )
                        conn.executemany(
                            "INSERT INTO diff_results"
                            "(job_id, dimension, group_name, weighted, "
                            "target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    job_id,
                                    d["dimension"],
                                    d["group_name"],
                                    d["weighted_count"],
                                    d["target_count"],
                                    d["difference"],
                                )
                                for d in result.group_diffs
                            ],
                        )
                    else:
                        conn.execute(
                            "UPDATE weighting_jobs SET status='unbalanced', "
                            "is_balanced=0, objective_gap=?, "
                            "error_reason=NULL, finished_at=? WHERE id=?",
                            (result.gap, _utc_now(), job_id),
                        )
                        conn.executemany(
                            "INSERT INTO diff_results"
                            "(job_id, dimension, group_name, weighted, "
                            "target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    job_id,
                                    d["dimension"],
                                    d["group_name"],
                                    d["weighted_count"],
                                    d["target_count"],
                                    d["difference"],
                                )
                                for d in result.group_diffs
                            ],
                        )
                    conn.commit()
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                if self._worker_token != token:
                    return
                with database.db_session(self._db_path) as conn:
                    if not self._pair_is_current(conn, r_ver, t_ver):
                        return
                    conn.execute(
                        "UPDATE weighting_jobs SET status='failed', "
                        "error_reason=?, finished_at=? WHERE id=?",
                        (f"配权计算异常: {exc!r}", _utc_now(), job_id),
                    )
                    conn.commit()

    def _pair_is_current(
        self, conn: sqlite3.Connection, r_ver: int, t_ver: int
    ) -> bool:
        return (
            self._current_version(conn, "respondents") == r_ver
            and self._current_version(conn, "targets") == t_ver
        )

    # ------------------------------------------------------------------
    # Result access (always carries both table versions)
    # ------------------------------------------------------------------
    def _current_finished_job(self, conn: sqlite3.Connection):
        r_ver = self._current_version(conn, "respondents")
        t_ver = self._current_version(conn, "targets")
        if r_ver is None or t_ver is None:
            missing = [
                name
                for name, ver in (
                    ("respondents", r_ver),
                    ("targets", t_ver),
                )
                if ver is None
            ]
            raise MissingTableError(
                "缺少必需数据表", {"missing_tables": missing}
            )
        job = self._latest_job(conn, r_ver, t_ver)
        if job is None:
            raise NotReadyError(
                "当前两表版本尚未完成配权，结果待生成",
                {
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                },
            )
        if job["status"] == "running":
            raise NotReadyError(
                "配权计算中，请稍后再查询",
                {"job_id": job["id"]},
            )
        if job["status"] == "failed":
            raise NoResultError(
                f"配权计算失败: {job['error_reason']}",
                {
                    "job_id": job["id"],
                    "error_reason": job["error_reason"],
                    "respondent_version": r_ver,
                    "target_version": t_ver,
                },
            )
        return job, r_ver, t_ver

    def get_weights(self, limit: int | None = None, offset: int = 0) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            job, r_ver, t_ver = self._current_finished_job(conn)
            if job["status"] != "balanced":
                raise NoResultError(
                    "当前两表版本无法配平，逐户权重不可领取；"
                    "请调用差额接口查看各组加权户数与目标的差额",
                    {
                        "job_id": job["id"],
                        "objective_gap": job["objective_gap"],
                        "respondent_version": r_ver,
                        "target_version": t_ver,
                    },
                )
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM weight_results WHERE job_id = ?",
                (job["id"],),
            ).fetchone()["c"]
            sql = (
                "SELECT household_id, weight FROM weight_results "
                "WHERE job_id = ? ORDER BY household_id"
            )
            params: list = [job["id"]]
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                params.extend([int(limit), int(offset)])
            rows = conn.execute(sql, params).fetchall()
            return {
                "status": "balanced",
                "job_id": job["id"],
                "respondent_version": r_ver,
                "target_version": t_ver,
                "objective_gap": job["objective_gap"],
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "weights": [dict(r) for r in rows],
            }

    def get_differences(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            job, r_ver, t_ver = self._current_finished_job(conn)
            rows = conn.execute(
                "SELECT dimension, group_name, weighted AS weighted_count, "
                "target_count, difference FROM diff_results "
                "WHERE job_id = ? ORDER BY dimension, group_name",
                (job["id"],),
            ).fetchall()
            return {
                "status": job["status"],
                "job_id": job["id"],
                "respondent_version": r_ver,
                "target_version": t_ver,
                "objective_gap": job["objective_gap"],
                "is_balanced": bool(job["is_balanced"]),
                "differences": [dict(r) for r in rows],
                "message": (
                    "各组加权户数减目标户数的差额（difference = weighted - target）"
                    if job["status"] == "unbalanced"
                    else "当前版本已配平，差额仅作核验参考"
                ),
            }

    def get_versions(self) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            r_ver = self._current_version(conn, "respondents")
            t_ver = self._current_version(conn, "targets")
            r_meta = None
            t_meta = None
            if r_ver is not None:
                r_meta = dict(conn.execute(
                    "SELECT version, created_at, row_count, content_hash "
                    "FROM respondent_versions WHERE version = ?",
                    (r_ver,),
                ).fetchone())
            if t_ver is not None:
                t_meta = dict(conn.execute(
                    "SELECT version, created_at, row_count, content_hash "
                    "FROM target_versions WHERE version = ?",
                    (t_ver,),
                ).fetchone())
            return {
                "respondents": r_meta,
                "targets": t_meta,
            }

    # ------------------------------------------------------------------
    # Supplementary-visit plans
    # ------------------------------------------------------------------
    def _validate_candidates(self, candidates: list[dict]) -> list[dict]:
        """Same field rules as the roster (which also enforces mutually
        unique household ids within the payload)."""
        self._validate_respondents(candidates)
        return [
            {
                "household_id": c["household_id"].strip(),
                "district": c["district"].strip(),
                "size_band": c["size_band"].strip(),
                "init_weight": float(c["init_weight"]),
            }
            for c in candidates
        ]

    def _load_job(self, conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        job = conn.execute(
            "SELECT * FROM weighting_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise JobNotFoundError(
                "作业号不存在", {"job_id": job_id}
            )
        return job

    def create_supplement_plan(
        self, job_id: int, candidates_raw: list[dict]
    ) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            job = self._load_job(conn, job_id)
            r_ver, t_ver = job["respondent_version"], job["target_version"]
            if job["status"] != "unbalanced":
                raise JobNotUnbalancedError(
                    "仅无法配平的作业可以发起补访选户",
                    {
                        "job_id": job_id,
                        "job_status": job["status"],
                    },
                )
            if not self._pair_is_current(conn, r_ver, t_ver):
                raise PlanVersionMismatchError(
                    "作业对应的两表版本已不是当前版本，"
                    "请基于当前版本重新配权后再发起补访",
                    {
                        "job_id": job_id,
                        "job_respondent_version": r_ver,
                        "job_target_version": t_ver,
                        "current_respondent_version":
                            self._current_version(conn, "respondents"),
                        "current_target_version":
                            self._current_version(conn, "targets"),
                    },
                )
            candidates = self._validate_candidates(candidates_raw)

            households = self._load_respondents(conn, r_ver)
            sampled = {hh["household_id"] for hh in households}
            duplicates = sorted(
                {c["household_id"] for c in candidates} & sampled
            )
            if duplicates:
                raise ValidationError(
                    "候补户号必须尚未入样，以下户号已在应答名单中",
                    {"already_sampled": duplicates},
                )
            target_rows = self._load_targets(conn, t_ver)
            target_tuples = [
                (t["dimension"], t["group_name"], t["target_count"])
                for t in target_rows
            ]
            # Candidates must only use groups covered by the pinned targets.
            covered = {(t["dimension"], t["group_name"]) for t in target_rows}
            missing_groups: list[dict] = []
            for c in candidates:
                for dimension, key in (
                    ("district", c["district"]),
                    ("size_band", c["size_band"]),
                ):
                    item = {"dimension": dimension, "group_name": key}
                    if (dimension, key) not in covered and item not in missing_groups:
                        missing_groups.append(item)
            if missing_groups:
                raise MissingTargetsError(
                    "总体目标表缺少候补户所在分组的目标",
                    {"missing_group_targets": missing_groups},
                )

        # Heavy MIP work runs outside the database lock.
        result = weighting.select_minimum_supplement(
            households, candidates, target_tuples
        )

        with self._lock, database.db_session(self._db_path) as conn:
            # Tables may have been swapped while the MIP was running.
            if not self._pair_is_current(conn, r_ver, t_ver):
                raise PlanVersionMismatchError(
                    "两表已换版，该作业对应的源表版本不再是当前版本，"
                    "请基于当前版本重新配权后再发起补访",
                    {
                        "job_id": job_id,
                        "job_respondent_version": r_ver,
                        "job_target_version": t_ver,
                        "current_respondent_version":
                            self._current_version(conn, "respondents"),
                        "current_target_version":
                            self._current_version(conn, "targets"),
                    },
                )
            if result.failure_reason is not None:
                # Solver malfunction: nothing stored, live data untouched.
                raise SupplementSolveError(
                    result.failure_reason, {"job_id": job_id}
                )

            selected_set = set(result.selected)
            status = "solved" if result.feasible else "no_solution"
            reason = None
            if not result.feasible:
                reason = (
                    "全部候补户入样后仍无法同时配平区县与家庭人数档；"
                    "请参考各组差额增补对应分组的候补户"
                )
            cur = conn.execute(
                "INSERT INTO supplement_plans"
                "(job_id, respondent_version, target_version, status, "
                " candidate_count, selected_count, no_solution_reason, "
                " best_gap, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id, r_ver, t_ver, status, len(candidates),
                    len(selected_set) if result.feasible else None,
                    reason,
                    result.best_gap,
                    _utc_now(),
                ),
            )
            plan_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO supplement_plan_households"
                "(plan_id, household_id, district, size_band, "
                " init_weight, is_selected) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        plan_id,
                        c["household_id"],
                        c["district"],
                        c["size_band"],
                        c["init_weight"],
                        1 if c["household_id"] in selected_set else 0,
                    )
                    for c in candidates
                ],
            )
            if not result.feasible:
                conn.executemany(
                    "INSERT INTO supplement_plan_diagnostics"
                    "(plan_id, dimension, group_name, weighted, "
                    " target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            plan_id,
                            d["dimension"],
                            d["group_name"],
                            d["weighted_count"],
                            d["target_count"],
                            d["difference"],
                        )
                        for d in result.group_diffs
                    ],
                )
            conn.commit()

        body = {
            "plan_id": plan_id,
            "job_id": job_id,
            "status": status,
            "status_label": PLAN_STATUS_LABELS[status],
            "respondent_version": r_ver,
            "target_version": t_ver,
            "candidate_count": len(candidates),
        }
        if result.feasible:
            body["selected_count"] = len(selected_set)
            body["selected_household_ids"] = result.selected
            body["message"] = (
                f"最少需补访 {len(selected_set)} 户即可同时配平两个维度"
            )
        else:
            body["selected_count"] = None
            body["selected_household_ids"] = []
            body["no_solution_reason"] = reason
            body["best_gap"] = result.best_gap
            body["blocking_groups"] = result.blocking_groups
            body["diagnostics"] = result.group_diffs
            body["message"] = reason
        return body

    def _load_plan(self, conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        plan = conn.execute(
            "SELECT * FROM supplement_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise PlanNotFoundError(
                "补访方案号不存在", {"plan_id": plan_id}
            )
        return plan

    def complete_supplement_plan(
        self, plan_id: int, responded_ids: list[str]
    ) -> dict:
        responded = [hid.strip() for hid in responded_ids if hid.strip()]
        with self._lock, database.db_session(self._db_path) as conn:
            plan = self._load_plan(conn, plan_id)
            if plan["status"] != "solved":
                raise PlanNotSolvedError(
                    "仅状态为「已求得补访方案」的方案可以回交应答户",
                    {"plan_id": plan_id, "plan_status": plan["status"]},
                )
            r_ver = plan["respondent_version"]
            t_ver = plan["target_version"]

            selected_rows = conn.execute(
                "SELECT household_id, district, size_band, init_weight "
                "FROM supplement_plan_households WHERE plan_id = ? "
                "AND is_selected = 1 ORDER BY household_id",
                (plan_id,),
            ).fetchall()
            selected = [dict(r) for r in selected_rows]
            selected_ids = [r["household_id"] for r in selected]
            selected_set = set(selected_ids)
            responded_set = set(responded)

            if responded_set != selected_set or len(responded_set) != len(responded):
                raise ResponseMismatchError(
                    "应答户须与入选户完全一致（同集合、无重复、无缺漏）",
                    {
                        "plan_id": plan_id,
                        "selected_household_ids": selected_ids,
                        "responded_household_ids": responded,
                        "missing": sorted(selected_set - responded_set),
                        "unexpected": sorted(responded_set - selected_set),
                    },
                )

            # The pinned source tables must still be the live versions.
            if not self._pair_is_current(conn, r_ver, t_ver):
                raise PlanVersionMismatchError(
                    "两表已换版，方案绑定的源表版本不再是当前版本，"
                    "正式名单与供数保持不变",
                    {
                        "plan_id": plan_id,
                        "plan_respondent_version": r_ver,
                        "plan_target_version": t_ver,
                        "current_respondent_version":
                            self._current_version(conn, "respondents"),
                        "current_target_version":
                            self._current_version(conn, "targets"),
                    },
                )

            households = self._load_respondents(conn, r_ver)
            target_rows = self._load_targets(conn, t_ver)
            target_tuples = [
                (t["dimension"], t["group_name"], t["target_count"])
                for t in target_rows
            ]
            merged = households + selected

        # Recompute official weights for the merged roster under the original
        # two-stage rule; done outside the lock.
        try:
            result = weighting.solve(merged, target_tuples)
        except Exception as exc:
            raise MergeFailedError(
                f"并入后按原规则重算异常: {exc!r}",
                {"plan_id": plan_id},
            )

        with self._lock, database.db_session(self._db_path) as conn:
            # Re-check every precondition inside the write transaction. Any
            # failure rolls back, leaving the live roster and supply intact.
            try:
                if result.failure_reason is not None:
                    raise MergeFailedError(
                        f"并入后按原规则重算失败: {result.failure_reason}",
                        {"plan_id": plan_id},
                    )
                if not result.balanced:
                    raise MergeFailedError(
                        "并入候补户后按原规则仍无法配平，已保留原正式名单与供数",
                        {"plan_id": plan_id, "objective_gap": result.gap},
                    )
                plan_now = conn.execute(
                    "SELECT status FROM supplement_plans WHERE id = ?",
                    (plan_id,),
                ).fetchone()
                if plan_now is None or plan_now["status"] != "solved":
                    raise PlanConflictError(
                        "方案状态已变化，无法并入",
                        {"plan_id": plan_id},
                    )
                if not self._pair_is_current(conn, r_ver, t_ver):
                    raise PlanVersionMismatchError(
                        "两表已换版，正式名单与供数保持不变",
                        {"plan_id": plan_id},
                    )

                digest = _content_hash(merged)
                new_r_ver = self._insert_respondent_snapshot(
                    conn, merged, digest
                )
                cur = conn.execute(
                    "INSERT INTO weighting_jobs"
                    "(respondent_version, target_version, status, "
                    " is_balanced, objective_gap, created_at, finished_at) "
                    "VALUES (?, ?, 'balanced', 1, ?, ?, ?)",
                    (new_r_ver, t_ver, result.gap, _utc_now(), _utc_now()),
                )
                new_job_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO weight_results"
                    "(job_id, household_id, weight) VALUES (?, ?, ?)",
                    [
                        (new_job_id, hh["household_id"], w)
                        for hh, w in zip(merged, result.weights)
                    ],
                )
                conn.executemany(
                    "INSERT INTO diff_results"
                    "(job_id, dimension, group_name, weighted, "
                    " target_count, difference) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            new_job_id,
                            d["dimension"],
                            d["group_name"],
                            d["weighted_count"],
                            d["target_count"],
                            d["difference"],
                        )
                        for d in result.group_diffs
                    ],
                )
                conn.execute(
                    "INSERT INTO current_versions(table_name, version) "
                    "VALUES ('respondents', ?) "
                    "ON CONFLICT(table_name) DO UPDATE SET "
                    "version = excluded.version",
                    (new_r_ver,),
                )
                conn.execute(
                    "UPDATE supplement_plans SET status='completed', "
                    "merged_respondent_version=?, merged_job_id=?, "
                    "completed_at=? WHERE id=?",
                    (new_r_ver, new_job_id, _utc_now(), plan_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "plan_id": plan_id,
            "status": "completed",
            "status_label": PLAN_STATUS_LABELS["completed"],
            "respondent_version": new_r_ver,
            "target_version": t_ver,
            "job_id": new_job_id,
            "merged_household_count": len(merged),
            "added_household_ids": selected_ids,
            "objective_gap": result.gap,
            "message": (
                "补访应答户已并入正式名单，新名单与按原规则计算的正式权重"
                "同时生效"
            ),
        }

    def get_supplement_plan(self, plan_id: int) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            plan = self._load_plan(conn, plan_id)
            rows = conn.execute(
                "SELECT household_id, district, size_band, init_weight, "
                "is_selected FROM supplement_plan_households "
                "WHERE plan_id = ? ORDER BY household_id",
                (plan_id,),
            ).fetchall()
            body = {
                "plan_id": plan["id"],
                "job_id": plan["job_id"],
                "status": plan["status"],
                "status_label": PLAN_STATUS_LABELS[plan["status"]],
                "respondent_version": plan["respondent_version"],
                "target_version": plan["target_version"],
                "candidate_count": plan["candidate_count"],
                "selected_count": plan["selected_count"],
                "selected_household_ids": [
                    r["household_id"] for r in rows if r["is_selected"]
                ],
                "candidates": [dict(r) for r in rows],
                "created_at": plan["created_at"],
                "completed_at": plan["completed_at"],
                "merged_respondent_version":
                    plan["merged_respondent_version"],
                "merged_job_id": plan["merged_job_id"],
            }
            if plan["status"] == "no_solution":
                body["no_solution_reason"] = plan["no_solution_reason"]
                body["best_gap"] = plan["best_gap"]
                diag = conn.execute(
                    "SELECT dimension, group_name, weighted AS "
                    "weighted_count, target_count, difference "
                    "FROM supplement_plan_diagnostics WHERE plan_id = ? "
                    "ORDER BY dimension, group_name",
                    (plan_id,),
                ).fetchall()
                body["diagnostics"] = [dict(r) for r in diag]
            return body

    def list_supplement_plans(self, limit: int = 50, offset: int = 0) -> dict:
        with self._lock, database.db_session(self._db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM supplement_plans"
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT id AS plan_id, job_id, status, respondent_version, "
                "target_version, candidate_count, selected_count, "
                "created_at, completed_at FROM supplement_plans "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
            plans = [dict(r) for r in rows]
            for plan in plans:
                plan["status_label"] = PLAN_STATUS_LABELS[plan["status"]]
            return {
                "total_count": total,
                "limit": limit,
                "offset": offset,
                "plans": plans,
            }
