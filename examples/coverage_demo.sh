#!/usr/bin/env bash
# 校准覆盖时间线端到端示例：
#   无证书设备 -> 登记相接/留空的证书 -> 单条+批次写入 ->
#   查询连续区段与最新版本计数 -> 更正迁移测量 -> 左闭右开边界 ->
#   跨窗取交集/时区等价写法 -> 撤销后实时变化 -> 非法时间窗拒绝(422) ->
#   审计快照冻结不受影响。
# 用法: BASE=http://localhost:8000 ./examples/coverage_demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
API="$BASE/api/v1"

if command -v jq >/dev/null 2>&1; then PPRINT="jq ."; else PPRINT="cat"; fi

post() { # post <path> <json-body> [extra curl args]
  local path="$1" body="$2"; shift 2
  echo "==> POST $path"
  curl -sS -X POST "$API$path" -H 'Content-Type: application/json' "$@" -d "$body" | eval "$PPRINT"
  echo
}

get() { # get <path>
  echo "==> GET $1"
  curl -sS "$API$1" | eval "$PPRINT"
  echo
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

D="COV-DEMO-D1"

echo "### 1. 无证书设备先查一次 -> 单个 uncovered 区段覆盖整窗，计数 0"
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2029-01-01T00:00:00%2B08:00"

echo "### 2. 登记三张证书：C1 覆盖 2026 年、C2 覆盖 2027 年（与 C1 端点相接）、C3 覆盖 2028 年 3-9 月（与 C2 间有空隙）"
post /certificates \
  '{"device_id":"COV-DEMO-D1","certificate_no":"COV-DEMO-C1","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: cov-demo-c1'
post /certificates \
  '{"device_id":"COV-DEMO-D1","certificate_no":"COV-DEMO-C2","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: cov-demo-c2'
post /certificates \
  '{"device_id":"COV-DEMO-D1","certificate_no":"COV-DEMO-C3","valid_from":"2028-03-01T00:00:00+08:00","valid_to":"2028-09-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: cov-demo-c3'

echo "### 3. 单条写入 2 条到 C1；批次写入 1 条到 C1、1 条到 C2（批次与单条同等计数）"
post /measurements \
  '{"device_id":"COV-DEMO-D1","measured_at":"2026-06-15T10:30:00+08:00","certificate_no":"COV-DEMO-C1"}'
post /measurements \
  '{"device_id":"COV-DEMO-D1","measured_at":"2026-02-01T09:00:00+08:00","certificate_no":"COV-DEMO-C1"}'
post /measurement-batches \
  '{"batch_key":"cov-demo-batch-1","records":[
      {"device_id":"COV-DEMO-D1","measured_at":"2026-10-01T08:00:00+08:00","certificate_no":"COV-DEMO-C1"},
      {"device_id":"COV-DEMO-D1","measured_at":"2027-05-01T08:00:00+08:00","certificate_no":"COV-DEMO-C2"}]}'

echo "### 4. 查询整窗 [2026, 2029)：连续 5 个区段（C1=3, C2=1, 空隙, C3=0, 尾部空隙）"
echo "    C1 与 C2 端点相接不产生空隙；相邻证书即使同为 valid 也分两个区段"
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2029-01-01T00:00:00%2B08:00"

echo "### 5. 更正第 1 条测量：时刻与证书都迁到 C2 -> C1 变 2、C2 变 2（只按最新版本计数）"
M1=$(curl -sS "$API/measurements?device_id=$D&limit=1000" \
  | python3 -c 'import sys,json;print(min(m["id"] for m in json.load(sys.stdin)))')
post "/measurements/$M1/corrections" \
  '{"base_version":1,"measured_at":"2027-03-01T10:00:00+08:00","certificate_no":"COV-DEMO-C2","reason":"recorded against wrong certificate; time corrected per lab review"}'
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2029-01-01T00:00:00%2B08:00"

echo "### 6. 左闭右开边界：窗终点 2026-10-01T08:00+08 恰为批次测量时刻 -> 不计入（右开）"
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2026-10-01T08:00:00%2B08:00"
echo "    终点放宽 1 秒 -> 该测量计入"
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2026-10-01T08:00:01%2B08:00"

echo "### 7. 跨窗取交集 + 端点相接：[2026-12-15, 2027-02-15) 跨 C1/C2，只有两个相接区段"
get "/calibration-coverage?device_id=$D&from=2026-12-15T00:00:00%2B08:00&to=2027-02-15T00:00:00%2B08:00"

echo "### 8. 时区等价：同一绝对窗 [2027 全年) 用 +08:00 / Z / +03:00 三种写法，计数一致（C2=2）"
get "/calibration-coverage?device_id=$D&from=2027-01-01T00:00:00%2B08:00&to=2028-01-01T00:00:00%2B08:00"
get "/calibration-coverage?device_id=$D&from=2026-12-31T16:00:00Z&to=2027-12-31T16:00:00Z"
get "/calibration-coverage?device_id=$D&from=2026-12-31T19:00:00%2B03:00&to=2027-12-31T19:00:00%2B03:00"

echo "### 9. 撤销 C2 后再次查询：该区段变 revoked，证书编号与计数保留（实时反映）"
CID=$(curl -sS "$API/certificates?device_id=$D" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["certificate_no"]=="COV-DEMO-C2"))')
post "/certificates/$CID/revoke" '{"reason":"drift found during compliance review"}'
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00%2B08:00&to=2029-01-01T00:00:00%2B08:00"

echo "### 10. 非法时间窗：缺时区 / 起点不早于终点 -> 422 且说明原因"
get "/calibration-coverage?device_id=$D&from=2026-01-01T00:00:00&to=2029-01-01T00:00:00%2B08:00"
get "/calibration-coverage?device_id=$D&from=2029-01-01T00:00:00%2B08:00&to=2026-01-01T00:00:00%2B08:00"

echo "### 11. 覆盖查询是实时视图；审计快照仍冻结：撤销后拍快照计数冻结，再次覆盖查询结果不被影响"
post /audit-snapshots \
  '{"device_id":"COV-DEMO-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}'
