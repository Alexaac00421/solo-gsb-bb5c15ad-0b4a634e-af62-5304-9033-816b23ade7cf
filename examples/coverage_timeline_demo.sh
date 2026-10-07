#!/usr/bin/env bash
# 校准覆盖时间线端到端示例：
#   登记首尾相接/带空隙的证书 -> 单条与批次写入测量 -> 查询时间线（连续区段完整覆盖） ->
#   更正测量（计数随最新版本迁移）-> 生成审计快照 -> 撤销证书 ->
#   时间线区段变为已撤销（审计快照保持冻结）-> 窗口与证书端点相接 ->
#   空隙窗口/无证书设备 -> 缺时区与起点不早于终点拒绝。
# 用法: BASE=http://localhost:8000 ./examples/coverage_timeline_demo.sh
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

timeline() { # timeline <device_id> <window_from> <window_to>（查询参数做 URL 编码，+08:00 等写法安全）
  local device="$1" from="$2" to="$3"
  echo "==> GET /coverage-timeline?device_id=$device&window_from=$from&window_to=$to"
  curl -sS -G "$API/coverage-timeline" \
    --data-urlencode "device_id=$device" \
    --data-urlencode "window_from=$from" \
    --data-urlencode "window_to=$to" | eval "$PPRINT"
  echo
}

json_get() { # json_get <python-expr on `d`>  从标准输入读 JSON
  python3 -c 'import sys,json;d=json.load(sys.stdin);print('"$1"')'
}

echo "### 0. 健康检查"
curl -sS "$BASE/health" | eval "$PPRINT"; echo

echo "### 1. 登记三张证书：C1 与 C2 首尾相接，C2 与 C3 之间留有空隙"
post /certificates \
  '{"device_id":"TL-D1","certificate_no":"TL-C1","valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2026-07-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: tl-demo-c1'
post /certificates \
  '{"device_id":"TL-D1","certificate_no":"TL-C2","valid_from":"2026-07-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: tl-demo-c2'
post /certificates \
  '{"device_id":"TL-D1","certificate_no":"TL-C3","valid_from":"2027-06-01T00:00:00+08:00","valid_to":"2028-01-01T00:00:00+08:00"}' \
  -H 'Idempotency-Key: tl-demo-c3'

echo "### 2. 写入测量：单条 m1/m2/m5 + 批次 m3/m4（单条与批次写入均计入区段测量数）"
post /measurements '{"device_id":"TL-D1","measured_at":"2026-03-01T10:00:00+08:00","certificate_no":"TL-C1"}'
post /measurements '{"device_id":"TL-D1","measured_at":"2026-04-01T10:00:00+08:00","certificate_no":"TL-C1"}'
post /measurement-batches \
  '{"batch_key":"tl-demo-batch-1","records":[
      {"device_id":"TL-D1","measured_at":"2026-05-01T10:00:00+08:00","certificate_no":"TL-C1"},
      {"device_id":"TL-D1","measured_at":"2026-08-01T10:00:00+08:00","certificate_no":"TL-C2"}]}'
post /measurements '{"device_id":"TL-D1","measured_at":"2027-07-01T10:00:00+08:00","certificate_no":"TL-C3"}'

M2=$(curl -sS "$API/measurements?device_id=TL-D1&limit=100" \
  | python3 -c 'import sys,json;print(sorted(m["id"] for m in json.load(sys.stdin))[1])')

echo "### 3. 查询时间线：窗 [2025-12-01, 2027-08-01)，连续区段完整覆盖、首尾相接无空隙"
echo "    区段依次：无证书(窗首空隙) -> TL-C1(active,3) -> TL-C2(active,1) -> 无证书(证书间空隙) -> TL-C3(active,1，跨窗只取交集)"
timeline TL-D1 "2025-12-01T00:00:00+08:00" "2027-08-01T00:00:00+08:00"

echo "### 4. 更正 m2：改挂 TL-C2 并移到 2026-09-01 -> 计数只按最新更正版本"
post "/measurements/$M2/corrections" \
  '{"base_version":1,"measured_at":"2026-09-01T10:00:00+08:00","certificate_no":"TL-C2","reason":"re-associated to TL-C2 per lab review"}'

echo "### 5. 再次查询同一时间线：TL-C1 区段计数 3->2，TL-C2 区段计数 1->2"
timeline TL-D1 "2025-12-01T00:00:00+08:00" "2027-08-01T00:00:00+08:00"

echo "### 6. 撤销前先生成一张合规审计快照（用于对照：时间线随后变化，快照保持冻结）"
RESP_S=$(curl -sS -X POST "$API/audit-snapshots" -H 'Content-Type: application/json' \
  -d '{"device_id":"TL-D1","window_from":"2026-01-01T00:00:00+08:00","window_to":"2028-01-01T00:00:00+08:00"}')
echo "$RESP_S" | eval "$PPRINT"; echo
SID=$(echo "$RESP_S" | json_get 'd["snapshot_id"]')

echo "### 7. 撤销 TL-C2"
CID2=$(curl -sS "$API/certificates?device_id=TL-D1" \
  | python3 -c 'import sys,json;print(next(c["id"] for c in json.load(sys.stdin) if c["certificate_no"]=="TL-C2"))')
post "/certificates/$CID2/revoke" '{"reason":"drift found in re-review"}'

echo "### 8. 撤销后再次查询：TL-C2 区段变为 revoked（测量数不变，仍归属该证书）"
timeline TL-D1 "2025-12-01T00:00:00+08:00" "2027-08-01T00:00:00+08:00"

echo "### 9. 审计快照保持冻结：第 6 步快照中 TL-C2 的记录仍为生成时的有效状态"
get "/audit-snapshots/$SID/items?limit=100"

echo "### 10. 窗口与证书端点相接：[2026-07-01, 2027-01-01) 恰为 TL-C2 有效期 -> 单一证书区段"
timeline TL-D1 "2026-07-01T00:00:00+08:00" "2027-01-01T00:00:00+08:00"

echo "### 11. 窗口落在证书空隙内：[2027-02-01, 2027-03-01) -> 单一无证书区段"
timeline TL-D1 "2027-02-01T00:00:00+08:00" "2027-03-01T00:00:00+08:00"

echo "### 12. 无证书设备：返回一个覆盖全窗的无证书区段，计数为零"
timeline TL-EMPTY "2026-01-01T00:00:00+08:00" "2027-01-01T00:00:00+08:00"

echo "### 13. 非法时间窗：缺时区 / 起点不早于终点 -> 422 并说明原因"
echo "==> GET /coverage-timeline（window_from 缺时区）"
curl -sS -G "$API/coverage-timeline" \
  --data-urlencode "device_id=TL-D1" \
  --data-urlencode "window_from=2026-01-01T00:00:00" \
  --data-urlencode "window_to=2027-01-01T00:00:00+08:00" | eval "$PPRINT"
echo
timeline TL-D1 "2027-01-01T00:00:00+08:00" "2026-01-01T00:00:00+08:00"
timeline TL-D1 "2026-06-01T00:00:00+08:00" "2026-06-01T00:00:00+08:00"

echo "### 14. 不同时区写法的同一绝对窗口：结果与第 8 步一致（时刻统一按 UTC 输出）"
timeline TL-D1 "2025-11-30T16:00:00Z" "2027-07-31T16:00:00Z"
