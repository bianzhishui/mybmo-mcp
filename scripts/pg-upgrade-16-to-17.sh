#!/usr/bin/env bash
#
# 把 Casdoor 自用的 PostgreSQL 从 16 迁到 17。
#
# 为什么需要它：deploy/casdoor/docker-compose.yml 在 P4 时用了 postgres:16-alpine，
# 与 ADR 0002 D3 定的 17 + bookworm 不符。PG 大版本之间数据目录不通用，只改镜像 tag
# 会让新容器因卷里的 PG_VERSION=16 直接拒绝启动。
#
# 做法：逻辑备份 + 整卷物理备份 → 只删 postgres 卷 → 用 17 重新 initdb → pg_restore
#      → 核对行数。casdoor_data（Casdoor 自己的文件卷）不动。
#
# 用法（在服务器的仓库根目录）：
#   bash scripts/pg-upgrade-16-to-17.sh           # 交互确认
#   bash scripts/pg-upgrade-16-to-17.sh --yes     # 无人值守
#
# 可调环境变量：TARGET_IMAGE（默认 postgres:17-bookworm）、BACKUP_ROOT（默认 /root/casdoor-pg-upgrade）
#
# 可重入：中途失败可以直接再跑一次。postgres 停着时会用**原容器**（16）启动做备份，
# 不会拿 compose 里的 17 去开 16 的数据卷。
#
# 文档：docs/operations.md 7.1

set -euo pipefail

TARGET_IMAGE="${TARGET_IMAGE:-postgres:17-bookworm}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/casdoor-pg-upgrade}"
STAMP="$(date +%F-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$STAMP"
ASSUME_YES=0

for arg in "$@"; do
    case "$arg" in
        --yes | -y) ASSUME_YES=1 ;;
        -h | --help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "未知参数：$arg" >&2; exit 2 ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$REPO_ROOT/deploy/casdoor"

log() { printf '\n== %s\n' "$*"; }
die() { printf '\n失败：%s\n' "$*" >&2; exit 1; }

# 按 compose 标签找 postgres 容器，而不是写死容器名、也不让 compose 按镜像去猜。
# 这样「旧容器（16）做备份」与「新容器（17）做恢复」共用同一套调用。
# 注意全程不用 ps | grep -q：grep 命中即退出会让上游吃 SIGPIPE（141），
# 配合 pipefail 会把"有容器"误判成失败。一律先落变量再判断。
pg_container() {
    local all ct
    all="$(docker ps --filter 'label=com.docker.compose.service=postgres' --format '{{.Names}}')" || true
    ct="$(head -n1 <<< "$all")"
    printf '%s' "$ct"
}

pg_exec() {
    local ct
    ct="$(pg_container)"
    [[ -n "$ct" ]] || return 1
    docker exec -i "$ct" "$@"
}

pg_running() {
    [[ -n "$(pg_container)" ]]
}

old_pg_container() {
    local all ct
    all="$(docker ps -a --filter 'ancestor=postgres:16-alpine' --format '{{.Names}}')" || true
    ct="$(head -n1 <<< "$all")"
    printf '%s' "$ct"
}

cd "$COMPOSE_DIR" || die "进不去 $COMPOSE_DIR"
COMPOSE=(docker compose -f docker-compose.yml)

# ---------------------------------------------------------------- 0. 前置检查
log "0/6 前置检查"

command -v docker >/dev/null || die "docker 未安装"
docker compose version >/dev/null 2>&1 || die "docker compose v2 不可用"
[[ -f docker-compose.yml ]] || die "找不到 $COMPOSE_DIR/docker-compose.yml"
[[ -f .env ]] || die "找不到 $COMPOSE_DIR/.env（POSTGRES_USER / POSTGRES_PASSWORD 在里面）"

# 镜像由 compose 决定，不是本脚本。只更新脚本、没更新 compose 的话，
# 重建出来的还是旧版本——那样会"成功地"迁回 16，必须在动数据卷之前拦住。
COMPOSE_IMAGE="$(grep -E '^[[:space:]]*image:[[:space:]]*postgres:' docker-compose.yml | awk '{print $2}')"
[[ "$COMPOSE_IMAGE" == "$TARGET_IMAGE" ]] \
    || die "compose 里的 postgres 镜像是「${COMPOSE_IMAGE:-未找到}」，不是 $TARGET_IMAGE。先把 deploy/casdoor/docker-compose.yml 更新到位（git pull 或 scp），否则重建出来仍是旧版本"

# 卷名由 compose 的 name: mybmo-casdoor 决定，正常就是 mybmo-casdoor_pgdata
VOLUME="$(docker volume ls --format '{{.Name}}' | grep -x 'mybmo-casdoor_pgdata' || true)"
[[ -n "$VOLUME" ]] || VOLUME="$(docker volume ls --format '{{.Name}}' | grep -E '_pgdata$' | head -n1 || true)"
[[ -n "$VOLUME" ]] || die "找不到 postgres 数据卷，确认部署形态是否就是这套 compose"

PGV="$(docker run --rm -v "$VOLUME":/v alpine cat /v/PG_VERSION 2>/dev/null || true)"
[[ -n "$PGV" ]] || die "读不到 $VOLUME/PG_VERSION，卷可能不是 PG 数据目录"

AVAIL_MB="$(df -Pm /root | awk 'NR==2 {print $4}')"
(( AVAIL_MB >= 1024 )) || die "/root 只剩 ${AVAIL_MB}MB，少于 1GB，清理后再跑"

echo "  数据卷    $VOLUME（PG $PGV）"
echo "  目标镜像  $TARGET_IMAGE"
echo "  备份目录  $BACKUP_DIR"
echo "  可用空间  ${AVAIL_MB}MB"

if [[ "$PGV" == "17" ]]; then
    CNT="$(pg_exec psql -U casdoor -d casdoor -Atc 'select count(*) from application' 2>/dev/null || echo '?')"
    echo "  已经是 17，application 行数：$CNT"
    if [[ "$CNT" == "0" || "$CNT" == "?" ]]; then
        cat <<EOF

数据目录是 17 但库里没有数据，像是上次迁移中断在恢复阶段。从备份恢复：

  cd $COMPOSE_DIR
  docker compose exec -T postgres pg_restore -U casdoor -d casdoor --no-owner \\
    < $BACKUP_ROOT/<时间戳>/casdoor.dump

或整卷回滚（见 docs/operations.md 7.1）。本次不做任何改动。
EOF
        exit 1
    fi
    echo "无需迁移。"
    exit 0
fi
[[ "$PGV" == "16" ]] || die "只支持 16 → 17，当前是 PG $PGV"

# compose 已指向 17，直接 up -d 会让 17 去开 16 的卷并拒绝启动。
# 所以停着的时候优先按**原容器**拉起，镜像还是它自己那份 16。
if ! pg_running; then
    OLD_CT="$(old_pg_container)"
    [[ -n "$OLD_CT" ]] || die "postgres 没在跑，且找不到 postgres:16 的旧容器。compose 已指向 17，没法用它重建 16 集群，先按 docs/operations.md 7.1 恢复"
    log "postgres 处于停止状态，用原容器（$OLD_CT）启动做备份"
    docker start "$OLD_CT" >/dev/null
    for _ in $(seq 1 60); do
        pg_exec pg_isready -U casdoor -d casdoor >/dev/null 2>&1 && break
        sleep 1
    done
fi

PGUSER="$(pg_exec printenv POSTGRES_USER | tr -d '\r')"
[[ -n "$PGUSER" ]] || die "取不到 POSTGRES_USER"

counts() {
    pg_exec psql -U "$PGUSER" -d casdoor -At -F'|' -c "
        select 'tables', count(*) from information_schema.tables where table_schema = 'public'
        union all select 'application', count(*) from application
        union all select 'user', count(*) from \"user\"
        union all select 'organization', count(*) from organization
        order by 1;"
}

log "当前数据"
BEFORE="$(counts)" || die "查询行数失败，先人工确认库是健康的"
echo "$BEFORE" | sed 's/^/  /'

if (( ASSUME_YES == 0 )); then
    [[ -t 0 ]] || die "非交互环境（stdin 不是终端）请显式加 --yes"
    cat <<EOF

把 Casdoor 的库迁到 PostgreSQL 17：
  · 只重建 $VOLUME，casdoor_data 不动
  · Casdoor 会停 1~3 分钟，期间 /mcp 全部 401
  · 备份留在 $BACKUP_DIR，回滚靠它

EOF
    read -r -p "输入 MIGRATE 继续（其他任意键取消）：" answer
    [[ "$answer" == "MIGRATE" ]] || die "已取消，未做任何改动"
fi

# ---------------------------------------------------------------- 1. 逻辑备份
log "1/6 逻辑备份（pg_dump -Fc）"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_ROOT" "$BACKUP_DIR"

pg_exec pg_dump -Fc -U "$PGUSER" -d casdoor > "$BACKUP_DIR/casdoor.dump"
[[ -s "$BACKUP_DIR/casdoor.dump" ]] || die "dump 是空文件，停在这里"
docker run --rm -v "$BACKUP_DIR":/b "$TARGET_IMAGE" pg_restore -l /b/casdoor.dump > "$BACKUP_DIR/toc.txt"
grep -q 'TABLE DATA' "$BACKUP_DIR/toc.txt" || die "dump 里没有任何表数据，停在这里"
printf '%s\n' "$BEFORE" > "$BACKUP_DIR/rows-before.txt"
echo "  $(du -h "$BACKUP_DIR/casdoor.dump" | cut -f1)  $(grep -c 'TABLE DATA' "$BACKUP_DIR/toc.txt") 张表有数据"

# ---------------------------------------------------------------- 2. 物理备份
log "2/6 整卷物理备份（回滚用）"
"${COMPOSE[@]}" stop
docker run --rm -v "$VOLUME":/from -v "$BACKUP_DIR":/backup alpine \
    tar czf /backup/pgdata-$PGV.tgz -C /from .

# 校验一律先落文件再 grep：tar | grep -q 里 grep 命中即退出，tar 会吃 SIGPIPE，
# 配合 pipefail 会把"校验通过"误判成失败（实测退出码 141）。
tar tzf "$BACKUP_DIR/pgdata-$PGV.tgz" > "$BACKUP_DIR/volume-toc.txt" || die "卷备份读不出来"
grep -qxE '\.?/?PG_VERSION' "$BACKUP_DIR/volume-toc.txt" || die "卷备份不完整（缺 PG_VERSION），停下来"
echo "  $(du -h "$BACKUP_DIR/pgdata-$PGV.tgz" | cut -f1)  $BACKUP_DIR/pgdata-$PGV.tgz"

# ---------------------------------------------------------------- 3. 重建
log "3/6 用 17 重建数据卷"
"${COMPOSE[@]}" down
docker volume rm "$VOLUME" >/dev/null
"${COMPOSE[@]}" up -d postgres

ready=0
for _ in $(seq 1 90); do
    if pg_exec pg_isready -U "$PGUSER" -d casdoor >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
(( ready == 1 )) || die "17 起不来，看日志：${COMPOSE[*]} logs --tail=50 postgres"
NEWPGV="$(docker run --rm -v "$VOLUME":/v alpine cat /v/PG_VERSION 2>/dev/null || true)"
echo "  新数据目录 PG_VERSION=$NEWPGV"
[[ "$NEWPGV" == "17" ]] || die "重建出来是 PG $NEWPGV 而不是 17（旧卷已删，用 $BACKUP_DIR/pgdata-$PGV.tgz 按 docs/operations.md 7.1 回滚）"

# ---------------------------------------------------------------- 4. 恢复
log "4/6 恢复数据"
set +e
pg_exec pg_restore -U "$PGUSER" -d casdoor --no-owner --no-privileges \
    < "$BACKUP_DIR/casdoor.dump" 2> "$BACKUP_DIR/restore.err"
RC=$?
set -e
if (( RC != 0 )); then
    echo "  pg_restore 退出码 $RC，错误见 $BACKUP_DIR/restore.err："
    head -20 "$BACKUP_DIR/restore.err" | sed 's/^/    /' || true
fi

# ---------------------------------------------------------------- 5. 校验
log "5/6 核对行数"
AFTER="$(counts)" || die "恢复后查不动库，回滚见 $BACKUP_DIR"
echo "$AFTER" | sed 's/^/  /'
if [[ "$BEFORE" != "$AFTER" ]]; then
    printf '%s\n' "$AFTER" > "$BACKUP_DIR/rows-after.txt"
    die "行数与迁移前不一致（$BACKUP_DIR/rows-before.txt vs rows-after.txt），先别放流量，按 docs/operations.md 7.1 回滚"
fi

# ---------------------------------------------------------------- 6. 收尾
log "6/6 起 Casdoor"
"${COMPOSE[@]}" up -d
sleep 3
"${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'

if command -v curl >/dev/null; then
    if curl -fsS --max-time 10 https://auth.pandabase.top/.well-known/openid-configuration >/dev/null; then
        echo "  discovery 正常"
    else
        echo "  discovery 拉不到，检查 Casdoor 日志：${COMPOSE[*]} logs --tail=50 casdoor"
    fi
fi

cat <<EOF

迁移完成：PG $PGV -> $NEWPGV
备份：
  $BACKUP_DIR/casdoor.dump       逻辑备份（含账号/应用/自定义 scope）
  $BACKUP_DIR/pgdata-$PGV.tgz    整卷物理备份

回滚（趁上面的备份还在）：
  cd $COMPOSE_DIR
  git checkout <升级前的提交> -- docker-compose.yml     # 镜像回到 16
  docker compose down
  docker volume rm $VOLUME && docker volume create $VOLUME
  docker run --rm -v $VOLUME:/to -v $BACKUP_DIR:/backup alpine \\
    sh -c 'cd /to && tar xzf /backup/pgdata-$PGV.tgz'
  docker compose up -d

确认无误后再删 $BACKUP_DIR 里的 tar 包；.dump 建议长期留着。
EOF
