import sys
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

# ================= 路径配置 =================
PROJECT_ROOT = os.getenv("WORK_PATH", "")
if __package__ in {None, ""} and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from sso_did.auth import TokenData, get_current_user

# ================= 配置运行日志 =================
LOG_DIR = Path(PROJECT_ROOT) / "logs"
LOG_FILE = "data_report.log"
LOG_PATH = os.path.join(LOG_DIR, LOG_FILE)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
logger = logging.getLogger("DataReport")
if not logger.handlers:
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    from concurrent_log_handler import ConcurrentRotatingFileHandler
    file_handler = ConcurrentRotatingFileHandler(LOG_PATH, "a", 10 * 1024 * 1024, 5, encoding='utf-8', lock_file_directory="/tmp")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.setLevel(logging.INFO)
logger.info(f"日志文件: {LOG_PATH}")

# ================= PG 配置 =================
CONV_TABLE = os.getenv("PG_MYAGENT_CONVERSATIONS_TABLE", "")
def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def _range_conds(start_date: str | None = None, end_date: str | None = None) -> tuple[list[str], list]:
    """构造 created_at 的时间过滤条件（不含 WHERE 关键字）。start_date/end_date 为 'YYYY-MM-DD'。"""
    conds: list[str] = []
    params: list = []
    if start_date:
        conds.append("created_at >= %s::date")
        params.append(start_date)
    if end_date:
        conds.append("created_at < (%s::date + INTERVAL '1 day')")
        params.append(end_date)
    return conds, params


def _range_clause(start_date: str | None = None, end_date: str | None = None) -> tuple[str, list]:
    """构造 created_at 的时间过滤子句（含 WHERE 前缀，仅当有条件时）。"""
    conds, params = _range_conds(start_date, end_date)
    return (" WHERE " + " AND ".join(conds) if conds else ""), params

# ================= 数据分析函数 =================
def get_total_stats(start_date: str | None = None, end_date: str | None = None) -> dict:
    """基础统计：总对话数、总消息数、日均使用量。"""
    try:
        range_sql, range_params = _range_clause(start_date, end_date)
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*)                                                  AS total_messages,
                        COUNT(DISTINCT session_id)                                AS total_sessions,
                        COUNT(DISTINCT uid)                                       AS total_users,
                        COUNT(*) FILTER (WHERE role = 'user')                     AS user_messages,
                        COUNT(*) FILTER (WHERE role = 'assistant')                AS assistant_messages,
                        AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL)   AS avg_duration_ms,
                        COALESCE(SUM(total_token), 0)                             AS total_tokens,
                        COALESCE(SUM(prompt_token), 0)                            AS total_prompt_tokens,
                        COALESCE(SUM(completion_token), 0)                        AS total_completion_tokens
                    FROM {CONV_TABLE}
                    {range_sql}
                """, range_params)
                row = cur.fetchone()

                # 近7天活跃会话数（叠加所选时间范围）
                conds = ["created_at >= NOW() - INTERVAL '7 days'"]
                params7 = []
                if start_date:
                    conds.append("created_at >= %s::date")
                    params7.append(start_date)
                if end_date:
                    conds.append("created_at < (%s::date + INTERVAL '1 day')")
                    params7.append(end_date)
                cur.execute(f"""
                    SELECT COUNT(DISTINCT session_id)
                    FROM {CONV_TABLE}
                    WHERE {' AND '.join(conds)}
                """, params7)
                active_sessions_7d = cur.fetchone()[0] or 0

                # 最早和最晚记录时间
                cur.execute(f"""
                    SELECT MIN(created_at), MAX(created_at)
                    FROM {CONV_TABLE}
                """)
                min_ts, max_ts = cur.fetchone()

        total_messages = row[0] or 0
        total_sessions = row[1] or 0
        total_users = row[2] or 0
        user_msgs = row[3] or 0
        assistant_msgs = row[4] or 0
        avg_duration = float(round(row[5], 1)) if row[5] else 0
        total_tokens = int(row[6] or 0)
        total_prompt_tokens = int(row[7] or 0)
        total_completion_tokens = int(row[8] or 0)

        # 计算日平均消息数（有数据的天数）
        days_active = 1
        if min_ts and max_ts:
            days_active = max(1, (max_ts - min_ts).days + 1)
        daily_avg = float(round(total_messages / days_active, 1))

        return {
            "total_sessions": total_sessions,
            "total_users": total_users,
            "total_messages": total_messages,
            "user_messages": user_msgs,
            "assistant_messages": assistant_msgs,
            "avg_duration_ms": avg_duration,
            "total_tokens": total_tokens,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "daily_avg_messages": daily_avg,
            "active_sessions_7d": active_sessions_7d,
            "days_active": days_active,
        }
    except Exception as e:
        logger.error(f"[GetTotalStats] 查询失败: {e}")
        return {}


def get_mode_stats(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """各对话模式的统计。"""
    try:
        range_sql, range_params = _range_clause(start_date, end_date)
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        mode,
                        COUNT(*)                                    AS message_count,
                        COUNT(DISTINCT session_id)                  AS session_count,
                        ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL), 1) AS avg_duration_ms
                    FROM {CONV_TABLE}
                    {range_sql}
                    AND mode IS NOT NULL AND mode != ''
                    GROUP BY mode
                    ORDER BY message_count DESC
                """, range_params)
                rows = cur.fetchall()

        return [
            {
                "mode": row[0],
                "message_count": row[1],
                "session_count": row[2],
                "avg_duration_ms": float(round(row[3], 1)) if row[3] else 0,
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[GetModeStats] 查询失败: {e}")
        return []


def get_daily_stats(days: int = 14, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """近 N 天每天的消息量和响应耗时趋势。"""
    try:
        base_sql, base_params = _range_clause(start_date, end_date)
        # 无外部范围时，退回默认近 N 天窗口
        if not base_sql:
            base_sql, base_params = (
                f" WHERE created_at >= NOW() - INTERVAL '{days} days'", [])
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        DATE(created_at)                                                AS day,
                        COUNT(*)                                                        AS message_count,
                        COUNT(*) FILTER (WHERE role = 'user')                           AS user_count,
                        ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL), 1) AS avg_duration_ms,
                        COALESCE(SUM(total_token), 0)                                   AS total_tokens
                    FROM {CONV_TABLE}
                    {base_sql}
                    GROUP BY DATE(created_at)
                    ORDER BY day ASC
                """, base_params)
                rows = cur.fetchall()

        return [
            {
                "date": row[0].isoformat() if row[0] else "",
                "message_count": row[1],
                "user_count": row[2],
                "avg_duration_ms": float(round(row[3], 1)) if row[3] else 0,
                "total_tokens": int(row[4] or 0),
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[GetDailyStats] 查询失败: {e}")
        return []


def get_trending_sessions(limit: int = 5, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """最活跃的会话 TOP N。"""
    try:
        range_sql, range_params = _range_clause(start_date, end_date)
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        session_id,
                        COUNT(*)                                    AS message_count,
                        MAX(turn_count)                             AS max_turns,
                        MIN(created_at)                             AS first_msg,
                        MAX(created_at)                             AS last_msg
                    FROM {CONV_TABLE}
                    {range_sql}
                    GROUP BY session_id
                    ORDER BY message_count DESC
                    LIMIT {limit}
                """, range_params)
                rows = cur.fetchall()

        return [
            {
                "session_id": row[0][:12] + "...",
                "message_count": row[1],
                "max_turns": row[2] or 0,
                "first_time": row[3].isoformat() if row[3] else "",
                "last_time": row[4].isoformat() if row[4] else "",
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[GetTrendingSessions] 查询失败: {e}")
        return []


def get_emotion_distribution(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """情绪分布（用于扇形图）。仅统计已打标的数据，排除 emotion 为空的记录。"""
    try:
        conds, range_params = _range_conds(start_date, end_date)
        conds.append("role = 'user'")
        conds.append("emotion IS NOT NULL AND emotion != ''")
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        emotion,
                        COUNT(*)                   AS count
                    FROM {CONV_TABLE}
                    WHERE {' AND '.join(conds)}
                    GROUP BY emotion
                    ORDER BY count DESC
                """, range_params)
                rows = cur.fetchall()
        return [{"name": row[0], "value": row[1]} for row in rows]
    except Exception as e:
        logger.error(f"[GetEmotionDistribution] 查询失败: {e}")
        return []


def get_tag_distribution(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """主题标签分布（用于条形图）。多标签按英文逗号拆开分别计数。"""
    try:
        conds, range_params = _range_conds(start_date, end_date)
        conds.append("role = 'user'")
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        TRIM(tag)                AS tag_name,
                        COUNT(*)                 AS count
                    FROM {CONV_TABLE},
                         LATERAL unnest(string_to_array(tags, ',')) AS tag
                    WHERE {' AND '.join(conds)} AND tags IS NOT NULL AND tags != ''
                    GROUP BY tag_name
                    ORDER BY count DESC
                """, range_params)
                rows = cur.fetchall()
        return [{"name": row[0], "value": row[1]} for row in rows]
    except Exception as e:
        logger.error(f"[GetTagDistribution] 查询失败: {e}")
        return []


def get_negative_trend(days: int = 14, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """按天统计消极/紧急情绪数量趋势（用于 Negative Trend 折线图）。"""
    try:
        base_sql, base_params = _range_clause(start_date, end_date)
        if not base_sql:
            base_sql, base_params = (
                f" WHERE created_at >= NOW() - INTERVAL '{days} days'", [])
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        DATE(created_at)                                              AS day,
                        COUNT(*) FILTER (WHERE role = 'user' AND emotion = '消极')    AS negative_count,
                        COUNT(*) FILTER (WHERE role = 'user' AND emotion = '紧急')    AS urgent_count,
                        COUNT(*) FILTER (WHERE role = 'user' AND emotion IN ('消极', '紧急')) AS total_negative
                    FROM {CONV_TABLE}
                    {base_sql}
                    GROUP BY DATE(created_at)
                    ORDER BY day ASC
                """, base_params)
                rows = cur.fetchall()
        return [
            {
                "date": row[0].isoformat() if row[0] else "",
                "negative_count": int(row[1] or 0),
                "urgent_count": int(row[2] or 0),
                "total_negative": int(row[3] or 0),
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[GetNegativeTrend] 查询失败: {e}")
        return []


def get_hourly_distribution(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """按小时分布（用于折线/柱状图）。"""
    try:
        range_sql, range_params = _range_clause(start_date, end_date)
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        EXTRACT(HOUR FROM created_at)::int AS hour,
                        COUNT(*)                           AS count
                    FROM {CONV_TABLE}
                    {range_sql}
                    GROUP BY hour
                    ORDER BY hour ASC
                """, range_params)
                rows = cur.fetchall()
        return [{"hour": row[0], "count": row[1]} for row in rows]
    except Exception as e:
        logger.error(f"[GetHourlyDistribution] 查询失败: {e}")
        return []


def get_duration_trend(days: int = 14, start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """每日平均耗时趋势（用于折线图）。"""
    try:
        base_sql, base_params = _range_clause(start_date, end_date)
        if not base_sql:
            base_sql, base_params = (
                f" WHERE created_at >= NOW() - INTERVAL '{days} days'", [])
        with _pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        DATE(created_at)                                         AS day,
                        ROUND(AVG(duration_ms) FILTER (WHERE duration_ms IS NOT NULL), 1) AS avg_ms
                    FROM {CONV_TABLE}
                    {base_sql}
                      AND duration_ms IS NOT NULL
                    GROUP BY DATE(created_at)
                    ORDER BY day ASC
                """, base_params)
                rows = cur.fetchall()
        return [
            {
                "date": row[0].isoformat() if row[0] else "",
                "avg_ms": float(row[1]) if row[1] else 0,
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"[GetDurationTrend] 查询失败: {e}")
        return []


# ================= API Router =================

router = APIRouter()


@router.get("/stats/overview")
async def stats_overview(start_date: str | None = Query(default=None),
                         end_date: str | None = Query(default=None),
                         _: TokenData = Depends(get_current_user)):
    """获取所有概览统计。start_date/end_date 可选，格式 YYYY-MM-DD，用于时间范围联动。"""
    logger.info(f"[API] 获取概览统计 range={start_date}~{end_date}")
    try:
        total_stats = get_total_stats(start_date=start_date, end_date=end_date)
        mode_stats = get_mode_stats(start_date=start_date, end_date=end_date)
        daily_stats = get_daily_stats(days=14, start_date=start_date, end_date=end_date)
        trending = get_trending_sessions(limit=5, start_date=start_date, end_date=end_date)
        emotion_dist = get_emotion_distribution(start_date=start_date, end_date=end_date)
        tag_dist = get_tag_distribution(start_date=start_date, end_date=end_date)
        hourly_dist = get_hourly_distribution(start_date=start_date, end_date=end_date)
        duration_trend = get_duration_trend(days=14, start_date=start_date, end_date=end_date)
        negative_trend = get_negative_trend(days=14, start_date=start_date, end_date=end_date)

        return JSONResponse(content={
            "success": True,
            "total_stats": total_stats,
            "mode_stats": mode_stats,
            "daily_stats": daily_stats,
            "trending_sessions": trending,
            "emotion_distribution": emotion_dist,
            "tag_distribution": tag_dist,
            "hourly_distribution": hourly_dist,
            "duration_trend": duration_trend,
            "negative_trend": negative_trend,
        })
    except Exception as e:
        logger.exception(f"[API] 概览统计失败: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )


# ================= 本地测试 =================

if __name__ == "__main__":
    import textwrap

    print("=" * 60)
    print("对话数据分析报告")
    print("=" * 60)

    total = get_total_stats()
    print(f"\n📊 基础统计:")
    print(f"  总会话数:     {total.get('total_sessions', 0)}")
    print(f"  总消息数:     {total.get('total_messages', 0)}")
    print(f"  用户消息:     {total.get('user_messages', 0)}")
    print(f"  助手消息:     {total.get('assistant_messages', 0)}")
    print(f"  平均耗时(ms): {total.get('avg_duration_ms', 0)}")
    print(f"  日均消息量:   {total.get('daily_avg_messages', 0)}")
    print(f"  7天活跃会话:  {total.get('active_sessions_7d', 0)}")
    print(f"  数据天数:     {total.get('days_active', 0)}")

    modes = get_mode_stats()
    print(f"\n📂 模式分布:")
    for m in modes:
        print(f"  {m['mode']:12s}  消息{m['message_count']:>6}条  会话{m['session_count']:>4}个  平均耗时{m['avg_duration_ms']:>8}ms")

    daily = get_daily_stats(days=7)
    print(f"\n📈 近7日消息趋势:")
    for d in daily:
        print(f"  {d['date']}  消息{d['message_count']:>4}条  用户{d['user_count']:>4}条  耗时{d['avg_duration_ms']:>8}ms")

    top = get_trending_sessions(limit=3)
    print(f"\n🏆 最活跃会话 TOP 3:")
    for t in top:
        print(f"  {t['session_id']}  消息{t['message_count']}条  轮次{t['max_turns']}")