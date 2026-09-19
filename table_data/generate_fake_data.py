"""
伪数据生成脚本

向 hrssc_conversations_history 表中灌入 10 万条模拟对话数据，
用于测试数据报表页面的展示效果。

使用方式：
    python data/generate_fake_data.py [行数]

默认生成 100000 条，可指定：python data/generate_fake_data.py 50000
"""

import sys
import os
import random
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ==================== PG 连接 ====================
CONV_TABLE = os.getenv("PG_HRSSC_CONVERSATIONS_TABLE", "")


def _pg_conn():
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DATABASE"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


# ==================== 伪数据模板 ====================

# 用户问题模板（HR 业务相关）
USER_QUESTIONS = [
    # 入离职
    "我想办理离职手续，需要什么流程？",
    "入职需要带什么材料？",
    "转岗申请怎么提交？",
    "离职证明怎么开？",
    "试用期离职需要提前几天通知？",
    "入职体检有指定医院吗？",
    "离职后社保怎么处理？",
    "合同到期不续签有赔偿吗？",
    "入职培训什么时候安排？",
    "离职交接清单有哪些内容？",

    # 请假
    "我今年还有几天年假？",
    "事假需要提供证明吗？",
    "病假工资怎么算？",
    "婚假可以休多少天？",
    "产假从什么时候开始算？",
    "年假可以跨年结转吗？",
    "调休怎么申请？",
    "哺乳假每天多长时间？",
    "丧假有几天？",
    "请假审批多久能通过？",

    # 考勤
    "今天忘记打卡了怎么办？",
    "加班怎么计算工资？",
    "迟到扣款标准是什么？",
    "考勤记录怎么查询？",
    "弹性工作时间怎么规定的？",
    "出差算考勤吗？",
    "加班可以调休吗？",
    "矿工怎么定义的？",
    "考勤异常怎么申诉？",
    "外勤怎么打卡？",

    # 社保公积金
    "社保缴纳基数怎么算？",
    "公积金怎么提取？",
    "异地就医怎么报销？",
    "医保卡怎么办理？",
    "公积金贷款额度是多少？",
    "社保断缴有什么影响？",
    "补充医疗保险怎么用？",
    "养老金怎么计算？",
    "生育津贴怎么申请？",
    "公积金转移怎么办理？",

    # 工资报销
    "工资条怎么看？",
    "报销流程是什么？",
    "差旅费报销标准是多少？",
    "发票丢了还能报销吗？",
    "工资发放日是哪天？",
    "加班费怎么计算？",
    "报销多久能到账？",
    "餐补怎么发放？",
    "交通补贴标准是多少？",
    "年终奖什么时候发？",

    # 福利政策
    "今年体检什么时候安排？",
    "节日福利有哪些？",
    "商业保险包含什么项目？",
    "员工旅游怎么报名？",
    "生日福利怎么领取？",
    "高温补贴标准是多少？",
    "住房补贴怎么申请？",
    "子女教育补助有吗？",
    "心理健康咨询怎么预约？",
    "健身房补贴怎么申请？",

    # 购车
    "今年还有员工购车名额吗？",
    "MINI 员工购车折扣是多少？",
    "购车贷款怎么办理？",
    "员工购车需要什么条件？",
    "购车补贴多久到账？",
    "新能源车有员工优惠吗？",
    "二手车有员工福利吗？",
    "购车申请流程是什么？",
    "牌照问题公司能帮忙吗？",
    "员工购车可以选配置吗？",

    # 闲聊
    "你好呀",
    "今天天气真不错",
    "吃了吗？",
    "周末愉快",
    "谢谢你的帮助",
    "好的我知道了",
    "辛苦了",
    "明白了",
    "嗯嗯",
    "好的谢谢",

    # 情绪
    "这个问题都问了三遍了还没解决！",
    "你们到底有没有在认真处理？",
    "太让我失望了",
    "效率也太低了吧",
    "能不能快点处理？",
]

# AI 回复模板
AI_REPLIES = [
    "离职流程如下：1. 提交离职申请 2. 部门审批 3. 办理交接 4. 开具离职证明。具体可查看内网指南。",
    "入职需要携带：身份证原件、学历证书、银行卡、一寸照片2张。具体以HR通知为准。",
    "转岗申请请在HR系统提交，经原部门和目标部门审批通过后生效。",
    "离职证明可在办理离职手续时一并开具，也可后续联系HR补办。",
    "试用期离职提前3天通知即可，正式员工需提前30天书面通知。",
    "根据公司年假政策，您当年度的年假天数根据入职年限计算，具体请登录HR系统查询。",
    "事假原则上需要提供相关证明材料，3天以上需部门经理审批。",
    "病假工资按当地最低工资标准的80%发放，需提供医院诊断证明。",
    "婚假可享受13天（含周末），需在结婚登记后1年内休完。",
    "根据规定，您可以在HR系统中查看剩余年假天数。",
    "忘记打卡可在系统内提交补卡申请，每月补卡次数有限制。",
    "加班工资计算方式：工作日1.5倍，休息日2倍，法定节假日3倍。",
    "社保缴纳基数按上年度月平均工资确定，每年7月调整一次。",
    "公积金提取可通过当地公积金管理中心APP办理，或咨询HR协助。",
    "报销流程：填写报销单 → 部门审批 → 财务审核 → 打款。一般在7个工作日内到账。",
    "员工购车项目每年开放一次，具体时间请关注公司通知。",
    "MINI 员工购车可享受额外折扣，具体折扣幅度与车型相关。",
    "请假审批一般1-2个工作日完成，紧急情况可联系部门经理加急处理。",
    "抱歉给您带来不便，我已记录您的问题并反馈给相关负责人跟进处理。",
    "很高兴为您服务！如有其他问题欢迎随时咨询。",
    "不客气，祝您工作愉快！",
    "我理解您的情况，已为您转接人工客服，请稍候。",
    "目前系统显示您的信息正常，建议重新登录后查看。",
]

# 来源数据（用于 sources 字段）
SOURCE_FILES = [
    "员工手册2026.pdf",
    "考勤管理制度.docx",
    "薪酬福利政策.pdf",
    "社保公积金指南.pdf",
    "员工购车项目管理规定.pdf",
    "请假管理规定.docx",
    "入职离职流程手册.pdf",
    "商业保险说明.pdf",
    "差旅报销标准.pdf",
    "绩效考核制度.docx",
]

def _random_mode() -> str:
    return random.choices(
        ["rag", "chat", "emotion"],
        weights=[75, 22, 3],
    )[0]


def _random_duration(mode: str) -> int:
    if mode == "rag":
        return random.randint(800, 5000)
    elif mode == "chat":
        return random.randint(500, 3000)
    else:
        return 0


def _random_sources() -> str | None:
    if random.random() > 0.6:
        return None
    n = random.randint(1, 3)
    selected = random.sample(SOURCE_FILES, n)
    return json.dumps([{"file_name": f, "content": f"内容摘要..."} for f in selected], ensure_ascii=False)


# 主题标签池
TAGS_POOL = [
    "入离职", "请假", "考勤", "社保公积金", "工资报销",
    "福利政策", "购车", "晋升奖惩", "保密竞业", "劳动法规",
    "办公规范", "闲聊", "情绪反馈", "其他",
]

# 情绪标签池
EMOTIONS_POOL = ["积极", "中性", "消极", "紧急"]


def _random_tags() -> str:
    """随机返回 1~2 个主题标签。"""
    n = random.randint(1, 2)
    return "，".join(random.sample(TAGS_POOL, n))


def _random_emotion() -> str:
    """随机返回一个情绪标签。"""
    return random.choice(EMOTIONS_POOL)


def _random_content(role: str) -> str:
    if role == "user":
        return random.choice(USER_QUESTIONS)
    else:
        return random.choice(AI_REPLIES)


# ==================== 批量插入 ====================

BATCH_SIZE = 2000  # 每批插入行数


def generate(total_rows: int):
    """生成伪数据并插入 PG。"""
    print(f"🚀 开始生成 {total_rows} 条伪数据...")
    start = time.time()

    # 预生成一批 session_id（少量会话，大量重复，便于聚合）
    session_count = max(1, total_rows // 50)  # 平均每个会话 50 条消息
    session_ids = [str(uuid4()) for _ in range(session_count)]

    # 时间范围：2026年6月20日 ~ 6月30日
    time_start = datetime(2026, 6, 20, tzinfo=timezone.utc)
    time_end = datetime(2026, 6, 30, tzinfo=timezone.utc)
    time_span = (time_end - time_start).total_seconds()

    conn = _pg_conn()
    rows_inserted = 0

    try:
        with conn:
            while rows_inserted < total_rows:
                batch_remaining = min(BATCH_SIZE, total_rows - rows_inserted)
                batch_data = []

                # 为每批生成一组会话
                batch_session_count = max(1, batch_remaining // 15)
                batch_sessions = random.sample(session_ids, min(batch_session_count, len(session_ids)))

                for sid in batch_sessions:
                    # 每个会话生成 2~20 轮对话
                    turns = random.randint(2, 20)
                    # 在 6月20日~30日之间随机选一个起始时间
                    offset_seconds = random.uniform(0, time_span)
                    base_time = time_start + timedelta(seconds=offset_seconds)

                    for t in range(1, turns + 1):
                        if rows_inserted >= total_rows:
                            break
                        ts = base_time + timedelta(seconds=random.randint(30, 600) * t)

                        mode = _random_mode()
                        duration = _random_duration(mode) if mode == "rag" else None
                        sources = _random_sources() if mode == "rag" and random.random() > 0.4 else None

                        # 用户消息（带标签）
                        batch_data.append((
                            "anonymous", sid, t, "user",
                            _random_content("user"),
                            _random_tags(), _random_emotion(),
                            sources, mode, duration, ts,
                        ))
                        rows_inserted += 1
                        if rows_inserted >= total_rows:
                            break

                        # AI 回复（带耗时，无标签）
                        ts_ai = ts + timedelta(seconds=random.randint(1, 10))
                        ai_duration = random.randint(800, 5000)
                        batch_data.append((
                            "anonymous", sid, t, "assistant",
                            _random_content("assistant"),
                            None, None,
                            None, mode, ai_duration, ts_ai,
                        ))
                        rows_inserted += 1
                        if rows_inserted >= total_rows:
                            break

                if not batch_data:
                    break

                # 批量插入
                with conn.cursor() as cur:
                    for row in batch_data:
                        cur.execute(
                            f"""
                            INSERT INTO {CONV_TABLE}
                                (uid, session_id, turn_count, role, content,
                                 tags, emotion, sources, mode, duration_ms, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            row,
                        )
                conn.commit()

                progress = min(rows_inserted, total_rows)
                elapsed = time.time() - start
                rate = progress / elapsed if elapsed > 0 else 0
                print(f"  ✅ 已插入 {progress}/{total_rows} 条 ({rate:.0f} 条/秒)")

    except Exception as e:
        print(f"❌ 插入失败: {e}")
        raise
    finally:
        conn.close()

    elapsed = time.time() - start
    print(f"\n🎉 完成！共插入 {rows_inserted} 条数据，耗时 {elapsed:.1f} 秒")


# ==================== 执行 ====================
if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100000
    generate(n)