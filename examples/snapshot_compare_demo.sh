#!/usr/bin/env bash
# 两份合规审计快照差异比较端到端示例：
#   登记首尾相接的证书 -> 写入测量 -> 基准快照前先更正一条 ->
#   生成基准快照 -> 两次快照之间做更正/新写入/撤销 -> 生成目标快照 ->
#   比较（进入/离开/变化，按测量编号稳定分页） ->
#   反方向比较 -> 设备不匹配拒绝 -> 时间窗不匹配拒绝 -> 未知标识 404 ->
#   之后再更正不改变同一对快照结果。
# 用法: BASE=http://localhost:8000 ./examples/snapshot_compare_demo.sh
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

json_get() { # json_get <python-expr on `d`>  从标准输入读 JSON
  python3 -c 'import sys,json;d=json.load(sys.stdin);print('"$1"')'
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记三张首尾相接、不重叠的证书（C1=2026 / C2=2027 / C3=2028）"
post /certificates \
  '{"device_id":"DIFF-D1","certificate_no":"DIFF-C1","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: diff-demo-c1'
post /certificates \
  '{"device_id":"DIFF-D1","certificate_no":"DIFF-C2","valid_from":"2027-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: diff-demo-c2'
post /certificates \
  '{"device_id":"DIFF-D1","certificate_no":"DIFF-C3","valid_from":"2028-01-01T00:00:00+08:00","valid_to":"2029-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: diff-demo-c3'
post /certificates \
  '{"device_id":"DIFF-D2","certificate_no":"DIFF-CX","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: diff-demo-cx'

echo "### 2. 写入测量：m1/m2/m3/m6 在 2026 窗内，m4 在 2028 窗外"
post /measurements '{"device_id":"DIFF-D1","measured_at":"2026-02-01T10:00:00+08:00","certificate_no":"DIFF-C1"}'
post /measurements '{"device_id":"DIFF-D1","measured_at":"2026-03-01T10:00:00+08:00","certificate_no":"DIFF-C1"}'
post /measurements '{"device_id":"DIFF-D1","measured_at":"2026-04-01T10:00:00+08:00","certificate_no":"DIFF-C1"}'
post /measurements '{"device_id":"DIFF-D1","measured_at":"2028-06-01T10:00:00+08:00","certificate_no":"DIFF-C3"}'
post /measurements '{"device_id":"DIFF-D1","measured_at":"2026-06-01T10:00:00+08:00","certificate_no":"DIFF-C1"}'

M3=$(curl -sS "$API/measurements?device_id=DIFF-D1&limit=100" \
  | python3 -c 'import sys,json;print(sorted(m["id"] for m in json.load(sys.stdin))[2])')
M6=$(curl -sS "$API/measurements?device_id=DIFF-D1&limit=100" \
  | python3 -c 'import sys,json;print(sorted(m["id"] for m in json.load(sys.stdin))[4])')
M1=$(curl -sS "$API/measurements?device_id=DIFF-D1&limit=100" \
  | python3 -c 'import sys,json;print(sorted(m["id"] for m in json.load(sys.stdin))[0])')
M2=$(curl -sS "$API/measurements?device_id=DIFF-D1&limit=100" \
  | python3 -c 'import sys,json;print(sorted(m["id"] for m in json.load(sys.stdin))[1])')

echo "### 3. 基准快照之前先更正 m3（仅时刻微调，仍在窗内）"
post "/measurements/$M3/corrections" \
  '{"base_version":1,"measured_at":"2026-04-15T10:00:00+08:00","reason":"mid-april date corrected before baseline snapshot"}'

echo "### 4. 生成基准快照：时间窗 [2026-01-01, 2028-01-01)，窗内 4 条全部有效"
RESP_B=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"DIFF-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}')
echo "$RESP_B" | eval "$PPRINT"; echo
BID=$(echo "$RESP_B" | json_get 'd["snapshot_id"]')
echo "    基准快照 baseline_snapshot_id = $BID"

echo "### 5. 两次快照之间的演变"
echo "    5.1 m1 更正测量时刻（仍用 C1）"
post "/measurements/$M1/corrections" \
  '{"base_version":1,"measured_at":"2026-02-15T10:00:00+08:00","reason":"measurement day logged wrong"}'
echo "    5.2 m2 更正到 2027 年并换用 C2（仍在同一时间窗内）"
post "/measurements/$M2/corrections" \
  '{"base_version":1,"measured_at":"2027-03-01T10:00:00+08:00","certificate_no":"DIFF-C2","reason":"re-scheduled to 2027 and re-associated to C2"}'
echo "    5.3 m6 更正到 2028 年：最新版本时刻出窗 -> 比较时应判为 left"
post "/measurements/$M6/corrections" \
  '{"base_version":1,"measured_at":"2028-06-15T10:00:00+08:00","certificate_no":"DIFF-C3","reason":"rescheduled into 2028, outside the 2026-2027 audit window"}'
echo "    5.4 新写入 m5（2026-05，窗内）-> 比较时应判为 entered"
post /measurements '{"device_id":"DIFF-D1","measured_at":"2026-05-01T10:00:00+08:00","certificate_no":"DIFF-C1"}'
echo "    5.5 撤销 DIFF-C1 -> 目标快照中仍挂在 C1 上的记录冻结为无效"
CID1=$(curl -sS "$API/certificates?device_id=DIFF-D1" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["certificate_no"]=="DIFF-C1"))')
post "/certificates/$CID1/revoke" '{"reason":"drift found in re-review"}'

echo "### 6. 生成目标快照（同设备、同一绝对时间窗）：4 条，1 有效 / 3 无效"
RESP_T=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"DIFF-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}')
echo "$RESP_T" | eval "$PPRINT"; echo
TID=$(echo "$RESP_T" | json_get 'd["snapshot_id"]')
echo "    目标快照 target_snapshot_id = $TID"

echo "### 7. 比较两份冻结快照：entered=1 / left=1 / changed=3，按测量编号升序"
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$TID&limit=2&offset=0"
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$TID&limit=2&offset=2"
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$TID&limit=2&offset=4"

echo "    变化项含前后（baseline/target）两侧字段；进入项只含 target 侧；离开项只含 baseline 侧。"

echo "### 8. 交换基准/目标方向：entered 与 left 互换，changed 数量不变"
get "/audit-snapshots/compare?baseline_snapshot_id=$TID&target_snapshot_id=$BID&limit=100"

echo "### 9. 不同时区写法但同一绝对窗口：可以比较，差异为零"
RESP_TZ=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"DIFF-D1","window_from":"2025-12-31T16:00:00Z","window_to":"2027-12-31T16:00:00Z"}')
TIDZ=$(echo "$RESP_TZ" | json_get 'd["snapshot_id"]')
get "/audit-snapshots/compare?baseline_snapshot_id=$TID&target_snapshot_id=$TIDZ"

echo "### 10. 拒绝比较：设备不同 -> 422 SNAPSHOT_DEVICE_MISMATCH（错误体说明双方设备）"
RESP_D2=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"DIFF-D2","window_from":"2026-01-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}')
D2ID=$(echo "$RESP_D2" | json_get 'd["snapshot_id"]')
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$D2ID"

echo "### 11. 拒绝比较：时间窗按绝对时刻不同 -> 422 SNAPSHOT_WINDOW_MISMATCH（错误体列出双方窗口）"
RESP_W=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"DIFF-D1","window_from":"2026-02-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}')
WID=$(echo "$RESP_W" | json_get 'd["snapshot_id"]')
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$WID"

echo "### 12. 未知快照标识 -> 404 SNAPSHOT_NOT_FOUND（基准或目标未知都一致）"
get "/audit-snapshots/compare?baseline_snapshot_id=deadbeefdeadbeefdeadbeefdeadbeef&target_snapshot_id=$TID"
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=deadbeefdeadbeefdeadbeefdeadbeef"

echo "### 13. 冻结稳定性：快照之后再做更正，同一对快照的比较结果不变"
post "/measurements/$M1/corrections" \
  '{"base_version":2,"measured_at":"2027-09-01T10:00:00+08:00","certificate_no":"DIFF-C2","reason":"late correction after both snapshots"}'
get "/audit-snapshots/compare?baseline_snapshot_id=$BID&target_snapshot_id=$TID&limit=100"
echo "    （与第 7 步汇总页结果一致：entered=1 / left=1 / changed=3）"
