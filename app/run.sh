#!/usr/bin/env bash
# ============================================================
# HRSSC_Management 服务管理脚本（多 Worker uvicorn）
# 用法:
#   ./run.sh {start|stop|restart|status} [workers]
#   workers: worker 进程数，默认 4
#
# 服务: uvicorn 跑 app.app:app，监听 10.85.212.73:8091
# 启动方式: venv uvicorn + setsid（脱离会话），stdout/stderr 写入 logs/hrssc_stdout.log
# PID 文件: logs/.run.pid 记录主进程（supervisor）PID
# 注意:
#   - 多 worker 模式必须用 uvicorn 命令行启动（app:app 字符串形式）。
#   - 本脚本只管理本端口(8091)的实例：进程匹配同时校验 `--port 8091`，
#     不会误杀 8091/8099 等其它端口上运行的同类服务。
# ============================================================
set -u

# ---- 配置 ----
APP_DIR="/root/iron/HRSSC_Management"
APP_MODULE="app.app:app"
HOST="10.85.212.73"
PORT="8091"
VENV_DIR="${APP_DIR}/../iron_venv"
[ -x "${VENV_DIR}/bin/uvicorn" ] || VENV_DIR="${APP_DIR}/iron_venv"            # 兜底：venv 在项目内
UVICORN="${VENV_DIR}/bin/uvicorn"
PYTHON_BIN="${VENV_DIR}/bin/python"
LOG_FILE="${APP_DIR}/logs/hrssc_stdout.log"
PID_FILE="${APP_DIR}/logs/.run.pid"
DEFAULT_WORKERS=4

cd "${APP_DIR}" || { echo "工作目录不存在: ${APP_DIR}"; exit 1; }

# ---- 颜色 ----
if [ -t 1 ]; then G='\033[32m'; R='\033[31m'; Y='\033[33m'; N='\033[0m'
else G=''; R=''; Y=''; N=''; fi
ok()   { printf "${G}[OK]${N} %s\n"  "$1"; }
err()  { printf "${R}[ERR]${N} %s\n" "$1"; }
info() { printf "${Y}>>>${N} %s\n"  "$1"; }

# ---- 进程匹配（按端口精确区分）----
# 只识别"本端口"的服务进程：多 worker 的 uvicorn supervisor、worker 子进程(--child)。
# 通过 `--port ${PORT}` 锁定本端口，避免误杀 8091/8099 等同类实例。
# 防 pgrep 自匹配：对候选 PID 读取 /proc/<pid>/cmdline 做白名单 + 端口双重校验。
is_service_proc() {
    local pid="$1" cmd
    cmd=$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null)
    [ -z "${cmd}" ] && return 1
    # 排除：脚本自身、shell、pgrep/grep 等查找工具（避免自匹配）
    case "${cmd}" in
        *run.sh*|*" pgrep "*|*" grep "*|*bash*run.sh*|*/bin/sh*) return 1 ;;
    esac
    # 命中服务 + 端口匹配：cmdline 同时含 uvicorn app.app:app 与 --port ${PORT}
    case "${cmd}" in
        *"uvicorn app.app:app"*"--port ${PORT}"*) return 0 ;;
        *) return 1 ;;
    esac
}
running_pids() {
    # 只匹配本端口服务，再补齐同 PGID 的 worker（uvicorn 的 worker 用 spawn_main fork，cmdline 不含 --port）。
    # 这样 status 能看到 supervisor + 各 worker，stop 也能按 PGID 完整清理。
    local sup="" p
    for p in $(pgrep -f "app\.app:app.*--port ${PORT}" 2>/dev/null | sort -n); do
        if is_service_proc "${p}"; then sup="${p}"; break; fi
    done
    if [ -z "${sup}" ]; then return; fi
    local base_pgid
    base_pgid=$(ps -o pgid= -p "${sup}" 2>/dev/null | tr -d ' ')
    echo "${sup}"
    # 列出与 supervisor 同进程组的所有 python 子进程（即 worker）
    if [ -n "${base_pgid}" ]; then
        for p in $(ps -eo pid,pgid,comm | awk -v g="${base_pgid}" '$2==g && $3=="python3" {print $1}' 2>/dev/null); do
            [ "${p}" != "${sup}" ] && echo "${p}"
        done
    fi
}
supervisor_pid() {
    local pid=""
    if [ -f "${PID_FILE}" ]; then
        pid=$(cat "${PID_FILE}" 2>/dev/null)
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null && is_service_proc "${pid}"; then
            echo "${pid}"; return 0
        fi
    fi
    # running_pids 返回"supervisor + 同 PGID worker"，其中 uvicorn supervisor 是唯一 cmdline 含
    # `uvicorn app.app:app --port` 的进程（worker 为 spawn_main 子进程），据此识别 supervisor。
    local p cmd
    for p in $(running_pids); do
        cmd=$(tr '\0' ' ' < "/proc/${p}/cmdline" 2>/dev/null)
        case "${cmd}" in
            *"uvicorn app.app:app"*) echo "${p}"; return 0 ;;
        esac
    done
    running_pids | head -1
}

# ---- 启动 ----
do_start() {
    local workers="${1:-${DEFAULT_WORKERS}}"
    local cur
    cur=$(supervisor_pid)
    if [ -n "${cur}" ]; then
        err "服务已在运行 (supervisor PID ${cur})"; exit 1
    fi
    if [ ! -f "app/app.py" ]; then
        err "入口文件不存在: ${APP_DIR}/app/app.py"; exit 1
    fi
    mkdir -p "${APP_DIR}/logs"
    info "启动 uvicorn ${APP_MODULE} ... workers=${workers} python=${PYTHON_BIN}"
    # setsid + </dev/null 完全脱离会话，避免父 shell 退出时连累服务；脚本退出不影响服务存活
    setsid "${UVICORN}" "${APP_MODULE}" \
        --host "${HOST}" --port "${PORT}" --workers "${workers}" \
        < /dev/null >> "${LOG_FILE}" 2>&1 &
    local pid=$!
    echo "${pid}" > "${PID_FILE}"

    # 等待最多 ~15s，确认 supervisor 没立刻挂、worker 起来
    for _ in $(seq 1 30); do
        sleep 0.5
        if ! kill -0 "${pid}" 2>/dev/null; then
            err "supervisor 已退出，请查日志: tail -50 ${LOG_FILE}"
            rm -f "${PID_FILE}"
            exit 1
        fi
    done
    local npids
    npids=$(running_pids | wc -l)
    ok "服务已启动 (supervisor PID ${pid}，共 ${npids} 个相关进程)"
    info "监听: ${HOST}:${PORT}  日志: ${LOG_FILE}"
}

# ---- 停止 ----
do_stop() {
    local sup
    sup=$(supervisor_pid)
    if [ -z "${sup}" ]; then
        info "服务未在运行"
        rm -f "${PID_FILE}"
        return 0
    fi
    # 杀整个进程组（supervisor + workers 同属一个 PGID），确保 worker 也一起退
    local pgid
    pgid=$(ps -o pgid= -p "${sup}" 2>/dev/null | tr -d ' ')
    if [ -n "${pgid}" ]; then
        info "停止进程组 PGID ${pgid} ..."
        kill -- "-${pgid}" 2>/dev/null
    else
        info "停止 PID ${sup} ..."
        kill "${sup}" 2>/dev/null
    fi
    for _ in $(seq 1 24); do
        sleep 0.5
        [ -z "$(running_pids)" ] && break
    done
    local remain
    remain=$(running_pids)
    if [ -n "${remain}" ]; then
        err "仍有进程未退出，强杀: ${remain}"
        kill -9 ${remain} 2>/dev/null
        sleep 1
    fi
    ok "已停止"
    rm -f "${PID_FILE}"
}

# ---- 状态 ----
do_status() {
    local sup
    sup=$(supervisor_pid)
    if [ -n "${sup}" ]; then
        ok "运行中 (supervisor PID ${sup})"
        local pids npids
        pids=$(running_pids | tr '\n' ' ')
        npids=$(running_pids | wc -l)
        info "相关进程(${npids}): ${pids}"
        if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
            ok "端口 ${PORT} 正在监听"
        else
            info "端口 ${PORT} 暂未监听（可能还在启动中）"
        fi
    else
        err "未运行"
    fi
}

# ---- 入口 ----
WORKERS_ARG="${2:-}"
case "${1:-}" in
    start)   do_start "${WORKERS_ARG}" ;;
    stop)    do_stop ;;
    restart) do_stop; do_start "${WORKERS_ARG}" ;;
    status)  do_status ;;
    *)
        echo "用法: $0 {start|stop|restart|status} [workers]"
        echo "  start [N]   启动 uvicorn 多 worker 服务（默认 ${DEFAULT_WORKERS} 个 worker）"
        echo "  stop        停止（按进程组停，连带所有 worker）"
        echo "  restart [N] 停止并重启"
        echo "  status      查看状态、worker 进程、端口"
        echo "监听端口: ${PORT}（仅管理本端口实例，不影响 8091 等其它服务）"
        exit 1
        ;;
esac