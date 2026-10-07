"""校准覆盖时间线查询：按设备编号 + 带时区的左闭右开时间窗返回连续区段。

语义：
- 结果按时间升序用连续区段**完整覆盖**查询窗 [window_from, window_to)，
  区段标为有效证书（valid）、已撤销证书（revoked）或无证书（uncovered）；
- 证书与查询窗取交集；端点相接（前张 valid_to == 后张 valid_from）不产生空隙，
  相邻证书即使状态相同也作为不同区段返回；
- 证书区段给出证书编号及"该区段内当前归属该证书"的测量数：
  每条测量只按其**最新更正版本**（无更正取原始登记）计数，
  最新版本时刻落在区段内且最新版本所用证书为该证书时计入，
  单条写入与批次写入均计入；uncovered 区段计数恒为 0；
- 设备不存在 / 设备在窗内无任何证书时，返回一个覆盖整窗的 uncovered 区段；
- 这是**实时**查询：区段、最新版本与撤销状态取自单事务 REPEATABLE READ
  的同一一致视图；查询本身只读、不物化，后续更正或撤销只改变再次查询的结果，
  已生成的审计快照（物化冻结）不受影响。
"""

from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, Query

from ..db import get_pool
from ..errors import ApiError
from ..schemas import CoverageSegment, CoverageTimelineOut

router = APIRouter()

# 查询窗与设备证书有效期相交的全部证书（同设备有效期互不重叠，exclusion 约束保证）。
_CERTS_SQL = """
SELECT id, certificate_no, valid_from, valid_to, revoked_at
FROM certificates
WHERE device_id = $1
  AND valid_from < $3   -- 证书起点 < 窗终点
  AND valid_to   > $2   -- 证书终点 > 窗起点（端点相接不算相交）
ORDER BY valid_from, id
"""

# 每个测量的最新版本：有更正取版本号最大的一行，否则取原始登记；
# 只保留最新版本时刻落在查询窗内的测量（旧版本不计数，被更正到窗外的不计数）。
# 与上面的证书读取在同一 REPEATABLE READ 事务快照下执行，
# 并发更正/撤销只能整体位于本次查询之前或之后。
_LATEST_MEASUREMENTS_SQL = """
SELECT l.measurement_id, l.measured_at, l.certificate_id
FROM (
    SELECT m.id AS measurement_id,
           COALESCE(mc.measured_at, m.measured_at) AS measured_at,
           COALESCE(mc.certificate_id, m.certificate_id) AS certificate_id
    FROM measurements m
    LEFT JOIN LATERAL (
        SELECT measured_at, certificate_id
        FROM measurement_corrections
        WHERE measurement_id = m.id
        ORDER BY version DESC
        LIMIT 1
    ) mc ON TRUE
    WHERE m.device_id = $1
) l
WHERE l.measured_at >= $2 AND l.measured_at < $3
"""


@router.get("", response_model=CoverageTimelineOut)
async def get_calibration_coverage(
    device_id: str = Query(
        min_length=1,
        max_length=128,
        description="设备编号",
    ),
    window_from: datetime = Query(
        alias="from",
        description="查询窗起点（含，左闭右开；必须带时区，如 2026-01-01T00:00:00+08:00）",
    ),
    window_to: datetime = Query(
        alias="to",
        description="查询窗终点（不含；必须带时区，且必须晚于起点）",
    ),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CoverageTimelineOut:
    """查询设备校准覆盖时间线。

    - 时间窗左闭右开 `[from, to)`，起点必须早于终点，时刻须带时区；
      缺时区返回 422（请求校验错误），`from >= to` 返回 422 `INVALID_TIME_WINDOW`；
    - 证书区段只取证书有效期与查询窗的交集；端点相接不产生空隙；
      相邻证书即使状态相同也分开；无证书部分以 uncovered 区段补齐；
    - 测量数按每条测量的最新更正版本计算（单条与批次写入均计入）；
    - 区段、最新版本与撤销状态取自同一一致视图；本接口实时只读、不冻结，
      后续更正/撤销只影响再次查询，已有审计快照仍保持冻结。
    """
    # AwareDatetime 由 Query 解析保证时区；跨字段关系在此显式校验并说明原因。
    if window_from.tzinfo is None or window_to.tzinfo is None:
        raise ApiError(
            422,
            "MISSING_TIMEZONE",
            "both 'from' and 'to' must include a timezone offset, e.g. +08:00",
        )
    if not window_from < window_to:
        raise ApiError(
            422,
            "INVALID_TIME_WINDOW",
            f"'from' must be earlier than 'to' (half-open [from, to)): got "
            f"from={window_from.isoformat()}, to={window_to.isoformat()}",
        )

    async with pool.acquire() as conn:
        # 只读一致视图：证书区段与最新版本测量共享同一事务快照
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            certs = await conn.fetch(_CERTS_SQL, device_id, window_from, window_to)
            latest = await conn.fetch(
                _LATEST_MEASUREMENTS_SQL, device_id, window_from, window_to
            )

    # 每个证书区段内当前归属该证书的测量数：按证书 id 分桶
    counts_by_cert: dict[int, int] = {}
    for r in latest:
        counts_by_cert[r["certificate_id"]] = (
            counts_by_cert.get(r["certificate_id"], 0) + 1
        )

    # 以证书与查询窗的交集为骨架，按时间升序拼接；空隙（含首尾）补 uncovered。
    # 同设备证书有效期互不重叠，相邻证书即使状态相同也不合并。
    segments: list[CoverageSegment] = []
    cursor = window_from
    for cert in certs:
        seg_from = max(cert["valid_from"], window_from)
        seg_to = min(cert["valid_to"], window_to)
        if seg_from > cursor:
            segments.append(
                CoverageSegment(
                    segment_from=cursor,
                    segment_to=seg_from,
                    segment_type="uncovered",
                    certificate_no=None,
                    measurement_count=0,
                )
            )
        segments.append(
            CoverageSegment(
                segment_from=seg_from,
                segment_to=seg_to,
                segment_type="revoked" if cert["revoked_at"] is not None else "valid",
                certificate_no=cert["certificate_no"],
                measurement_count=counts_by_cert.get(cert["id"], 0),
            )
        )
        cursor = seg_to
    if cursor < window_to:
        segments.append(
            CoverageSegment(
                segment_from=cursor,
                segment_to=window_to,
                segment_type="uncovered",
                certificate_no=None,
                measurement_count=0,
            )
        )
    # 窗内无任何证书（含设备从未登记）：一个覆盖整窗的无证书区段，计数为零
    if not segments:
        segments.append(
            CoverageSegment(
                segment_from=window_from,
                segment_to=window_to,
                segment_type="uncovered",
                certificate_no=None,
                measurement_count=0,
            )
        )

    return CoverageTimelineOut(
        device_id=device_id,
        window_from=window_from,
        window_to=window_to,
        segment_count=len(segments),
        segments=segments,
    )
