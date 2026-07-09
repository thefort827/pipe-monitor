"""
微信机器人配置 - iLink Bot API（ClawBot）
"""
import os

# ===== 消息推送目标联系人 =====
# 微信用户ID列表（从收到的消息中获取 from_user_id）
ALERT_CONTACTS = os.getenv("ALERT_CONTACTS", "").split(",") if os.getenv("ALERT_CONTACTS") else []

# ===== 分析报告定时推送时间（24小时制） =====
REPORT_SCHEDULE_HOURS = [8, 18]

# ===== 告警检测间隔（秒） =====
ALERT_CHECK_INTERVAL = 300
