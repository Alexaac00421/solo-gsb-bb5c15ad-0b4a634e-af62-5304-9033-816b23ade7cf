"""请求/响应模型。

所有时刻字段使用 AwareDatetime：缺少时区信息的请求会被 422 拒绝。
"""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 证书
# ---------------------------------------------------------------------------

class CertificateCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")
    valid_from: AwareDatetime = Field(description="有效期起点（含，须带时区）")
    valid_to: AwareDatetime = Field(description="有效期终点（不含，须带时区）")

    @model_validator(mode="after")
    def _check_range(self) -> "CertificateCreate":
        if not self.valid_from < self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to")
        return self


class CertificateOut(BaseModel):
    id: int
    device_id: str
    certificate_no: str
    valid_from: datetime
    valid_to: datetime
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str = Field(description="active / revoked")
    created_at: datetime


class RevokeRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500, description="撤销原因")


# ---------------------------------------------------------------------------
# 测量记录
# ---------------------------------------------------------------------------

class MeasurementCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    measured_at: AwareDatetime = Field(description="测量时刻（须带时区）")
    certificate_no: str = Field(min_length=1, max_length=128, description="证书编号")


# ---------------------------------------------------------------------------
# 测量批次
# ---------------------------------------------------------------------------

class MeasurementBatchCreate(BaseModel):
    batch_key: str = Field(
        min_length=1,
        max_length=255,
        description="批次键：由调用方指定，相同键 + 相同有序内容重试返回首次结果",
    )
    records: list[MeasurementCreate] = Field(
        min_length=1,
        max_length=1000,
        description="按顺序排列的测量记录；任一条不合格则整批不落库",
    )


class MeasurementBatchOut(BaseModel):
    batch_key: str
    measurement_ids: list[int] = Field(
        description="与输入记录顺序一一对应的测量记录标识"
    )
    created_at: datetime


class Validity(BaseModel):
    is_valid: bool
    reason: str


class MeasurementOut(BaseModel):
    id: int
    device_id: str
    measured_at: datetime
    certificate_no: str
    certificate_id: int
    created_at: datetime
    validity: Validity = Field(description="该记录按当前证书状态计算的实时有效性")


# ---------------------------------------------------------------------------
# 测量更正（追加式版本链）
# ---------------------------------------------------------------------------

class CorrectionCreate(BaseModel):
    base_version: int = Field(
        ge=1,
        description="当前最新版本号（乐观锁）：原始登记为 1，每次更正 +1；"
        "与最新版本不符时返回 409",
    )
    measured_at: AwareDatetime | None = Field(
        default=None, description="更正后的测量时刻（须带时区）；缺省沿用当前版本值"
    )
    certificate_no: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="更正后的证书编号（须属于该设备）；缺省沿用当前版本值",
    )
    reason: str = Field(min_length=1, max_length=500, description="更正原因")

    @model_validator(mode="after")
    def _check_at_least_one_field(self) -> "CorrectionCreate":
        if self.measured_at is None and self.certificate_no is None:
            raise ValueError("at least one of measured_at / certificate_no must be provided")
        return self


class MeasurementVersionOut(BaseModel):
    version: int = Field(description="版本号：原始登记为 1，每次更正 +1")
    device_id: str
    measured_at: datetime
    certificate_no: str
    certificate_id: int
    reason: str | None = Field(description="更正原因；原始版本为 null")
    created_at: datetime
    validity: Validity = Field(description="该版本按当前证书状态实时计算的有效性")


class MeasurementHistoryOut(BaseModel):
    measurement_id: int
    device_id: str
    original: MeasurementVersionOut = Field(description="原始版本（version=1）")
    corrections: list[MeasurementVersionOut] = Field(description="按版本号升序排列的更正链")
    current: MeasurementVersionOut = Field(description="当前生效版本（无更正时即原始版本）")


# ---------------------------------------------------------------------------
# 合规审计快照（生成后不可变）
# ---------------------------------------------------------------------------

class AuditSnapshotCreate(BaseModel):
    device_id: str = Field(min_length=1, max_length=128, description="设备编号")
    window_from: AwareDatetime = Field(description="审计窗起点（含，须带时区）")
    window_to: AwareDatetime = Field(description="审计窗终点（不含，须带时区）")

    @model_validator(mode="after")
    def _check_range(self) -> "AuditSnapshotCreate":
        if not self.window_from < self.window_to:
            raise ValueError("window_from must be earlier than window_to")
        return self


class SnapshotValidity(BaseModel):
    is_valid: bool = Field(description="生成快照时冻结的有效性；此后证书撤销不影响快照")
    reason: str


class AuditSnapshotOut(BaseModel):
    snapshot_id: str = Field(description="快照标识（随机 token），凭此分页读取明细")
    device_id: str
    window_from: datetime
    window_to: datetime
    total_count: int = Field(description="窗内记录总数（有效 + 无效；空窗为 0）")
    valid_count: int
    invalid_count: int
    created_at: datetime = Field(description="快照生成时刻")


class AuditSnapshotItemOut(BaseModel):
    measurement_id: int = Field(description="测量编号（分页稳定排序键）")
    version: int = Field(description="生成快照时该测量的最新版本号：原始登记为 1，每次更正 +1")
    measured_at: datetime = Field(description="该最新版本的测量时刻")
    certificate_no: str = Field(description="该最新版本登记/更正所用证书编号")
    validity: SnapshotValidity = Field(
        description="生成快照时冻结的有效性及原因（取自当时的证书撤销状态）"
    )


class AuditSnapshotPage(BaseModel):
    snapshot_id: str
    device_id: str
    window_from: datetime
    window_to: datetime
    total_count: int
    valid_count: int
    invalid_count: int
    limit: int
    offset: int
    items: list[AuditSnapshotItemOut] = Field(
        description="按 measurement_id 升序稳定分页的明细页"
    )


# ---------------------------------------------------------------------------
# 两份冻结审计快照的差异比较
# ---------------------------------------------------------------------------

# 差异类型：仅目标存在 / 仅基准存在 / 两边都有但冻结字段不同
SnapshotDiffType = Literal["entered", "left", "changed"]


class AuditSnapshotDiffItem(BaseModel):
    measurement_id: int = Field(description="测量编号（差异合并列表的稳定排序键）")
    diff_type: SnapshotDiffType = Field(
        description="entered=仅目标快照存在；left=仅基准快照存在；"
        "changed=两边都有但版本/时刻/证书编号/有效性/原因任一不同"
    )
    baseline: AuditSnapshotItemOut | None = Field(
        default=None,
        description="基准侧冻结字段：仅 left / changed 时存在；entered 时为 null",
    )
    target: AuditSnapshotItemOut | None = Field(
        default=None,
        description="目标侧冻结字段：仅 entered / changed 时存在；left 时为 null",
    )


class AuditSnapshotDiffPage(BaseModel):
    baseline_snapshot_id: str
    target_snapshot_id: str
    device_id: str = Field(description="两份快照必须同设备（不符则拒绝比较）")
    window_from: datetime = Field(description="两份快照共有的、按绝对时刻相同的时间窗起点")
    window_to: datetime = Field(description="两份快照共有的、按绝对时刻相同的时间窗终点")
    entered_count: int = Field(description="仅目标快照存在的测量数")
    left_count: int = Field(description="仅基准快照存在的测量数")
    changed_count: int = Field(description="两边都有但冻结字段不同的测量数")
    diff_count: int = Field(description="差异总数（entered + left + changed）")
    limit: int
    offset: int
    items: list[AuditSnapshotDiffItem] = Field(
        description="三类差异按 measurement_id 升序合并后的稳定分页；完全相同的测量不输出"
    )


# ---------------------------------------------------------------------------
# 校准覆盖时间线（实时一致视图查询，不物化、不冻结）
# ---------------------------------------------------------------------------

# 区段类型：有效证书 / 已撤销证书 / 无证书
CoverageSegmentType = Literal["valid", "revoked", "uncovered"]


class CoverageSegment(BaseModel):
    segment_from: datetime = Field(description="区段起点（含），按时间升序拼接覆盖整个查询窗")
    segment_to: datetime = Field(description="区段终点（不含）；与下一区段起点相接，不产生空隙")
    segment_type: CoverageSegmentType = Field(
        description="valid=有效证书；revoked=已撤销证书；uncovered=无证书"
    )
    certificate_no: str | None = Field(
        default=None,
        description="证书编号：valid / revoked 区段给出所属证书；uncovered 为 null",
    )
    measurement_count: int = Field(
        description="该区段内当前归属该证书的测量数（每条测量只按最新更正版本计数，"
        "单条与批次写入均计入）；uncovered 区段恒为 0"
    )


class CoverageTimelineOut(BaseModel):
    device_id: str
    window_from: datetime = Field(description="查询窗起点（含，左闭右开）")
    window_to: datetime = Field(description="查询窗终点（不含）")
    segment_count: int = Field(description="区段总数（相邻证书即使状态相同也分开）")
    segments: list[CoverageSegment] = Field(
        description="按时间升序、连续完整覆盖查询窗 [window_from, window_to) 的区段序列；"
        "设备无任何证书时为单个 uncovered 区段且计数为 0"
    )
