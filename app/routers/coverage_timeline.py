"""校准覆盖时间线：按设备 + 带时区的左闭右开时间窗实时查询证书覆盖区段。

一致性保证：
- 证书区段（有效期与撤销状态）与"每条测量最新更正版本"的归属统计在同一
  REPEATABLE READ 事务内读取，二者取自同一数据库快照；并发的更正、撤销或
  新登记只能整体位于该快照之前或之后，不会出现撕裂视图；
- 本接口只读、不物化：后续更正或撤销只改变再次查询的结果；
  已生成的合规审计快照仍保持冻结，不受本接口影响。
"""

from datetime import datetime, timezone

import asyncpg
from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from ..db import get_pool
from ..errors import ApiError
from ..schemas import CoverageSegment, CoverageTimelineOut

router = APIRouter()

# 与查询窗重叠的全部证书。同设备证书有效期由 exclusion 约束保证互不重叠，
# 按 valid_from 升序即时间升序，区段之间只可能隔着"无证书"空隙
_CERTS_SQL = """
SELECT id, certificate_no, valid_from, valid_to, revoked_at
FROM certificates
WHERE device_id = $1 AND valid_from < $3 AND valid_to > $2
ORDER BY valid_from
"""

# 按证书统计窗内测量数：每条测量只取最新更正版本（无更正时为原始登记 version 1），
# 单条写入与批次写入同在 measurements 表，均计入。
# 写入/更正时强制 measured_at 落在所选证书有效期内，故"最新版本属于证书 C 且
# 时刻在窗内"的测量必然落在 C 与窗的交集区段内，按证书分组即按区段计数。
_COUNTS_SQL = """
SELECT l.certificate_id, count(*) AS n
FROM (
    SELECT m.id,
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
GROUP BY l.certificate_id
"""


def _none_segment(segment_from: datetime, segment_to: datetime) -> CoverageSegment:
    return CoverageSegment(
        segment_from=segment_from,
        segment_to=segment_to,
        status="none",
        certificate_no=None,
        certificate_id=None,
        measurement_count=0,
    )


@router.get("", response_model=CoverageTimelineOut)
async def get_coverage_timeline(
    device_id: str = Query(min_length=1, max_length=128, description="设备编号"),
    window_from: AwareDatetime = Query(description="查询窗起点（含，须带时区）"),
    window_to: AwareDatetime = Query(description="查询窗终点（不含，须带时区）"),
    pool: asyncpg.Pool = Depends(get_pool),
) -> CoverageTimelineOut:
    """查询设备的校准覆盖时间线。

    - 时间窗左闭右开 `[window_from, window_to)`，起点必须早于终点，时刻须带时区；
      缺时区或起点不早于终点返回 422 并说明原因；
    - 结果按时间升序以连续区段**完整覆盖**查询窗：与窗相交的证书只取交集部分，
      证书之间的空隙（含窗内首末）以"无证书"区段补齐，端点相接不留空隙；
      相邻证书即使状态相同也各自成段；
    - 证书区段标为 `active`（有效证书）或 `revoked`（已撤销证书），给出证书编号
      及区段内当前归属该证书的测量数（只按每条测量的最新更正版本计数，
      单条与批次写入均计入）；无证书区段计数为 0；
    - 设备没有任何证书时返回一个覆盖全窗的无证书区段；
    - 区段、最新版本与撤销状态取自 REPEATABLE READ 同一一致视图；
      后续更正或撤销只改变再次查询的结果。
    """
    if not window_from < window_to:
        raise ApiError(
            422,
            "INVALID_TIME_WINDOW",
            "window_from must be earlier than window_to: "
            f"[{window_from.isoformat()}, {window_to.isoformat()})",
        )
    # 统一换算 UTC：响应内所有时刻（含区段端点）按同一基准输出
    window_from = window_from.astimezone(timezone.utc)
    window_to = window_to.astimezone(timezone.utc)

    async with pool.acquire() as conn:
        # 单事务 REPEATABLE READ：证书区段、最新更正版本与撤销状态共享同一快照
        async with conn.transaction(isolation="repeatable_read"):
            certs = await conn.fetch(
                _CERTS_SQL, device_id, window_from, window_to
            )
            count_rows = await conn.fetch(
                _COUNTS_SQL, device_id, window_from, window_to
            )
    counts = {r["certificate_id"]: r["n"] for r in count_rows}

    segments: list[CoverageSegment] = []
    cursor = window_from
    for cert in certs:
        # 证书跨窗只取与查询窗的交集
        seg_from = max(cert["valid_from"], window_from)
        seg_to = min(cert["valid_to"], window_to)
        if cursor < seg_from:
            segments.append(_none_segment(cursor, seg_from))
        segments.append(
            CoverageSegment(
                segment_from=seg_from,
                segment_to=seg_to,
                status="revoked" if cert["revoked_at"] is not None else "active",
                certificate_no=cert["certificate_no"],
                certificate_id=cert["id"],
                measurement_count=counts.get(cert["id"], 0),
            )
        )
        cursor = seg_to
    if cursor < window_to:
        segments.append(_none_segment(cursor, window_to))

    return CoverageTimelineOut(
        device_id=device_id,
        window_from=window_from,
        window_to=window_to,
        segments=segments,
    )
