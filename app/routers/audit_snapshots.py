"""合规审计快照：按设备 + 左闭右开时间窗生成不可变审计快照，并凭快照标识稳定分页。

一致性保证：
- 生成在单事务内以 REPEATABLE READ 隔离级别运行；"最新版本测量时刻"的计算
  （原始登记 vs 最新更正）与证书撤销状态取自同一数据库快照，
  并发的更正/撤销/新写入只能整体位于该快照之前或之后；
- 明细与计数在生成时物化冻结，此后更正、撤销或新写入都不会改变
  已生成快照的明细与总数（快照表与明细表均为只追加，触发器禁止改删）。
"""

import uuid

import asyncpg
from fastapi import APIRouter, Depends, Query, Response

from ..db import get_pool
from ..errors import ApiError
from ..schemas import (
    AuditSnapshotCreate,
    AuditSnapshotDiffItem,
    AuditSnapshotDiffPage,
    AuditSnapshotItemOut,
    AuditSnapshotOut,
    AuditSnapshotPage,
    SnapshotValidity,
)

router = APIRouter()

# 每个测量的最新版本：有更正取版本号最大的一行，否则取原始登记；
# LEFT JOIN LATERAL 在 REPEATABLE READ 下与下面的证书状态读取共享同一快照
_LATEST_SQL = """
SELECT l.measurement_id, l.version, l.measured_at,
       c.id AS certificate_id, c.certificate_no, c.revoked_at, c.revoke_reason
FROM (
    SELECT m.id AS measurement_id,
           COALESCE(mc.version, 1) AS version,
           COALESCE(mc.measured_at, m.measured_at) AS measured_at,
           COALESCE(mc.certificate_id, m.certificate_id) AS certificate_id
    FROM measurements m
    LEFT JOIN LATERAL (
        SELECT version, measured_at, certificate_id
        FROM measurement_corrections
        WHERE measurement_id = m.id
        ORDER BY version DESC
        LIMIT 1
    ) mc ON TRUE
    WHERE m.device_id = $1
) l
JOIN certificates c ON c.id = l.certificate_id
WHERE l.measured_at >= $2 AND l.measured_at < $3
ORDER BY l.measurement_id
"""


def _frozen_validity(revoked_at, revoke_reason) -> SnapshotValidity:
    """按生成快照那一刻的证书撤销状态冻结有效性与原因（措辞与实时查询一致）。"""
    if revoked_at is not None:
        reason = f"certificate revoked at {revoked_at.isoformat()}"
        if revoke_reason:
            reason += f": {revoke_reason}"
        return SnapshotValidity(is_valid=False, reason=reason)
    return SnapshotValidity(
        is_valid=True,
        reason="certificate is active and covers the measurement time",
    )


def _summary(row) -> AuditSnapshotOut:
    return AuditSnapshotOut(
        snapshot_id=row["token"],
        device_id=row["device_id"],
        window_from=row["window_from"],
        window_to=row["window_to"],
        total_count=row["total_count"],
        valid_count=row["valid_count"],
        invalid_count=row["invalid_count"],
        created_at=row["created_at"],
    )


@router.post("", response_model=AuditSnapshotOut, status_code=201)
async def create_audit_snapshot(
    payload: AuditSnapshotCreate,
    response: Response,
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotOut:
    """生成合规审计快照。

    - 时间窗左闭右开 `[window_from, window_to)`，起点必须早于终点，时刻须带时区；
      非法请求返回 422 且不产生任何快照；
    - 纳入单条写入与批次写入的全部测量，但只选**最新版本测量时刻**落在窗内者；
    - 版本链与证书撤销状态取自 REPEATABLE READ 同一快照视图，
      并发更正/撤销只能整体位于快照前或后；
    - 明细与有效/无效总数在生成时冻结，空窗计数为 0。
    """
    async with pool.acquire() as conn:
        # 极小概率的 token 撞键：换新 token 整体重跑一次
        for attempt in range(2):
            token = uuid.uuid4().hex
            async with conn.transaction(isolation="repeatable_read"):
                rows = await conn.fetch(
                    _LATEST_SQL,
                    payload.device_id,
                    payload.window_from,
                    payload.window_to,
                )
                total = len(rows)
                valid = sum(1 for r in rows if r["revoked_at"] is None)
                invalid = total - valid
                try:
                    snap = await conn.fetchrow(
                        "INSERT INTO audit_snapshots "
                        "(token, device_id, window_from, window_to, "
                        " total_count, valid_count, invalid_count) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                        token,
                        payload.device_id,
                        payload.window_from,
                        payload.window_to,
                        total,
                        valid,
                        invalid,
                    )
                except asyncpg.UniqueViolationError:
                    if attempt == 0:
                        continue
                    raise
                if rows:
                    await conn.execute(
                        """
                        INSERT INTO audit_snapshot_items
                            (snapshot_id, measurement_id, version, measured_at,
                             certificate_no, is_valid, reason)
                        SELECT $1, t.measurement_id, t.version, t.measured_at,
                               t.certificate_no, t.is_valid, t.reason
                        FROM unnest(
                            $2::bigint[], $3::int[], $4::timestamptz[],
                            $5::text[], $6::bool[], $7::text[]
                        ) AS t(measurement_id, version, measured_at,
                               certificate_no, is_valid, reason)
                        """,
                        snap["id"],
                        [r["measurement_id"] for r in rows],
                        [r["version"] for r in rows],
                        [r["measured_at"] for r in rows],
                        [r["certificate_no"] for r in rows],
                        [r["revoked_at"] is None for r in rows],
                        [
                            _frozen_validity(r["revoked_at"], r["revoke_reason"]).reason
                            for r in rows
                        ],
                    )
            break

    response.headers["Location"] = f"/api/v1/audit-snapshots/{snap['token']}"
    return _summary(snap)


# 两份冻结明细按 measurement_id 全外连接并分类：
#   entered = 仅目标存在；left = 仅基准存在；
#   changed = 两边都有但版本/测量时刻/证书编号/有效性/原因任一不同；
#   完全相同的测量（全部字段相等）不输出。
# 时刻字段为 TIMESTAMPTZ，比较按绝对时刻进行，时区写法差异不影响结果。
# b / t 两个 CTE 仅由内部快照主键参数化（token 已先解析）。
_DIFF_SQL_TEMPLATE = """
WITH b AS (
    SELECT measurement_id, version, measured_at, certificate_no, is_valid, reason
    FROM audit_snapshot_items WHERE snapshot_id = {baseline_id}
),
t AS (
    SELECT measurement_id, version, measured_at, certificate_no, is_valid, reason
    FROM audit_snapshot_items WHERE snapshot_id = {target_id}
)
SELECT
    COALESCE(b.measurement_id, t.measurement_id) AS measurement_id,
    CASE
        WHEN b.measurement_id IS NULL THEN 'entered'
        WHEN t.measurement_id IS NULL THEN 'left'
        WHEN b.version IS DISTINCT FROM t.version
          OR b.measured_at IS DISTINCT FROM t.measured_at
          OR b.certificate_no IS DISTINCT FROM t.certificate_no
          OR b.is_valid IS DISTINCT FROM t.is_valid
          OR b.reason IS DISTINCT FROM t.reason THEN 'changed'
    END AS diff_type,
    b.measurement_id AS b_id, b.version AS b_version, b.measured_at AS b_measured_at,
    b.certificate_no AS b_certificate_no, b.is_valid AS b_is_valid, b.reason AS b_reason,
    t.measurement_id AS t_id, t.version AS t_version, t.measured_at AS t_measured_at,
    t.certificate_no AS t_certificate_no, t.is_valid AS t_is_valid, t.reason AS t_reason
FROM b
FULL OUTER JOIN t ON b.measurement_id = t.measurement_id
WHERE b.measurement_id IS NULL
   OR t.measurement_id IS NULL
   OR b.version IS DISTINCT FROM t.version
   OR b.measured_at IS DISTINCT FROM t.measured_at
   OR b.certificate_no IS DISTINCT FROM t.certificate_no
   OR b.is_valid IS DISTINCT FROM t.is_valid
   OR b.reason IS DISTINCT FROM t.reason
"""


def _item_out(prefix: str, r) -> AuditSnapshotItemOut:
    """把 b_* / t_* 前缀的一侧字段还原为冻结明细。"""
    return AuditSnapshotItemOut(
        measurement_id=r[f"{prefix}_id"],
        version=r[f"{prefix}_version"],
        measured_at=r[f"{prefix}_measured_at"],
        certificate_no=r[f"{prefix}_certificate_no"],
        validity=SnapshotValidity(
            is_valid=r[f"{prefix}_is_valid"], reason=r[f"{prefix}_reason"]
        ),
    )


# 注意：本路由必须注册在 "/{snapshot_id}" 之前，否则 "compare" 会被当成快照标识。
@router.get("/compare", response_model=AuditSnapshotDiffPage)
async def compare_audit_snapshots(
    baseline_snapshot_id: str = Query(
        min_length=1,
        description="基准快照标识（较早一次复核生成的冻结快照）",
    ),
    target_snapshot_id: str = Query(
        min_length=1,
        description="目标快照标识（较晚一次复核生成的冻结快照）",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotDiffPage:
    """比较两份冻结审计快照，辨明进入、离开与发生变化的测量。

    - 只比较**同一设备**且时间窗**按绝对时刻完全相同**（TIMESTAMPTZ 比较，
      时区写法可不同）的两份快照；不符时拒绝（422）并说明原因；
    - 任一快照标识未知返回 404 `SNAPSHOT_NOT_FOUND`；
    - 按测量编号对照两份冻结明细：仅目标存在为 entered，仅基准存在为 left，
      两边都有但版本/测量时刻/证书编号/有效性/原因任一不同为 changed，
      完全相同的测量不输出；
    - 三类差异合并后按 `measurement_id` 升序稳定分页（`limit`/`offset`）；
    - 比较只读取生成时物化的明细，此后的更正、撤销或新写入不会改变
      同一对快照的比较结果。
    """
    async with pool.acquire() as conn:
        baseline = await conn.fetchrow(
            "SELECT * FROM audit_snapshots WHERE token = $1", baseline_snapshot_id
        )
        if baseline is None:
            raise ApiError(
                404,
                "SNAPSHOT_NOT_FOUND",
                f"baseline audit snapshot '{baseline_snapshot_id}' not found",
            )
        target = await conn.fetchrow(
            "SELECT * FROM audit_snapshots WHERE token = $1", target_snapshot_id
        )
        if target is None:
            raise ApiError(
                404,
                "SNAPSHOT_NOT_FOUND",
                f"target audit snapshot '{target_snapshot_id}' not found",
            )

        if baseline["device_id"] != target["device_id"]:
            raise ApiError(
                422,
                "SNAPSHOT_DEVICE_MISMATCH",
                "snapshots must belong to the same device: baseline device "
                f"'{baseline['device_id']}' vs target device "
                f"'{target['device_id']}'",
            )
        # TIMESTAMPTZ 相等即绝对时刻相同，'2026-01-01T00:00:00+08:00' 与
        # '2025-12-31T16:00:00Z' 视为同一时间窗。
        if (
            baseline["window_from"] != target["window_from"]
            or baseline["window_to"] != target["window_to"]
        ):
            raise ApiError(
                422,
                "SNAPSHOT_WINDOW_MISMATCH",
                "snapshots must share the exact same absolute time window: "
                f"baseline [{baseline['window_from'].isoformat()}, "
                f"{baseline['window_to'].isoformat()}) vs target "
                f"[{target['window_from'].isoformat()}, "
                f"{target['window_to'].isoformat()})",
            )

        diff_sql = _DIFF_SQL_TEMPLATE.format(
            baseline_id=int(baseline["id"]), target_id=int(target["id"])
        )
        counts = await conn.fetch(
            f"SELECT diff_type, count(*) AS n FROM ({diff_sql}) d GROUP BY diff_type"
        )
        by_type = {r["diff_type"]: r["n"] for r in counts}
        entered = by_type.get("entered", 0)
        left = by_type.get("left", 0)
        changed = by_type.get("changed", 0)

        rows = await conn.fetch(
            f"SELECT * FROM ({diff_sql}) d "
            "ORDER BY d.measurement_id LIMIT $1 OFFSET $2",
            limit,
            offset,
        )

    items = [
        AuditSnapshotDiffItem(
            measurement_id=r["measurement_id"],
            diff_type=r["diff_type"],
            baseline=_item_out("b", r) if r["b_id"] is not None else None,
            target=_item_out("t", r) if r["t_id"] is not None else None,
        )
        for r in rows
    ]
    return AuditSnapshotDiffPage(
        baseline_snapshot_id=baseline["token"],
        target_snapshot_id=target["token"],
        device_id=baseline["device_id"],
        window_from=baseline["window_from"],
        window_to=baseline["window_to"],
        entered_count=entered,
        left_count=left,
        changed_count=changed,
        diff_count=entered + left + changed,
        limit=limit,
        offset=offset,
        items=items,
    )


async def _get_snapshot(conn, token: str):
    snap = await conn.fetchrow("SELECT * FROM audit_snapshots WHERE token = $1", token)
    if snap is None:
        raise ApiError(
            404, "SNAPSHOT_NOT_FOUND", f"audit snapshot '{token}' not found"
        )
    return snap


@router.get("/{snapshot_id}", response_model=AuditSnapshotOut)
async def get_audit_snapshot(
    snapshot_id: str,
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotOut:
    """快照摘要（标识、时间窗、有效/无效总数）。"""
    snap = await _get_snapshot(pool, snapshot_id)
    return _summary(snap)


@router.get("/{snapshot_id}/items", response_model=AuditSnapshotPage)
async def list_audit_snapshot_items(
    snapshot_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    pool: asyncpg.Pool = Depends(get_pool),
) -> AuditSnapshotPage:
    """按测量编号升序稳定分页读取快照明细。

    页内字段固定为测量编号、版本、时刻、证书编号、有效性及原因；
    内容在快照生成时已冻结，分页期间发生的更正/撤销/新写入不影响本结果。
    """
    snap = await _get_snapshot(pool, snapshot_id)
    rows = await pool.fetch(
        "SELECT measurement_id, version, measured_at, certificate_no, is_valid, reason "
        "FROM audit_snapshot_items WHERE snapshot_id = $1 "
        "ORDER BY measurement_id LIMIT $2 OFFSET $3",
        snap["id"],
        limit,
        offset,
    )
    items = [
        AuditSnapshotItemOut(
            measurement_id=r["measurement_id"],
            version=r["version"],
            measured_at=r["measured_at"],
            certificate_no=r["certificate_no"],
            validity=SnapshotValidity(is_valid=r["is_valid"], reason=r["reason"]),
        )
        for r in rows
    ]
    return AuditSnapshotPage(
        snapshot_id=snap["token"],
        device_id=snap["device_id"],
        window_from=snap["window_from"],
        window_to=snap["window_to"],
        total_count=snap["total_count"],
        valid_count=snap["valid_count"],
        invalid_count=snap["invalid_count"],
        limit=limit,
        offset=offset,
        items=items,
    )
