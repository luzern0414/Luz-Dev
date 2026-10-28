#!/usr/bin/env bash
# Luz 发布与回滚脚本（references/operations.md §3、RUNBOOK.md §1–§2）。在生产/预发主机 /opt/luz/deploy/ 下执行。
#
#   ./release.sh deploy   <registry/luz-api@sha256:...>   # 预检 → 发布前备份 → 迁移 → 健康门控滚动替换 → 冒烟 → 观察 15 分钟
#   ./release.sh rollback [<上一版本镜像>]                 # 默认回到 .release/previous 记录的版本（不回退数据库）
#   ./release.sh watch    [分钟]                          # 单独执行发布后观察
#   ./release.sh status
#
# 前提（expand/contract）：每次迁移必须向后兼容——旧代码能跑在新库结构上。
#   expand 版本：只加表/加可空列/加索引（CONCURRENTLY）/双写；contract 版本（下一次发布）才删列、删表、加 NOT NULL。
#   因此应用回滚不需要回退数据库；数据库只在迁移本身有误时“前向修复”或按 RUNBOOK §3 恢复。
#
# 零停机原理：api 不设 container_name/ports；先把 api 扩到 2 倍（--no-recreate 保留旧容器，新容器用新镜像），
# 等新容器 /readyz 健康，nginx upstream `resolve`（10s）把新 IP 加入；再给旧容器 SIGTERM（uvicorn 20s 内处理完在途请求，
# 停止监听后 nginx 对连接失败自动转到下一台），最后删除旧容器。思路同 docker-rollout 插件，这里不依赖插件。
#
# 环境变量（/etc/luz/release.env 可覆盖）：
#   PROM_URL=http://127.0.0.1:9090   API_HEALTH_URL=https://api.example.com/api/v1/app/config
#   WATCH_MINUTES=15  AUTO_ROLLBACK=1  HEALTH_TIMEOUT=180  SMOKE_CMD=（可选：k6 smoke 或关键流程脚本）
set -Eeuo pipefail

cd "$(dirname "$(readlink -f "$0")")"
# shellcheck disable=SC1091
[ -f /etc/luz/release.env ] && . /etc/luz/release.env

COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.prod.yml}
DC=(docker compose -f "$COMPOSE_FILE")
STATE_DIR=${STATE_DIR:-.release}
PROM_URL=${PROM_URL:-http://127.0.0.1:9090}
API_HEALTH_URL=${API_HEALTH_URL:-}
WATCH_MINUTES=${WATCH_MINUTES:-15}
AUTO_ROLLBACK=${AUTO_ROLLBACK:-1}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}
DNS_SETTLE=${DNS_SETTLE:-15}               # > nginx resolver valid=10s
MIN_FREE_PCT=${MIN_FREE_PCT:-20}
MAX_BACKUP_AGE_H=${MAX_BACKUP_AGE_H:-26}
# 回滚触发阈值（任一满足即回滚）
MAX_5XX_RATIO=${MAX_5XX_RATIO:-0.02}       # 5xx 比例 > 2%（5 分钟窗口）
P95_FACTOR=${P95_FACTOR:-2}                # p95 高于发布前基线 2 倍
P95_FLOOR=${P95_FLOOR:-0.3}                # 基线很低时的下限（秒），避免 20ms→45ms 误触发
MAX_NOTIFY_VERIFY_FAIL=${MAX_NOTIFY_VERIFY_FAIL:-5}   # 支付回调验签失败（10 分钟增量）

mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/release.log"
log() { printf '%s release %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------- Prometheus 查询（返回第一个值，无数据返回 0）
promq() {
    curl -fsS --max-time 5 --data-urlencode "query=$1" "$PROM_URL/api/v1/query" 2>/dev/null |
    python3 -c 'import sys,json
try:
    r=json.load(sys.stdin)["data"]["result"]; v=r[0]["value"][1] if r else "0"
    print("0" if v in ("NaN","+Inf","-Inf") else v)
except Exception:
    print("0")'
}
gt() { python3 -c "import sys; sys.exit(0 if float('$1') > float('$2') else 1)"; }

Q_5XX='sum(rate(http_requests_total{job="luz-api",status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total{job="luz-api"}[5m])), 1e-9)'
Q_P95='histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="luz-api",route!~"/(healthz|readyz|metrics)"}[5m])))'
Q_NOTIFY='sum(increase(luz_payment_notify_verify_failed_total[10m]))'

# ---------------------------------------------------------------- 容器健康等待
wait_healthy() {   # container ids...
    local deadline=$(( $(date +%s) + HEALTH_TIMEOUT )) id st all
    while :; do
        all=1
        for id in "$@"; do
            st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo missing)
            case "$st" in
                healthy) ;;
                unhealthy|exited|dead|missing) log "container ${id:0:12} is $st"; return 1 ;;
                *) all=0 ;;
            esac
        done
        [ "$all" = 1 ] && return 0
        [ "$(date +%s)" -lt "$deadline" ] || { log "health timeout after ${HEALTH_TIMEOUT}s"; return 1; }
        sleep 2
    done
}

# ---------------------------------------------------------------- api 健康门控滚动替换
rolling_replace_api() {
    local old new want
    want=$("${DC[@]}" config --format json | python3 -c 'import sys,json; print(json.load(sys.stdin)["services"]["api"].get("deploy",{}).get("replicas",2))')
    mapfile -t old < <("${DC[@]}" ps -q api)
    if [ "${#old[@]}" -eq 0 ]; then
        "${DC[@]}" up -d --no-deps --wait --wait-timeout "$HEALTH_TIMEOUT" api; return
    fi
    log "api: scale ${#old[@]} -> $(( ${#old[@]} + want )) (old kept, new on $LUZ_IMAGE)"
    "${DC[@]}" up -d --no-deps --no-recreate --scale api=$(( ${#old[@]} + want )) api
    mapfile -t new < <(comm -13 <(printf '%s\n' "${old[@]}" | sort) <("${DC[@]}" ps -q api | sort))
    [ "${#new[@]}" -gt 0 ] || die "no new api containers created"
    if ! wait_healthy "${new[@]}"; then
        log "new api containers unhealthy; removing them, old version keeps serving"
        docker logs --tail 50 "${new[0]}" 2>&1 | tee -a "$LOG" >&2 || true
        docker rm -f "${new[@]}" >/dev/null
        return 1
    fi
    log "new api healthy; waiting ${DNS_SETTLE}s for nginx to resolve new upstreams"
    sleep "$DNS_SETTLE"
    # 旧容器：SIGTERM → uvicorn 停止监听并处理完在途请求（20s）→ 30s 后才 SIGKILL
    docker stop --time 30 "${old[@]}" >/dev/null
    docker rm "${old[@]}" >/dev/null
    log "api: old containers removed"
}

replace_singletons() {   # worker / scheduler：停旧起新（stop_grace_period 内完成当前批次）
    "${DC[@]}" up -d --no-deps --wait --wait-timeout "$HEALTH_TIMEOUT" worker scheduler
}

smoke() {
    if [ -n "$API_HEALTH_URL" ]; then
        curl -fsS --max-time 5 -o /dev/null "$API_HEALTH_URL" || { log "smoke: $API_HEALTH_URL failed"; return 1; }
    fi
    for id in $("${DC[@]}" ps -q api); do
        docker exec "$id" python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3).status == 200 else 1)" \
            || { log "smoke: readyz failed in ${id:0:12}"; return 1; }
    done
    if [ -n "${SMOKE_CMD:-}" ]; then bash -c "$SMOKE_CMD" || { log "smoke: SMOKE_CMD failed"; return 1; }; fi
    log "smoke ok"
}

# ---------------------------------------------------------------- 发布后观察：可量化的回滚触发条件
watch_release() {
    local minutes=${1:-$WATCH_MINUTES} base_p95 limit_p95 end r p n
    base_p95=$(cat "$STATE_DIR/baseline_p95" 2>/dev/null || echo 0)
    limit_p95=$(python3 -c "print(max(float('$base_p95') * $P95_FACTOR, $P95_FLOOR))")
    end=$(( $(date +%s) + minutes * 60 ))
    log "watch ${minutes}m: 5xx>${MAX_5XX_RATIO} | p95>${limit_p95}s (baseline ${base_p95}s x${P95_FACTOR}) | notify verify fail>${MAX_NOTIFY_VERIFY_FAIL}/10m | readyz"
    sleep 60   # 给 5 分钟窗口积累新版本样本
    while [ "$(date +%s)" -lt "$end" ]; do
        r=$(promq "$Q_5XX"); p=$(promq "$Q_P95"); n=$(promq "$Q_NOTIFY")
        log "watch 5xx_ratio=$r p95=${p}s notify_verify_fail=$n"
        if gt "$r" "$MAX_5XX_RATIO"; then log "TRIGGER 5xx ratio $r"; return 1; fi
        if gt "$p" "$limit_p95"; then log "TRIGGER p95 ${p}s"; return 1; fi
        if gt "$n" "$MAX_NOTIFY_VERIFY_FAIL"; then log "TRIGGER notify verify failures $n"; return 1; fi
        smoke >/dev/null 2>&1 || { log "TRIGGER smoke/readyz failed"; return 1; }
        sleep 30
    done
    log "watch passed"
}

preflight() {
    local free age last
    docker manifest inspect "$LUZ_IMAGE" >/dev/null 2>&1 || docker pull -q "$LUZ_IMAGE" >/dev/null || die "image not found: $LUZ_IMAGE"
    [[ "$LUZ_IMAGE" == *@sha256:* ]] || die "deploy by digest only (got $LUZ_IMAGE)"
    free=$(df -P /var/lib/docker | awk 'NR==2 {print 100 - $5}')
    [ "${free%\%}" -ge "$MIN_FREE_PCT" ] || die "disk free ${free}% < ${MIN_FREE_PCT}%"
    # 直接读 backup.sh 写的 textfile 指标，不依赖 Prometheus 是否在线
    last=$(awk '/^luz_backup_last_success_timestamp_seconds/{print $2}' \
        "${TEXTFILE_DIR:-/var/lib/node_exporter/textfile}/luz_backup_pg_dump.prom" 2>/dev/null | head -1)
    age=$(( $(date +%s) - ${last:-0} ))
    if [ "$age" -gt $(( MAX_BACKUP_AGE_H * 3600 )) ]; then log "last daily backup older than ${MAX_BACKUP_AGE_H}h; running one now"; ./backup.sh dump daily; fi
}

record() {   # result
    printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "${SUDO_USER:-$USER}" "$1" "$LUZ_IMAGE" \
        "$("${DC[@]}" --profile release run --rm --no-deps migrate alembic current 2>/dev/null | tail -1 || echo '?')" >> "$STATE_DIR/history.tsv"
}

do_deploy() {
    LUZ_IMAGE=${1:?usage: release.sh deploy <registry/luz-api@sha256:...>}
    export LUZ_IMAGE
    log "=== deploy $LUZ_IMAGE"
    preflight
    [ -f "$STATE_DIR/current" ] && cp "$STATE_DIR/current" "$STATE_DIR/previous"
    promq "$Q_P95" > "$STATE_DIR/baseline_p95"
    # 1. 发布前即时备份（迁移前快照，按版本命名）
    ./backup.sh dump "pre-release-$(date -u +%Y%m%dT%H%M%SZ)"
    # 2. 拉取
    "${DC[@]}" pull -q api worker scheduler
    "${DC[@]}" --profile release pull -q migrate
    # 3. 迁移（expand：旧代码仍可运行）
    "${DC[@]}" --profile release run --rm migrate || die "migration failed; nothing replaced yet (fix forward)"
    # 4. api 健康门控滚动替换；失败时旧版本继续服务
    rolling_replace_api || die "api rollout aborted; previous version still serving"
    echo "$LUZ_IMAGE" > "$STATE_DIR/current"
    # 5. worker / scheduler
    replace_singletons
    # 6. 冒烟 + 观察
    if smoke && watch_release "$WATCH_MINUTES"; then
        record ok; log "=== deploy ok"
    else
        record failed
        if [ "$AUTO_ROLLBACK" = 1 ] && [ -f "$STATE_DIR/previous" ]; then
            log "auto rollback"; do_rollback "$(cat "$STATE_DIR/previous")"
        fi
        die "deploy failed after rollout; see $LOG"
    fi
}

do_rollback() {
    LUZ_IMAGE=${1:-$(cat "$STATE_DIR/previous" 2>/dev/null || true)}
    [ -n "$LUZ_IMAGE" ] || die "no previous version recorded; pass image explicitly"
    export LUZ_IMAGE
    log "=== rollback to $LUZ_IMAGE (database NOT downgraded; expand/contract keeps old code compatible)"
    rolling_replace_api || die "rollback rollout failed"
    replace_singletons
    echo "$LUZ_IMAGE" > "$STATE_DIR/current"
    smoke || log "WARN smoke failed after rollback; escalate"
    record rollback
}

case "${1:-}" in
    deploy)   shift; do_deploy "$@" ;;
    rollback) shift; do_rollback "$@" ;;
    watch)    shift; LUZ_IMAGE=$(cat "$STATE_DIR/current"); export LUZ_IMAGE; watch_release "${1:-$WATCH_MINUTES}" ;;
    status)   cat "$STATE_DIR/current" 2>/dev/null; tail -5 "$STATE_DIR/history.tsv" 2>/dev/null; LUZ_IMAGE=$(cat "$STATE_DIR/current" 2>/dev/null || echo x) "${DC[@]}" ps ;;
    *) echo "usage: $0 {deploy <image@digest>|rollback [image]|watch [minutes]|status}" >&2; exit 2 ;;
esac
