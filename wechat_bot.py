"""
微信机器人模块 - 基于 iLink Bot API（ClawBot）
用于管网监测数据的智能分析与微信推送
支持多轮 MiMo 调用生成高质量分析 + 图表可视化
"""
import logging
import time
import threading
import traceback
import json
import os
import sqlite3
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from openai import OpenAI

import config
import config_secrets
import wechat_config
from ilink_client import ILINKClient

logger = logging.getLogger(__name__)

BJ_TZ = ZoneInfo('Asia/Shanghai')


def now_bj():
    return datetime.now(BJ_TZ)


def _log_message(sender, receiver, message, message_type='text', status='sent'):
    """记录消息到管理数据库"""
    try:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO message_logs (sender, receiver, message, message_type, status) VALUES (?, ?, ?, ?, ?)',
            (sender, receiver, message, message_type, status)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error("Log message error: %s", e)


def _ensure_user_exists(wechat_id):
    """确保用户存在于 users 表中，不存在则自动添加"""
    try:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin.db')
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 检查用户是否已存在
        cursor.execute('SELECT id FROM users WHERE wechat_id = ?', (wechat_id,))
        if cursor.fetchone():
            conn.close()
            return
        
        # 自动添加用户
        username = wechat_id.split('@')[0] if '@' in wechat_id else wechat_id
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, wechat_id) VALUES (?, ?, ?, ?)',
            (username, '', 'user', wechat_id)
        )
        conn.commit()
        conn.close()
        logger.info("Auto-added user: %s", wechat_id)
    except Exception as e:
        logger.error("Auto-add user error: %s", e)

ai_client = OpenAI(
    api_key=config_secrets.MIMO_API_KEY,
    base_url=config_secrets.MIMO_API_BASE,
)

DOMAIN_KNOWLEDGE = """
## 城西管网监测系统背景
- 区域：黄山市城西片区，雨污合流管网
- 设备类型：道路交汇口监测井（90台）、小区雨污水出口（18台）、截污管接入口（2台）
- 监测指标：
  - liquid_level：液位（单位：m），表示水面距井底的高度。正常范围 0.1-2.0m，>2.5m 为高风险
  - cod：化学需氧量（单位：mg/L），反映有机污染程度。生活污水正常 100-400mg/L，>500mg/L 为异常
  - ammonia_n：氨氮（单位：mg/L），反映生活污水浓度。正常 10-50mg/L，>80mg/L 为异常
  - voltage：设备电压（单位：V），正常 6-8V，<5V 需关注电池状态

## 行业标准参考
- 《城镇排水管渠与泵站运行、维护及安全技术规程》CJJ 68-2016
- 管网液位预警分级：
  - 蓝色预警：液位 > 2.0m（偏高）
  - 黄色预警：液位 > 2.5m（高风险）
  - 橙色预警：液位 > 3.0m（严重）
  - 红色预警：液位 > 3.5m 或距地面 < 0.5m（溢出风险）

## 液位突变判断标准
- 正常波动：< 0.1m/h
- 轻微突变：0.1-0.3m/h（关注）
- 明显突变：0.3-0.8m/h（预警）
- 严重突变：> 0.8m/h（紧急）
"""

AI_SYSTEM_PROMPT = """你是城西管网监测系统的高级智能分析助手。

## 核心原则：先找合理解释，再考虑异常
- 液位下降最常见原因是：雨停了、上游用水减少、泵站抽排正常 → 这些是**正常现象**，不要当成故障
- 液位上升最常见原因是：正在下雨、上游来水增加 → **除非超过2.5m或有溢流迹象**，否则是正常响应
- 只有在数据**明显违背物理规律**时（如雨天液位反而骤降、无降雨时液位骤升）才考虑设备故障
- **不要过度解读**：3个数据点不能证明故障趋势，5个以上才考虑

## 分析框架
1. **先看天气**：当前是否在下雨？雨停了多久？这是液位变化的首要因素
2. **再看液位**：变化幅度是否在正常范围内（降雨时+0.5~1.5m属正常）
3. **最后判断**：只有排除天气因素后仍有异常才标记为设备/管网问题

## 输出要求
- 简洁明了，**不要超过200字**
- 重点说"发生了什么"和"需要做什么"，不要长篇大论解释概念
- 如果是正常现象（如雨停水位降），直接说"属正常范围"即可
- 只有真正需要处理的问题才给出行动建议

{domain_knowledge}
"""

AI_DEVICE_PROMPT = """你是设备分析师，**只能分析用户指定的这一个设备**。

## 分析框架
1. **设备档案**：名称、类型、区域、绑定管点
2. **当前状态**：各指标当前值 + 阈值对比
3. **趋势分析**：近48h液位变化趋势（上升/稳定/下降）
4. **异常检测**：是否有突变、超标、长时间无数据
5. **风险评级**：该设备当前风险等级
6. **处理建议**：针对该设备的具体措施

## 输出要求
- 只讨论这一个设备，不提及其他设备
- 关键数据加粗，异常数据标⚠️
- 给出量化描述（如"液位 1.8m，比昨日均值高 0.3m"）
- 建议具体到操作步骤
- 不超过 300 字

{domain_knowledge}
"""

AI_TERMINOLOGY_PROMPT = """你是管网监测术语专家，用通俗易懂的语言解释专业术语。

解释要求：
1. 先给出一句话定义
2. 说明在管网监测中的意义
3. 举一个生活中的例子
4. 说明正常范围和异常情况

输出：简洁明了，不超过150字"""

AI_COMPARISON_PROMPT = """你是管网数据分析专家，对比分析多台设备的数据。

分析框架：
1. 列出对比维度（液位、COD、氨氮、电压）
2. 找出差异最大的指标
3. 分析差异原因
4. 给出综合评价

输出格式：
- 表格形式展示对比数据
- 标注差异显著的项目
- 给出排名和建议"""

AI_CHART_PROMPT = """你是管网数据分析专家。请根据以下数据特征，确定需要生成哪些图表来辅助分析。

可用图表类型：
- anomaly_overview: 异常类型分布（饼图+柱状图）
- system_overview: 系统概览仪表板（在线率+状态+区域分布）
- alert_priority: 告警优先级矩阵（严重度分布）
- device_trend: 设备液位趋势（单设备折线图）
- device_detailed: 设备多指标趋势（液位+COD+氨氮+电压）
- component_comparison: 连通分量水位对比

请用 JSON 格式回复，格式: {"charts": ["chart_type1", "chart_type2"], "reason": "选择原因"}"""

RAG_SYSTEM_PROMPT = """你是城西管网监测AI分析师。请基于以下原始数据做**简短**分析。

## 分析原则（重要！）
- **先看天气再下结论**：雨停了液位下降是正常现象，不是设备故障
- **液位变化的正常范围**：
  - 降雨中：液位上升0.5~1.5m属正常响应
  - 雨停后1~3小时：液位逐步回落属正常
  - 无降雨时：液位波动±0.2m以内属正常
- **只有以下情况才需要报警**：
  - 液位>2.5m且持续上涨（有溢流风险）
  - 雨天液位反而骤降（可能是设备故障或管道堵塞）
  - 无雨无泵操作时液位骤升（可能是上游来水异常）

## 输出要求
- **不超过150字**
- 如果情况正常，一句话说明即可（如"雨停后水位回落，属正常范围"）
- 只有真正需要处理的问题才展开说明
- 不要解释术语、不要科普、不要写报告格式

{domain_knowledge}
"""

_ilink_client: Optional[ILINKClient] = None
_data_cache_ref = None


def _get_cache():
    global _data_cache_ref
    return _data_cache_ref or {}


def set_data_cache(cache):
    global _data_cache_ref
    _data_cache_ref = cache


# ===== 数据收集 =====

def _collect_full_context():
    """收集完整的管网数据上下文"""
    cache = _get_cache()
    if not cache:
        return "暂无数据", {}

    lines = []
    stats = {}

    devices = cache.get('devices', [])
    bindings = cache.get('bindings', [])
    active = cache.get('active_bindings', [])
    nodes = cache.get('nodes', [])
    pipes = cache.get('pipes', [])
    report = cache.get('liquid_report', {})

    stats['device_count'] = len(devices)
    stats['active_count'] = len(active)
    stats['bound_count'] = len(bindings)

    lines.append("=== 基础信息 ===")
    lines.append("设备总数: %d" % len(devices))
    lines.append("已绑定: %d" % len(bindings))
    lines.append("活跃设备: %d" % len(active))
    lines.append("管点: %d, 管线: %d" % (len(nodes), len(pipes)))
    lines.append("最后更新: %s" % cache.get('last_update', '未知'))

    if report:
        lines.append("\n=== 液位连通器分析 ===")
        lines.append("异常总数: %d" % report.get('total', 0))
        lines.append("活跃设备: %d" % report.get('active_device_count', 0))
        lines.append("连通分量: %d" % report.get('component_count', 0))
        stats['anomaly_count'] = report.get('total', 0)

        for cat in ['consistency_anomalies', 'reversal_anomalies', 'frozen_anomalies',
                     'sudden_anomalies', 'rainfall_anomalies']:
            items = report.get(cat, [])
            if items:
                lines.append("\n【%s】(%d条)" % (cat.replace('_anomalies', ''), len(items)))
                for a in items[:8]:
                    sev = a.get('severity', '')
                    did = a.get('device_id', '')
                    reason = a.get('reason', a.get('description', ''))
                    lines.append("  [%s] %s: %s" % (sev, did, reason))

        comp_stats = report.get('component_stats', {})
        if comp_stats:
            lines.append("\n=== 连通分量统计 ===")
            for ci, comp in list(comp_stats.items())[:5]:
                lines.append("分量#%s: %d台, 均值%.2fm" % (
                    ci, comp.get('device_count', 0), comp.get('mean_water_level', 0)))

        rainfall = report.get('rainfall_periods', [])
        if rainfall:
            lines.append("\n=== 近期降雨 ===")
            for rp in rainfall[:3]:
                lines.append("%s ~ %s: %.1fmm" % (rp.get('start', ''), rp.get('end', ''), rp.get('total_mm', 0)))

    anomaly_result = cache.get('anomaly_result')
    if anomaly_result:
        lines.append("\n=== 其他异常检测 ===")
        summary = anomaly_result.get('summary', {})
        lines.append("阈值超标: %d" % summary.get('threshold_count', 0))
        lines.append("溢满风险: %d" % summary.get('overflow_count', 0))
        lines.append("管道充满度: %d" % summary.get('fullness_count', 0))
        lines.append("上下游异常: %d" % summary.get('updown_count', 0))

        overflow = anomaly_result.get('overflow', [])
        if overflow:
            lines.append("\n【溢满风险设备】")
            for o in overflow[:5]:
                lines.append("  %s: 距地面%.2fm [%s]" % (
                    o.get('device_id', ''), o.get('overflow_risk', 0), o.get('severity', '')))

        voltage = anomaly_result.get('voltage_status', {})
        low_voltage = {k: v for k, v in voltage.items() if v.get('severity') in ('medium', 'high')}
        if low_voltage:
            lines.append("\n【电压异常设备】(%d台)" % len(low_voltage))
            for did, v in list(low_voltage.items())[:5]:
                lines.append("  %s: %.2fV [%s]" % (did, v.get('min_voltage', 0), v.get('severity', '')))

    online_count = sum(1 for d in devices if d.get('isonline') == 1 or d.get('isonline') == '1')
    stats['online_count'] = online_count

    area_stats = {}
    for b in bindings:
        area = b.get('area_name', '未知')
        if area not in area_stats:
            area_stats[area] = {'device_count': 0, 'anomaly_count': 0}
        area_stats[area]['device_count'] += 1
    stats['area_stats'] = area_stats

    return '\n'.join(lines), stats


def _collect_device_context(device_id):
    """收集单个设备的详细上下文（含统计分析、天气关联、异常标记）"""
    import data_processor
    cache = _get_cache()
    lines = []

    binding = None
    for b in cache.get('bindings', []):
        if b['device_id'] == device_id:
            binding = b
            break

    if not binding:
        return "未找到设备 %s" % device_id

    bound_node = binding.get('bound_node', {})
    lines.append("=== 设备 %s 的数据 ===" % device_id)
    lines.append("（请仅分析此设备，不要扩展到其他设备）")
    lines.append("绑定管点: %s" % (bound_node.get('point_id', 'N/A') if bound_node else 'N/A'))
    lines.append("距离: %.1fm" % binding.get('distance', 0))
    lines.append("区域: %s" % binding.get('area_name', '未知'))

    if bound_node:
        lines.append("井底高程: %s m" % bound_node.get('well_bottom_elev', 'N/A'))
        lines.append("地面高程: %s m" % bound_node.get('ground_elev', 'N/A'))
        if bound_node.get('well_bottom_elev') is not None and bound_node.get('ground_elev') is not None:
            depth = bound_node['ground_elev'] - bound_node['well_bottom_elev']
            lines.append("井深: %.2f m" % depth)

    readings_48h = data_processor.get_device_readings(device_id, hours=48)
    readings_7d = data_processor.get_device_readings(device_id, hours=168)

    last_seen = binding.get('last_seen') or binding.get('first_seen', '')
    if readings_7d:
        last_recorded = readings_7d[0].get('recorded_at', '') if readings_7d else ''
    else:
        last_recorded = ''
    if last_recorded:
        try:
            last_dt = datetime.strptime(last_recorded, '%Y-%m-%d %H:%M:%S')
            now = now_bj()
            delta_hours = (now - last_dt).total_seconds() / 3600
            lines.append("最后上报: %s (%.0f小时前)" % (last_recorded, delta_hours))
            if delta_hours > 24:
                lines.append("⚠️ 设备已 %.0f 小时未上报数据" % delta_hours)
        except (ValueError, TypeError):
            lines.append("最后上报: %s" % last_recorded)

    stats = data_processor.get_device_statistics(device_id, hours=48)
    if stats:
        lines.append("\n=== 统计分析 ===")
        lines.append("当前液位: %sm %s" % (stats['current'], stats['status']))
        lines.append("48h均值: %sm, 标准差: %sm" % (stats['mean'], stats['std']))
        lines.append("变化趋势: %s (%.4fm/h)" % (stats['trend'], abs(stats['change_rate'])))
        lines.append("48h范围: %s ~ %sm (共%d条)" % (stats['min'], stats['max'], stats['count']))

    if readings_48h:
        lines.append("\n=== 近48h 读数 (%d条) ===" % len(readings_48h))
        liquid_levels = [float(r['liquid_level']) for r in readings_48h if r.get('liquid_level') is not None]
        if liquid_levels:
            lines.append("液位范围: %.2f ~ %.2f m" % (min(liquid_levels), max(liquid_levels)))
            lines.append("液位均值: %.2f m" % (sum(liquid_levels) / len(liquid_levels)))
            lines.append("液位波动: %.2f m" % (max(liquid_levels) - min(liquid_levels)))

        cod_vals = [float(r['cod']) for r in readings_48h if r.get('cod') is not None]
        if cod_vals:
            lines.append("COD范围: %.1f ~ %.1f mg/L" % (min(cod_vals), max(cod_vals)))

        nh_vals = [float(r['ammonia_n']) for r in readings_48h if r.get('ammonia_n') is not None]
        if nh_vals:
            lines.append("氨氮范围: %.2f ~ %.2f mg/L" % (min(nh_vals), max(nh_vals)))

        vol_vals = [float(r['voltage']) for r in readings_48h if r.get('voltage') is not None]
        if vol_vals:
            lines.append("电压范围: %.2f ~ %.2f V" % (min(vol_vals), max(vol_vals)))

        for r in readings_48h[:12]:
            lines.append("  %s: 液位%sm COD=%s NH3N=%s V=%s" % (
                r.get('recorded_at', ''),
                r.get('liquid_level', '-'),
                r.get('cod', '-'),
                r.get('ammonia_n', '-'),
                r.get('voltage', '-'),
            ))
    elif readings_7d:
        lines.append("\n=== 近48h 无数据，以下为近7天数据 (%d条) ===" % len(readings_7d))
        liquid_levels = [float(r['liquid_level']) for r in readings_7d if r.get('liquid_level') is not None]
        if liquid_levels:
            lines.append("液位范围: %.2f ~ %.2f m" % (min(liquid_levels), max(liquid_levels)))
            lines.append("液位均值: %.2f m" % (sum(liquid_levels) / len(liquid_levels)))
        for r in readings_7d[:12]:
            lines.append("  %s: 液位%sm COD=%s NH3N=%s V=%s" % (
                r.get('recorded_at', ''),
                r.get('liquid_level', '-'),
                r.get('cod', '-'),
                r.get('ammonia_n', '-'),
                r.get('voltage', '-'),
            ))
    else:
        lines.append("\n=== 无历史读数 ===")

    if bound_node and bound_node.get('latitude') and bound_node.get('longitude'):
        try:
            import weather_service
            lat = bound_node['latitude']
            lon = bound_node['longitude']
            summary = weather_service.get_recent_precipitation_summary(lat, lon, hours=24)
            if summary.get('total_24h', 0) > 0 or summary.get('total_7d', 0) > 0:
                lines.append("\n=== 近期天气 ===")
                lines.append("24h降雨量: %.1fmm" % summary.get('total_24h', 0))
                lines.append("7天降雨量: %.1fmm" % summary.get('total_7d', 0))
        except:
            pass

    report = cache.get('liquid_report', {})
    device_anomalies = []
    for cat in ['consistency_anomalies', 'reversal_anomalies', 'frozen_anomalies',
                 'sudden_anomalies', 'rainfall_anomalies']:
        for a in report.get(cat, []):
            if a.get('device_id') == device_id:
                device_anomalies.append(a)
    if device_anomalies:
        lines.append("\n=== 异常记录 ===")
        for a in device_anomalies:
            lines.append("  ⚠️ [%s] %s" % (a.get('severity', ''), a.get('reason', a.get('description', ''))))

    return '\n'.join(lines)


def _collect_rag_context(trigger_alerts=None, cache=None):
    """收集原始数据用于MiMo RAG分析（直接传原始数据，不经过预处理）"""
    cache = cache or _get_cache()
    if not cache:
        return "暂无数据"

    lines = []
    now_str = now_bj().strftime('%Y-%m-%d %H:%M')

    lines.append(f"=== 管网实时监测数据 ({now_str} CST) ===")

    devices = cache.get('devices', [])
    bindings = cache.get('bindings', [])

    lines.append(f"设备总数: {len(devices)}")
    lines.append(f"已绑定设备: {len(bindings)}")

    import data_processor

    if trigger_alerts:
        lines.append(f"\n=== 触发告警 ({len(trigger_alerts)}条) ===")
        for alert in trigger_alerts[:10]:
            alert_type = alert.get('type', '')
            title = alert.get('title', '')
            message = alert.get('message', '')
            lines.append(f"[{alert_type}] {title}")
            lines.append(f"  {message}")

    lines.append("\n=== 异常设备原始读数 ===")
    trigger_device_ids = set()
    if trigger_alerts:
        for alert in trigger_alerts:
            data = alert.get('data', {})
            if isinstance(data, list):
                for td in data:
                    trigger_device_ids.add(td.get('device_id', ''))
            elif isinstance(data, dict):
                trigger_device_ids.add(data.get('device_id', ''))

    target_devices = []
    for b in bindings:
        did = b.get('device_id', '')
        if did in trigger_device_ids:
            target_devices.append(did)
    if not target_devices:
        target_devices = [b.get('device_id', '') for b in bindings[:20]]

    for did in target_devices[:15]:
        readings = data_processor.get_device_readings(did, hours=6)
        if not readings:
            continue

        binding = next((b for b in bindings if b.get('device_id') == did), None)
        area = binding.get('area_name', '') if binding else ''
        node = binding.get('bound_node', {}) if binding else {}
        node_id = node.get('point_id', '') if node else ''
        ground_elev = node.get('ground_elev') if node else None
        well_bottom = node.get('well_bottom_elev') if node else None

        lines.append(f"\n设备: {did} | 区域: {area} | 管点: {node_id}")
        if ground_elev is not None and well_bottom is not None:
            lines.append(f"  地面高程: {ground_elev:.2f}m, 井底高程: {well_bottom:.2f}m")

        for r in readings[:6]:
            ts = r.get('recorded_at', '')
            ll = r.get('liquid_level', '-')
            cod = r.get('cod', '-')
            nh = r.get('ammonia_n', '-')
            v = r.get('voltage', '-')
            lines.append(f"  {ts}: 液位={ll}m COD={cod} 氨氮={nh} 电压={v}V")

    lines.append("\n=== 近期降雨数据 ===")
    try:
        sample_lat, sample_lon = 30.0, 118.0
        for d in devices:
            if d.get('latitude') and d.get('longitude'):
                sample_lat = d['latitude']
                sample_lon = d['longitude']
                break

        import weather_service
        summary = weather_service.get_recent_precipitation_summary(sample_lat, sample_lon, hours=24)
        lines.append(f"近24h降雨量: {summary.get('total_24h', 0):.1f}mm")
        lines.append(f"近7天降雨量: {summary.get('total_7d', 0):.1f}mm")

        forecast = weather_service.get_forecast_next_hours(sample_lat, sample_lon, hours=3)
        if forecast:
            lines.append("\n未来3小时天气预报:")
            for f in forecast[:4]:
                t_str = f.get('time', '').replace('T', ' ')[:16]
                p = f.get('precipitation', 0)
                lines.append(f"  {t_str}: 降雨{p:.1f}mm/h")
    except Exception as e:
        lines.append(f"天气数据获取失败: {e}")

    return '\n'.join(lines)

def _call_mimo(context, question, system_prompt=None, max_tokens=4096, temperature=0.5):
    try:
        prompt = (system_prompt or AI_SYSTEM_PROMPT).format(domain_knowledge=DOMAIN_KNOWLEDGE)
        response = ai_client.chat.completions.create(
            model=config_secrets.MIMO_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "管网数据:\n%s\n\n分析要求: %s" % (context, question)}
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error("MiMo error: %s", e)
        return None


# ===== Agent Tool Definitions =====

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_device_readings",
            "description": "获取指定设备的最近N条液位读数",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备ID，如 HSTX_TSPS_TX11"},
                    "hours": {"type": "integer", "description": "查询最近几小时，默认24"}
                },
                "required": ["device_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_device_info",
            "description": "获取设备基本信息（名称、区域、绑定管点、当前状态）",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备ID"}
                },
                "required": ["device_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取当前天气和降雨数据",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_devices_summary",
            "description": "获取所有设备的摘要列表（ID、液位、状态）",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_device_chart",
            "description": "生成设备液位趋势折线图并保存为图片",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string", "description": "设备ID"},
                    "hours": {"type": "integer", "description": "显示最近几小时，默认168"}
                },
                "required": ["device_id"]
            }
        }
    },
]


def _execute_tool(tool_name, tool_args):
    """执行 agent 工具调用"""
    import data_processor
    import json

    try:
        if tool_name == "get_device_readings":
            did = tool_args.get("device_id", "")
            hours = tool_args.get("hours", 24)
            readings = data_processor.get_device_readings(did, hours=hours)
            if not readings:
                return json.dumps({"error": f"设备 {did} 无读数数据"}, ensure_ascii=False)
            result = []
            for r in readings[:20]:
                result.append({
                    "time": str(r.get("recorded_at", ""))[:16],
                    "liquid_level": r.get("liquid_level"),
                    "cod": r.get("cod"),
                    "ammonia_n": r.get("ammonia_n"),
                    "voltage": r.get("voltage"),
                })
            return json.dumps({"device_id": did, "readings": result, "total": len(readings)}, ensure_ascii=False)

        elif tool_name == "get_device_info":
            did = tool_args.get("device_id", "")
            cache = _get_cache()
            bindings = cache.get("bindings", [])
            for b in bindings:
                if b.get("device_id") == did:
                    node = b.get("bound_node", {}) or {}
                    return json.dumps({
                        "device_id": did,
                        "name": b.get("name", did),
                        "area": b.get("area_name", ""),
                        "node_id": node.get("point_id", ""),
                        "ground_elev": node.get("ground_elev"),
                        "well_bottom_elev": node.get("well_bottom_elev"),
                        "bind_distance": b.get("distance"),
                    }, ensure_ascii=False)
            return json.dumps({"error": f"未找到设备 {did}"}, ensure_ascii=False)

        elif tool_name == "get_weather":
            try:
                import weather_service
                cache = _get_cache()
                devices = cache.get("devices", [])
                lat, lon = 29.7, 118.3
                for d in devices:
                    if d.get("latitude") and d.get("longitude"):
                        lat, lon = d["latitude"], d["longitude"]
                        break
                summary = weather_service.get_recent_precipitation_summary(lat, lon, hours=24)
                forecast = weather_service.get_forecast_next_hours(lat, lon, hours=3)
                return json.dumps({
                    "rainfall_24h_mm": summary.get("total_24h", 0),
                    "rainfall_7d_mm": summary.get("total_7d", 0),
                    "forecast": [{"time": f.get("time", ""), "precipitation": f.get("precipitation", 0)} for f in (forecast or [])[:4]]
                }, ensure_ascii=False)
            except Exception as e:
                return json.dumps({"error": str(e)}, ensure_ascii=False)

        elif tool_name == "get_all_devices_summary":
            cache = _get_cache()
            bindings = cache.get("bindings", [])
            result = []
            for b in bindings[:30]:
                did = b.get("device_id", "")
                readings = data_processor.get_device_readings(did, hours=6)
                level = readings[0].get("liquid_level") if readings else None
                result.append({
                    "device_id": did,
                    "area": b.get("area_name", ""),
                    "liquid_level": level,
                })
            return json.dumps({"devices": result, "total": len(bindings)}, ensure_ascii=False)

        elif tool_name == "generate_device_chart":
            did = tool_args.get("device_id", "")
            hours = tool_args.get("hours", 168)
            readings = data_processor.get_device_readings(did, hours=hours)
            if not readings:
                return json.dumps({"error": f"设备 {did} 无数据"}, ensure_ascii=False)
            chart_path = _generate_device_trend_chart(did, readings)
            if chart_path:
                return json.dumps({"chart_path": chart_path, "message": f"已生成 {did} 液位趋势图"}, ensure_ascii=False)
            return json.dumps({"error": "图表生成失败"}, ensure_ascii=False)

        return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


AGENT_SYSTEM_PROMPT = """你是城西管网监测系统的智能助手。你可以通过工具获取实时数据来回答用户问题。

## 回复原则
- **简洁直接**：用户问什么答什么，不要扩展无关内容
- **先用工具查数据**：不要凭空分析，先调用工具获取真实数据
- **正常现象直接说**：雨停水位降是正常的，不需要长篇分析
- **回复不超过150字**

## 工具使用建议
- 用户问某设备 → 先调 get_device_info，再调 get_device_readings
- 用户要折线图 → 调 generate_device_chart
- 用户问天气/降雨 → 调 get_weather
- 用户问整体情况 → 调 get_all_devices_summary

{domain_knowledge}
"""


def _run_agent(user_message, max_rounds=5):
    """运行 smolagents ToolCallingAgent"""
    try:
        from agent import run_agent
        result, charts = run_agent(user_message)
        logger.info("Agent result: %s, charts: %s", result[:100] if result else None, charts)
        return result, charts
    except Exception as e:
        logger.error("smolagents error: %s\n%s", e, traceback.format_exc())
        context, _ = _collect_full_context()
        analysis = _call_mimo(context, user_message)
        return analysis, []


def _call_mimo_json(context, question):
    try:
        response = ai_client.chat.completions.create(
            model=config_secrets.MIMO_MODEL,
            messages=[
                {"role": "system", "content": "你是数据分析助手，只用JSON格式回复，不要其他文字。"},
                {"role": "user", "content": "管网数据摘要:\n%s\n\n问题: %s" % (context, question)}
            ],
            max_tokens=4096,
            temperature=0.3,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error("MiMo JSON error: %s", e)
        return None


# ===== 图表生成 =====

def _generate_anomaly_chart(liquid_report, anomaly_result=None):
    try:
        import wechat_chart
        return wechat_chart.plot_anomaly_overview(liquid_report, anomaly_result)
    except Exception as e:
        logger.error("Chart generation error: %s", e)
        return None


def _generate_device_trend_chart(device_id, readings, anomalies=None):
    try:
        import wechat_chart
        return wechat_chart.plot_device_trend(device_id, readings, anomalies)
    except Exception as e:
        logger.error("Chart generation error: %s", e)
        return None


def _generate_device_detailed_chart(device_id, readings, anomalies=None):
    try:
        import wechat_chart
        return wechat_chart.plot_device_detailed_trend(device_id, readings, anomalies)
    except Exception as e:
        logger.error("Chart generation error: %s", e)
        return None


def _generate_system_overview_chart():
    try:
        import wechat_chart
        cache = _get_cache()
        devices = cache.get('devices', [])
        bindings = cache.get('bindings', [])
        active = cache.get('active_bindings', [])
        report = cache.get('liquid_report', {})
        anomaly_result = cache.get('anomaly_result', {})

        online_count = sum(1 for d in devices if d.get('isonline') == 1 or d.get('isonline') == '1')
        anomaly_count = report.get('total', 0)

        area_stats = {}
        for b in bindings:
            area = b.get('area_name', '未知')
            if area not in area_stats:
                area_stats[area] = {'device_count': 0, 'anomaly_count': 0}
            area_stats[area]['device_count'] += 1

        if anomaly_result:
            summary = anomaly_result.get('summary', {})
            anomaly_count = max(anomaly_count,
                                summary.get('threshold_count', 0) +
                                summary.get('overflow_count', 0))

        stats = {
            'device_count': len(devices),
            'active_count': len(active),
            'online_count': online_count,
            'anomaly_count': anomaly_count,
            'area_stats': area_stats,
        }
        return wechat_chart.plot_system_overview(stats)
    except Exception as e:
        logger.error("Chart generation error: %s", e)
        return None


def _generate_alert_priority_chart(anomalies):
    try:
        import wechat_chart
        return wechat_chart.plot_alert_priority(anomalies)
    except Exception as e:
        logger.error("Chart generation error: %s", e)
        return None


def _parse_chart_request(text):
    """从 MiMo JSON 回复中解析图表请求"""
    import json
    try:
        text = text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
        data = json.loads(text)
        return data.get('charts', [])
    except (json.JSONDecodeError, AttributeError):
        return []


# ===== 指令处理 =====

def _handle_report(from_user, context_token):
    """处理 /report 指令：多轮 MiMo 分析 + 图表"""
    context, stats = _collect_full_context()
    report = _get_cache().get('liquid_report', {})
    anomaly_result = _get_cache().get('anomaly_result')

    chart_paths = []

    system_overview = _generate_system_overview_chart()
    if system_overview:
        chart_paths.append(system_overview)

    anomaly_chart = _generate_anomaly_chart(report, anomaly_result)
    if anomaly_chart:
        chart_paths.append(anomaly_chart)

    analysis = _call_mimo(context, "请对当前管网液位连通器分析结果进行深度分析，识别关键异常和风险，给出建议。")
    if not analysis:
        analysis = "=== 液位分析报告 ===\n异常设备数: %d\n分析时间: %s" % (
            report.get('total', 0), now_bj().strftime('%Y-%m-%d %H:%M'))

    if _ilink_client:
        for cp in chart_paths:
            try:
                _ilink_client.send_image(from_user, cp, context_token)
                time.sleep(0.5)
            except Exception as e:
                logger.error("Send chart error: %s", e)
        _ilink_client.send_message(from_user, analysis, context_token)
        _log_message('bot', from_user, analysis, 'text', 'sent')


def _handle_report_screenshot(from_user, context_token):
    """处理报告请求：Chrome 截图 + matplotlib 降级"""
    logger.info("_handle_report_screenshot called")

    cache = _get_cache()
    report = cache.get('liquid_report', {})
    anomalies_count = report.get('total', 0)
    active = len(cache.get('active_bindings', []))
    now_str = now_bj().strftime('%Y-%m-%d %H:%M')

    # 1. 尝试 Chrome 截图（HTML 完整报告）
    screenshot_path = _capture_report_screenshot()

    # 2. 降级到 matplotlib 图表
    chart_paths = []
    if not screenshot_path:
        logger.info("Chrome screenshot failed, falling back to matplotlib")
        try:
            from wechat_chart import plot_report_summary
            report_chart = plot_report_summary(report)
            if report_chart:
                chart_paths.append(report_chart)
        except Exception as e:
            logger.error("Generate report chart error: %s", e)

    # 2. 构建文本消息
    lines = [f"📊 管网液位工程分析报告 | {now_str}", ""]
    lines.append(f"设备 {len(cache.get('devices', []))}台 | 活跃 {active}台 | 异常 {anomalies_count}条")

    sev_icon = {'critical': '🔴', 'high': '🔴', 'medium': '🟡', 'low': '🔵'}
    cat_icons = {'consistency_anomalies': '🔺', 'reversal_anomalies': '🔄',
                 'frozen_anomalies': '❄️', 'sudden_anomalies': '⚡',
                 'rainfall_anomalies': '🌧️'}
    cat_labels = {'consistency_anomalies': '水位偏离', 'reversal_anomalies': '流向异常',
                  'frozen_anomalies': '数据冻结', 'sudden_anomalies': '突变异常',
                  'rainfall_anomalies': '降雨异常'}
    anomaly_cats = ['consistency_anomalies', 'reversal_anomalies', 'frozen_anomalies',
                    'sudden_anomalies', 'rainfall_anomalies']

    for cat in anomaly_cats:
        items = report.get(cat, [])
        if not items:
            continue
        icon = cat_icons.get(cat, '•')
        lines.append(f"\n{icon} {cat_labels[cat]} ({len(items)}条)")
        lines.append("━" * 12)
        for a in items[:3]:
            sev = a.get('severity', '')
            did = a.get('device_id', '')
            icon = sev_icon.get(sev, '⚪')
            reason = a.get('reason', a.get('description', ''))
            if isinstance(reason, list):
                reason = str(reason)
            reason = str(reason).strip()
            parts = reason.split('，', 1)
            main_desc = parts[0][:80]
            sub_reason = parts[1].strip()[:60] if len(parts) > 1 else ''
            lines.append(f"{icon} {did}")
            lines.append(f"   {main_desc}")
            if sub_reason:
                lines.append(f"   → {sub_reason}")
            lines.append("")
        remaining = len(items) - 3
        if remaining > 0:
            lines.append(f"   ... 还有 {remaining} 条")

    lines.append(f"\n如需详情请告诉我设备编号。")
    summary = '\n'.join(lines)

    # 3. 发送消息
    logger.info("Sending report message to %s", from_user)
    if _ilink_client:
        # 发送截图或图表
        if screenshot_path:
            try:
                _ilink_client.send_image(from_user, screenshot_path, context_token)
                logger.info("Screenshot sent: %s", screenshot_path)
            except Exception as e:
                logger.error("Send screenshot error: %s", e)

        for chart_path in chart_paths:
            try:
                _ilink_client.send_image(from_user, chart_path, context_token)
                logger.info("Chart image sent: %s", chart_path)
            except Exception as e:
                logger.error("Send chart error: %s", e)

        # 发送文字报告
        result = _ilink_client.send_message(from_user, summary, context_token)
        logger.info("send_message result: %s", result)
        _log_message('bot', from_user, summary, 'text', 'sent')
    else:
        logger.warning("ilink_client not initialized")


def _handle_device(device_id, from_user, context_token):
    """处理 /device 指令：多轮 MiMo 分析 + 趋势图"""
    import data_processor

    context = _collect_device_context(device_id)
    if context.startswith("未找到"):
        if _ilink_client:
            _ilink_client.send_message(from_user, context, context_token)
            _log_message('bot', from_user, context, 'text', 'sent')
        return

    readings = data_processor.get_device_readings(device_id, hours=168)
    chart_paths = []

    detailed_chart = _generate_device_detailed_chart(device_id, readings)
    if detailed_chart:
        chart_paths.append(detailed_chart)
    else:
        trend_chart = _generate_device_trend_chart(device_id, readings)
        if trend_chart:
            chart_paths.append(trend_chart)

    analysis = _call_mimo(
        context,
        "分析设备 %s 的运行状态和液位趋势，给出评估和建议。只分析这一个设备。" % device_id,
        system_prompt=AI_DEVICE_PROMPT,
        max_tokens=1024,
        temperature=0.3
    )
    if not analysis:
        analysis = context

    if _ilink_client:
        for cp in chart_paths:
            try:
                _ilink_client.send_image(from_user, cp, context_token)
                time.sleep(0.5)
            except Exception as e:
                logger.error("Send chart error: %s", e)
        _ilink_client.send_message(from_user, analysis, context_token)
        _log_message('bot', from_user, analysis, 'text', 'sent')


def _handle_alerts(from_user, context_token):
    """处理 /alerts 指令：多轮 MiMo 分析 + 图表"""
    cache = _get_cache()
    report = cache.get('liquid_report', {})
    anomalies = report.get('anomalies', [])
    anomaly_result = cache.get('anomaly_result')

    if not anomalies and anomaly_result:
        all_anomalies = []
        for cat in ['threshold', 'overflow', 'upstream_downstream', 'mismatch']:
            all_anomalies.extend(anomaly_result.get(cat, []))
        anomalies = all_anomalies

    if not anomalies:
        if _ilink_client:
            _ilink_client.send_message(from_user, "当前无异常告警", context_token)
            _log_message('bot', from_user, "当前无异常告警", 'text', 'sent')
        return

    chart_paths = []

    priority_chart = _generate_alert_priority_chart(anomalies)
    if priority_chart:
        chart_paths.append(priority_chart)

    anomaly_chart = _generate_anomaly_chart(report, anomaly_result)
    if anomaly_chart:
        chart_paths.append(anomaly_chart)

    context_result = _collect_full_context()
    context = context_result[0] if isinstance(context_result, tuple) else context_result
    analysis = _call_mimo(context, "请分析当前告警情况，识别最关键的告警，分析原因和关联性，给出优先处理建议。")
    if not analysis:
        lines = ["=== 管网异常告警 (%d条) ===" % len(anomalies)]
        for a in anomalies[:15]:
            lines.append("[%s] %s: %s" % (a.get('severity', ''), a.get('device_id', ''), a.get('reason', a.get('description', ''))))
        analysis = '\n'.join(lines)

    if _ilink_client:
        for cp in chart_paths:
            try:
                _ilink_client.send_image(from_user, cp, context_token)
                time.sleep(0.5)
            except Exception as e:
                logger.error("Send chart error: %s", e)
        _ilink_client.send_message(from_user, analysis, context_token)
        _log_message('bot', from_user, analysis, 'text', 'sent')


def _classify_user_intent(text):
    """判断用户意图"""
    text_lower = text.lower()

    if any(kw in text for kw in ['/device', '查看设备', '设备详情', '设备状态', '展示设备', '折线图', '趋势图', '液位图']):
        return 'device_query'

    if any(kw in text for kw in ['什么是', '解释一下', '含义', '什么叫', '是什么意思']):
        return 'terminology'

    if any(kw in text for kw in ['日报', '周报', '报表', '月报', '报告', '分析报告', '巡检报告']):
        return 'report'

    if any(kw in text for kw in ['对比', '比较', '差异', '哪个更', '哪个高']):
        return 'comparison'

    if any(kw in text for kw in ['预测', '趋势', '未来', '明天', '下周']):
        return 'prediction'

    return 'general'


def _handle_ai_chat(text, from_user, context_token):
    """处理 AI 聊天（Agent 模式）"""
    import re
    intent = _classify_user_intent(text)
    logger.info("AI chat: text=%s, intent=%s", text[:50], intent)

    if intent == 'device_query':
        match = re.search(r'HSTX_\w+', text)
        if match:
            device_id = match.group(0)
            logger.info("Device query: %s", device_id)
            _handle_device(device_id, from_user, context_token)
            return

    if intent == 'terminology':
        analysis = _call_mimo(text, "请解释这个术语", system_prompt=AI_TERMINOLOGY_PROMPT, max_tokens=512)
        if _ilink_client:
            _ilink_client.send_message(from_user, analysis or "AI 服务暂时不可用", context_token)
            _log_message('bot', from_user, analysis or "AI 服务暂时不可用", 'text', 'sent')
        return

    if intent == 'report':
        _handle_report_screenshot(from_user, context_token)
        return

    # 使用 Agent 模式处理其他查询
    analysis, chart_paths = _run_agent(text)

    if not analysis:
        analysis = "AI 服务暂时不可用，请稍后重试"

    if _ilink_client:
        for cp in (chart_paths or []):
            try:
                _ilink_client.send_image(from_user, cp, context_token)
            except Exception as e:
                logger.error("Send chart error: %s", e)
        _ilink_client.send_message(from_user, analysis, context_token)
        _log_message('bot', from_user, analysis, 'text', 'sent')


def _handle_command(text, from_user, context_token):
    """处理指令"""
    cmd = text.strip().lower()
    if cmd == '/help':
        if _ilink_client:
            _ilink_client.send_message(from_user, HELP_TEXT, context_token)
            _log_message('bot', from_user, HELP_TEXT, 'text', 'sent')
    elif cmd == '/status':
        cache = _get_cache()
        lines = [
            "=== 管网监测系统状态 ===",
            "设备总数: %d" % len(cache.get('devices', [])),
            "活跃设备: %d" % len(cache.get('active_bindings', [])),
            "管点: %d" % len(cache.get('nodes', [])),
            "管线: %d" % len(cache.get('pipes', [])),
            "最后更新: %s" % cache.get('last_update', '未知'),
        ]
        if _ilink_client:
            _ilink_client.send_message(from_user, '\n'.join(lines), context_token)
            _log_message('bot', from_user, '\n'.join(lines), 'text', 'sent')
    elif cmd == '/report':
        _handle_report(from_user, context_token)
    elif cmd == '/alerts':
        _handle_alerts(from_user, context_token)
    elif cmd.startswith('/device '):
        device_id = text.split(' ', 1)[1].strip()
        _handle_device(device_id, from_user, context_token)
    elif cmd.startswith('/chart'):
        parts = cmd.split()
        if len(parts) > 1:
            _handle_device(parts[1], from_user, context_token)
        else:
            _handle_report(from_user, context_token)
    else:
        if _ilink_client:
            reply = "未知指令: %s\n输入 /help 查看可用指令" % text
            _ilink_client.send_message(from_user, reply, context_token)
            _log_message('bot', from_user, reply, 'text', 'sent')


HELP_TEXT = """=== 管网监测机器人指令 ===
/status - 查看系统状态
/report - AI 深度分析报告 + 图表
/alerts - 告警分析与处理建议
/device <ID> - 设备详情 + 趋势图
/chart <ID> - 设备液位趋势图
/help - 显示帮助

支持的媒体类型：
- 图片：AI 自动分析内容
- 语音：自动转文字处理
- 视频：保存并确认
- 文件：保存并解析内容

也可以直接发消息提问，AI 会结合管网数据深度分析回答"""


# ===== 消息处理 =====

def _on_message(msg):
    try:
        from_user = msg.get("from_user_id", "")
        context_token = msg.get("context_token", "")
        item_list = msg.get("item_list", [])
        if not item_list:
            return

        # 自动添加新用户
        _ensure_user_exists(from_user)

        # 分离文本和媒体消息
        text = ""
        media_items = []
        for item in item_list:
            item_type = item.get("type")
            if item_type == 1:
                text = item.get("text_item", {}).get("text", "")
            elif item_type in (2, 3, 4, 5):
                media_items.append(item)

        # 处理媒体消息
        if media_items:
            _handle_media_messages(media_items, from_user, context_token)

        # 处理文本消息
        if text:
            logger.info("Message from %s: %s", from_user, text[:50])
            _log_message(from_user, 'bot', text, 'text', 'received')
            if text.startswith('/'):
                _handle_command(text, from_user, context_token)
            else:
                _handle_ai_chat(text, from_user, context_token)

    except Exception as e:
        logger.error("Message handling error: %s\n%s", e, traceback.format_exc())


def _handle_media_messages(media_items, from_user, context_token):
    """处理媒体消息（图片、语音、视频、文件）"""
    try:
        from media_handler import process_image, process_voice, process_video, process_file
    except ImportError:
        logger.error("media_handler module not found")
        if _ilink_client:
            _ilink_client.send_message(from_user, "媒体处理模块未安装", context_token)
        return

    for item in media_items:
        item_type = item.get("type")

        try:
            if item_type == 2:  # 图片
                image_item = item.get("image_item", {})
                url = image_item.get("url", "")
                if url:
                    _log_message(from_user, 'bot', '[图片]', 'image', 'received')
                    result = process_image(url, from_user)
                    if result and _ilink_client:
                        analysis = result.get('analysis', '')
                        if analysis:
                            _ilink_client.send_message(from_user, f"图片已保存，AI分析结果：\n{analysis}", context_token)
                        else:
                            _ilink_client.send_message(from_user, "图片已保存", context_token)

            elif item_type == 3:  # 语音
                voice_item = item.get("voice_item", {})
                url = voice_item.get("url", "")
                duration = voice_item.get("duration", 0)
                if url:
                    _log_message(from_user, 'bot', f'[语音 {duration}秒]', 'voice', 'received')
                    result = process_voice(url, from_user, duration)
                    if result:
                        # 语音转文字后作为文本处理
                        logger.info("Voice to text: %s", result[:50])
                        _log_message(from_user, 'bot', result, 'voice_to_text', 'received')
                        if result.startswith('/'):
                            _handle_command(result, from_user, context_token)
                        else:
                            _handle_ai_chat(result, from_user, context_token)

            elif item_type == 4:  # 视频
                video_item = item.get("video_item", {})
                url = video_item.get("url", "")
                duration = video_item.get("duration", 0)
                if url:
                    _log_message(from_user, 'bot', f'[视频 {duration}秒]', 'video', 'received')
                    result = process_video(url, from_user, duration)
                    if result and _ilink_client:
                        _ilink_client.send_message(from_user, f"视频已保存（时长 {duration} 秒）", context_token)

            elif item_type == 5:  # 文件
                file_item = item.get("file_item", {})
                url = file_item.get("url", "")
                file_name = file_item.get("file_name", "unknown")
                file_size = file_item.get("file_size", 0)
                if url:
                    _log_message(from_user, 'bot', f'[文件: {file_name}]', 'file', 'received')
                    result = process_file(url, from_user, file_name, file_size)
                    if result and _ilink_client:
                        content = result.get('content', '')
                        if content:
                            _ilink_client.send_message(from_user, f"文件 {file_name} 已保存\n\n{content}", context_token)
                        else:
                            _ilink_client.send_message(from_user, f"文件 {file_name} 已保存", context_token)

        except Exception as e:
            logger.error("Handle media error (type=%d): %s", item_type, e)
            if _ilink_client:
                _ilink_client.send_message(from_user, "媒体处理出错，请稍后重试", context_token)


# ===== 启动/停止 =====

def start_wechat_bot(cache_ref):
    global _ilink_client, _data_cache_ref
    _data_cache_ref = cache_ref

    token = config_secrets.ILINK_TOKEN
    if not token:
        logger.warning("WeChat bot skipped: ILINK_TOKEN not set")
        return None

    base_url = getattr(config_secrets, 'ILINK_API_BASE', '')
    _ilink_client = ILINKClient(base_url, token)
    thread = _ilink_client.start_polling(_on_message)
    logger.info("WeChat bot started via iLink API")
    return thread


def push_alert_to_contacts(alert_text: str):
    if not _ilink_client:
        logger.warning("Bot not started, cannot push alert")
        return
    for user_id in wechat_config.ALERT_CONTACTS:
        try:
            _ilink_client.send_message(user_id, alert_text)
            logger.info("Alert sent to %s", user_id)
        except Exception as e:
            logger.error("Failed to send alert to %s: %s", user_id, e)


def push_analysis_report(trigger_alerts, cache=None):
    """推送MiMo RAG分析报告到微信（基于原始数据直接分析）"""
    if not _ilink_client:
        logger.warning("Bot not started, cannot push analysis report")
        return

    try:
        rag_context = _collect_rag_context(trigger_alerts=trigger_alerts, cache=cache)

        analysis = _call_mimo(
            rag_context,
            "请分析当前管网异常情况，识别关键风险，分析原因，给出处理建议。",
            system_prompt=RAG_SYSTEM_PROMPT
        )

        if not analysis:
            now_str = now_bj().strftime('%Y-%m-%d %H:%M')
            lines = [f"【管网监测报告】{now_str}"]
            for alert in trigger_alerts[:5]:
                lines.append(f"- {alert.get('title', '')}: {alert.get('message', '')[:100]}")
            analysis = '\n'.join(lines)

        now_str = now_bj().strftime('%Y-%m-%d %H:%M:%S')
        final_text = f"⏰ {now_str} CST\n\n{analysis}"

        for user_id in wechat_config.ALERT_CONTACTS:
            try:
                _ilink_client.send_message(user_id, final_text)
                logger.info("Analysis report sent to %s", user_id)
            except Exception as e:
                logger.error("Failed to send analysis report to %s: %s", user_id, e)

    except Exception as e:
        logger.error("Push analysis report error: %s", e)


def _capture_report_screenshot():
    """截取 /report 页面为 PNG（Chrome headless --screenshot + virtual-time-budget）"""
    try:
        import subprocess
        import time as _time

        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'charts')
        os.makedirs(output_dir, exist_ok=True)

        # Find Chrome executable
        chrome_paths = [
            r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
        ]
        chrome_exe = None
        for p in chrome_paths:
            if os.path.exists(p):
                chrome_exe = p
                break
        if not chrome_exe:
            logger.error("Chrome not found")
            return None

        cache_bust = int(_time.time() * 1000)
        app_base = getattr(config_secrets, 'APP_BASE_URL', 'http://127.0.0.1:5000')
        url = f'{app_base}/report?t={cache_bust}'
        filename = 'report_%s.png' % now_bj().strftime('%Y%m%d_%H%M%S')
        filepath = os.path.join(output_dir, filename)

        # --virtual-time-budget: give JS 60s to render charts before screenshot
        command = [
            chrome_exe,
            '--headless=new',
            '--disable-gpu',
            '--disable-web-security',
            '--no-sandbox',
            '--force-device-scale-factor=2',
            '--window-size=1400,5500',
            '--virtual-time-budget=60000',
            f'--screenshot={filepath}',
            url,
        ]

        logger.info("Running Chrome screenshot with virtual-time-budget=60s")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300
        )

        if os.path.exists(filepath):
            logger.info("Report screenshot captured: %s", filepath)
            return filepath

        logger.warning("Report screenshot failed. stderr: %s", result.stderr[:300])
        return None

    except Exception as e:
        logger.error("Capture report screenshot error: %s", e)
        return None


def _upload_to_supabase_storage(local_path, bucket='reports'):
    """上传文件到 Supabase Storage，返回公开 URL"""
    try:
        import requests
        supabase_url = os.getenv('SUPABASE_URL', '')
        supabase_key = os.getenv('SUPABASE_KEY', '')

        if not supabase_key:
            logger.warning("SUPABASE_KEY not set")
            return None

        filename = os.path.basename(local_path)
        # 使用时间戳避免重名
        storage_path = f"wechat/{now_bj().strftime('%Y%m%d_%H%M%S')}_{filename}"

        with open(local_path, 'rb') as f:
            file_data = f.read()

        url = f"{supabase_url}/storage/v1/object/{bucket}/{storage_path}"
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "image/png",
        }

        resp = requests.put(url, headers=headers, data=file_data, timeout=30)

        if resp.status_code in (200, 201):
            # 构造公开 URL
            public_url = f"{supabase_url}/storage/v1/object/public/{bucket}/{storage_path}"
            logger.info("Uploaded to Supabase: %s", public_url)
            return public_url
        else:
            logger.error("Supabase upload failed: %d %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.error("Upload to Supabase error: %s", e)
        return None


def _delete_supabase_file(url, bucket='reports'):
    """删除 Supabase Storage 中的文件"""
    try:
        import requests
        supabase_url = os.getenv('SUPABASE_URL', '')
        supabase_key = os.getenv('SUPABASE_KEY', '')

        if not supabase_key or not url:
            return

        # 从 URL 提取路径
        path = url.split(f'/object/public/{bucket}/')[-1]
        delete_url = f"{supabase_url}/storage/v1/object/{bucket}/{path}"
        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
        }

        requests.delete(delete_url, headers=headers, timeout=10)
    except Exception:
        pass


def push_hourly_report(cache=None):
    """推送管网液位巡检报告到微信（每小时定时调用）"""
    if not _ilink_client:
        logger.warning("Bot not started, cannot push hourly report")
        return

    try:
        import ai_report_generator
        import time
        cache = cache or _get_cache()
        if not cache:
            logger.warning("No data cache, skipping hourly report")
            return

        # 1. 生成工程建议报告
        logger.info("Generating engineering report...")
        ai_report_generator.generate_report(cache)

        # 2. 生成巡检报告
        logger.info("Generating hourly report...")
        report = ai_report_generator.generate_hourly_report(cache)
        if not report:
            logger.warning("Hourly report generation returned None")
            return

        # 3. 访问页面触发数据加载，等待 API 响应
        logger.info("Triggering page data load...")
        try:
            import requests
            app_base = getattr(config_secrets, 'APP_BASE_URL', 'http://127.0.0.1:5000')
            requests.get(f'{app_base}/api/hourly-report', timeout=10)
            requests.get(f'{app_base}/api/engineering-report', timeout=10)
        except Exception as e:
            logger.warning("Page data load trigger failed: %s", e)

        # 4. 截图并上传到 Supabase
        logger.info("Capturing screenshot...")
        screenshot_path = _capture_report_screenshot()
        image_url = None
        if screenshot_path:
            image_url = _upload_to_supabase_storage(screenshot_path)
            try:
                os.remove(screenshot_path)
            except Exception:
                pass

        # 6. 发送消息
        text = _format_hourly_report(report)
        now_str = now_bj().strftime('%Y-%m-%d %H:%M:%S')
        final_text = f"📊 管网巡检报告 | {now_str} CST\n\n{text}"

        if image_url:
            final_text += f"\n\n📷 完整报告截图（6小时内有效）:\n{image_url}"

        for user_id in wechat_config.ALERT_CONTACTS:
            try:
                _ilink_client.send_message(user_id, final_text)
                logger.info("Hourly report sent to %s", user_id)
            except Exception as e:
                logger.error("Failed to send hourly report to %s: %s", user_id, e)

    except Exception as e:
        logger.error("Push hourly report error: %s", e)


def _format_hourly_report(report):
    """将巡检报告 JSON 格式化为微信可读文本"""
    lines = []

    # 摘要
    summary = report.get('summary', {})
    status = summary.get('system_status', '未知')
    status_icon = {'正常': '🟢', '关注': '🟡', '异常': '🔴'}.get(status, '⚪')
    lines.append(f"系统状态: {status_icon} {status}")
    lines.append(f"活跃设备: {summary.get('active_devices', '-')} | "
                 f"异常数: {summary.get('total_anomalies', '-')} | "
                 f"降雨: {summary.get('rainfall_mm', 0)}mm")

    # 异常预警
    anomalies = report.get('anomaly_warnings', [])
    if anomalies:
        lines.append(f"\n⚠️ 异常预警 ({len(anomalies)}条)")
        severity_order = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
        sorted_anomalies = sorted(anomalies,
            key=lambda a: severity_order.get(a.get('severity', ''), 9))
        for a in sorted_anomalies[:8]:
            sev = a.get('severity', '')
            sev_label = {'critical': '严重', 'high': '高', 'medium': '中', 'low': '低'}.get(sev, sev)
            desc = a.get('description', '')
            if isinstance(desc, list):
                desc = str(desc)
            lines.append(f"  [{sev_label}] {a.get('device_id', '')}: {str(desc)[:80]}")

    # 建议
    recs = report.get('recommendations', {})
    emergency = recs.get('emergency', [])
    short_term = recs.get('short_term', [])
    long_term = recs.get('long_term', [])

    if emergency:
        lines.append(f"\n🔴 紧急处理")
        for r in emergency[:3]:
            lines.append(f"  • {str(r)[:80]}")
    if short_term:
        lines.append(f"\n🟡 短期建议")
        for r in short_term[:3]:
            lines.append(f"  • {str(r)[:80]}")
    if long_term:
        lines.append(f"\n🟢 长期规划")
        for r in long_term[:2]:
            lines.append(f"  • {str(r)[:80]}")

    if not anomalies and not emergency and not short_term and not long_term:
        lines.append("\n✅ 当前无异常，系统运行正常")

    return '\n'.join(lines)
