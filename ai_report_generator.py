"""
AI 给排水工程建议报告生成器
- 收集设备异常数据、管点拓扑、降雨信息
- 调用 MiMo AI 生成专业工程建议
- 缓存到 data_cache，每小时自动更新
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta

import config
import liquid_analysis
import config_secrets

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是城西片区给排水管网工程专家。根据监测数据生成专业的工程建议报告。

## 参数含义（务必在报告中向用户解释）
- 液位(liquid_level): 管道内水面距管底的高度(m)，正常范围0.1-2.0m
- 充满度(fullness): 液位/管径×100%，>80%为高风险，>50%需关注
- 水力坡降(slope): 管道坡度(%)，负值为顺坡(正常排水方向)，正值为逆坡(可能淤堵)
- 水位标高(water_level): 井底高程+液位，连通器原理下同组设备应接近
- 水位偏差(water_level_diff): 同组设备水位标高差异
- 降雨响应: 降雨后水位恢复时间，>6h恢复缓慢说明排水不畅

## 重要规则
- 只基于提供的数据分析，绝对不要编造数据
- 如果某项数据为空或不可用，明确说明"数据不可用"，不要猜测
- 不要编造降雨事件、设备异常或任何未在数据中体现的信息
- 拓扑上下文(topology_context)已由系统预计算，标注为"正常"的设备严禁在报告中定性为"异常"或"淤堵"
- is_terrain_anomaly=false 的设备是地形因素导致的水位差异，属正常现象
- 你必须引用系统计算好的 topology_context 来解释水位成因，不要自行判断拓扑关系
- ground_proximity 数据中的 gap_to_ground 值：负值=已溢出，<0.3=即将溢出，<0.5=高风险

## 输出要求
严格JSON格式，包含三个级别。每条建议包含:
- title: 简洁标题
- detail: 详细说明（含参数含义解释和具体数据）
- devices: 相关设备ID列表
- priority: high/medium/low

JSON结构:
{
  "emergency": [{"title": "...", "detail": "...", "devices": ["..."], "priority": "high"}],
  "short_term": [{"title": "...", "detail": "...", "devices": ["..."], "priority": "medium"}],
  "long_term": [{"title": "...", "detail": "...", "devices": [], "priority": "low"}]
}

如果没有某级别事项，返回空数组[]。"""


def _fallback_rainfall_from_sqlite():
    """直接从 SQLite 查询降雨数据作为回退"""
    try:
        import os
        db_path = getattr(config, 'DB_PATH', None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pipe_device.db')
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        cutoff = (datetime.utcnow() - timedelta(hours=72)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
            SELECT recorded_at, rainfall_mm FROM weather_data
            WHERE rainfall_mm IS NOT NULL AND rainfall_mm > 0
            AND recorded_at >= ?
            ORDER BY recorded_at
        """, (cutoff,))
        rows = cursor.fetchall()
        conn.close()

        rainfall_hours = set()
        hourly_rain = {}
        for r in rows:
            t_str = str(r[0]).replace('T', ' ')[:13]
            mm = float(r[1] or 0)
            if mm > 0.1:
                rainfall_hours.add(t_str)
                hourly_rain[t_str] = hourly_rain.get(t_str, 0) + mm

        sorted_hours = sorted(rainfall_hours)
        periods = []
        if sorted_hours:
            start = sorted_hours[0]
            end = sorted_hours[0]
            total = hourly_rain.get(sorted_hours[0], 0)
            for h in sorted_hours[1:]:
                if h <= end:
                    total += hourly_rain.get(h, 0)
                    end = h
                else:
                    if total > 1.0:
                        periods.append((start, end, total))
                    start = h
                    end = h
                    total = hourly_rain.get(h, 0)
            if total > 1.0:
                periods.append((start, end, total))

        logger.info("[AI-REPORT] SQLite rainfall fallback: %d hours, %d periods", len(rainfall_hours), len(periods))
        return rainfall_hours, periods
    except Exception as e:
        logger.error("[AI-REPORT] SQLite rainfall fallback failed: %s", e)
        return set(), []


def _build_component_topology(component_devices, node_map):
    """构建连通组拓扑信息，供 AI 判断地形差异"""
    topology = {}
    for ci, devs in component_devices.items():
        if len(devs) < 2:
            continue
        dev_list = []
        for b in devs:
            node = node_map.get(b.get('bound_node', {}).get('point_id', ''), {})
            dev_list.append({
                'device_id': b['device_id'],
                'node_id': b.get('bound_node', {}).get('point_id', ''),
                'well_bottom_elev': node.get('well_bottom_elev'),
                'ground_elev': node.get('ground_elev'),
            })
        elevations = [d['well_bottom_elev'] for d in dev_list if d['well_bottom_elev'] is not None]
        if elevations:
            mean_elev = sum(elevations) / len(elevations)
            for d in dev_list:
                if d['well_bottom_elev'] is not None:
                    d['elev_diff_from_mean'] = round(d['well_bottom_elev'] - mean_elev, 2)
        topology[f"component_{ci}"] = {
            'device_count': len(dev_list),
            'devices': dev_list,
            'min_elev': min(elevations) if elevations else None,
            'max_elev': max(elevations) if elevations else None,
            'elev_range': round(max(elevations) - min(elevations), 2) if elevations else None,
        }
    return topology


def collect_report_data(cache):
    """从 data_cache 收集报告所需数据"""
    bindings = cache.get('bindings', [])
    pipes = cache.get('pipes', [])
    node_map = cache.get('node_map', {})

    active_bindings, _ = liquid_analysis.get_active_devices(bindings, hours=48)
    adjacency, components = liquid_analysis.build_pipe_graph(pipes, node_map)
    component_devices, _ = liquid_analysis.assign_devices_to_components(active_bindings, node_map, components)

    consistency_anomalies, component_stats = liquid_analysis.analyze_component_water_levels(component_devices, node_map)

    # 使用 RainfallRepository 统一获取降雨数据
    from repositories.rainfall_repo import RainfallRepository
    rainfall_hours, rainfall_periods = RainfallRepository.get_recent_periods(hours=72)
    logger.info("[AI-REPORT] Rainfall: %d hours, %d periods", len(rainfall_hours), len(rainfall_periods))

    rainfall_anomalies = liquid_analysis.analyze_rainfall_response(active_bindings, pipes, node_map, rainfall_hours)
    sudden_anomalies = liquid_analysis.detect_level_sudden_change(bindings, hours=24)
    frozen_anomalies = liquid_analysis.detect_frozen_data(active_bindings, hours=48)

    # 预计算拓扑上下文（含 is_terrain_anomaly 标签）
    component_topology = liquid_analysis.build_topology_context(component_devices, node_map)

    # 水位接近地面高程预警
    ground_proximity_raw = liquid_analysis.calculate_ground_proximity(bindings, hours=24)
    ground_proximity = liquid_analysis.classify_ground_proximity(ground_proximity_raw)

    device_info = []
    for b in active_bindings:
        did = b['device_id']
        node = b.get('bound_node') or {}
        readings = liquid_analysis.get_device_recent_readings(did, hours=48, limit=5)
        latest = readings[0] if readings else {}
        device_info.append({
            'device_id': did,
            'name': b.get('name', did),
            'area': b.get('area_name', ''),
            'type': b.get('device_type', ''),
            'lon': b.get('longitude'),
            'lat': b.get('latitude'),
            'node_id': node.get('point_id', ''),
            'ground_elev': node.get('ground_elev'),
            'well_bottom_elev': node.get('well_bottom_elev'),
            'latest_level': latest.get('liquid_level'),
            'latest_time': latest.get('recorded_at', ''),
        })

    # 为每个设备附加拓扑上下文标签
    topology_map = {}
    for group in component_topology.values():
        for d in group.get('devices', []):
            topology_map[d['device_id']] = d

    for item in consistency_anomalies:
        topo = topology_map.get(item['device_id'], {})
        item['topology_context'] = topo.get('topology_context', '')
        item['is_terrain_anomaly'] = topo.get('is_terrain_anomaly')

    return {
        'anomaly_summary': {
            'consistency': [{'device_id': a['device_id'], 'water_level_diff': round(a.get('water_level_diff', 0), 2),
                             'severity': a.get('severity', ''), 'reason': a.get('reason', ''),
                             'topology_context': a.get('topology_context', ''),
                             'is_terrain_anomaly': a.get('is_terrain_anomaly')}
                            for a in consistency_anomalies],
            'rainfall': [{'device_id': a['device_id'], 'type': a.get('type', ''),
                          'reason': a.get('reason', '')} for a in rainfall_anomalies],
            'sudden': [{'device_id': a['device_id'], 'reason': a.get('reason', '')} for a in sudden_anomalies],
            'frozen': [{'device_id': a['device_id'], 'reason': a.get('reason', '')} for a in frozen_anomalies],
            'ground_proximity': [{'device_id': a['device_id'], 'gap_to_ground': a['gap_to_ground'],
                                  'severity': a['severity'], 'label': a['label'],
                                  'water_level': a['water_level'], 'ground_elev': a['ground_elev']}
                                 for a in ground_proximity if a['severity'] != 'low'],
        },
        'device_info': device_info[:30],
        'component_topology': component_topology,
        'rainfall_data': {
            'periods': [(str(s), str(e), round(t, 1)) for s, e, t in rainfall_periods[:5]],
            'recent_hours': len(rainfall_hours),
            'total_rainfall_mm': round(sum(t for _, _, t in rainfall_periods), 1) if rainfall_periods else 0,
        },
        'component_count': len(components),
        'active_device_count': len(active_bindings),
        'total_anomalies': len(consistency_anomalies) + len(rainfall_anomalies) + len(sudden_anomalies) + len(frozen_anomalies),
    }


def call_mimo_ai(data):
    """调用 MiMo API 生成工程建议"""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=config_secrets.MIMO_API_KEY,
            base_url=config_secrets.MIMO_API_BASE,
        )
        user_msg = json.dumps(data, ensure_ascii=False, indent=2)
        response = client.chat.completions.create(
            model=config_secrets.MIMO_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
            max_tokens=2000,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            import re
            fixed = re.sub(r'(\{|,)\s*(\w+)\s*:', r'\1"\2":', content)
            return json.loads(fixed)
    except Exception as e:
        logger.error("MiMo API call failed: %s", e)
        return None


def generate_report(cache):
    """生成 AI 工程建议报告并缓存"""
    logger.info("[AI-REPORT] Collecting data...")
    data = collect_report_data(cache)

    logger.info("[AI-REPORT] Calling MiMo AI (active=%d, anomalies=%d)...",
                data['active_device_count'], data['total_anomalies'])
    report = call_mimo_ai(data)

    if report:
        report['generated_at'] = datetime.now().isoformat()
        report['active_device_count'] = data['active_device_count']
        report['total_anomalies'] = data['total_anomalies']
        cache['ai_engineering_report'] = report
        logger.info("[AI-REPORT] Generated: %d emergency, %d short-term, %d long-term",
                     len(report.get('emergency', [])),
                     len(report.get('short_term', [])),
                     len(report.get('long_term', [])))
    else:
        logger.warning("[AI-REPORT] MiMo API failed, using fallback rules")
        cache['ai_engineering_report'] = _fallback_report(data)

    return cache.get('ai_engineering_report')


def _fallback_report(data):
    """MiMo API 不可用时的硬编码 fallback"""
    anomalies = data['anomaly_summary']
    emergency, short_term, long_term = [], [], []

    high_bias = [a for a in anomalies['consistency'] if abs(a.get('water_level_diff', 0)) > 1.5]
    if high_bias:
        devs = list(set(a['device_id'] for a in high_bias))
        emergency.append({
            'title': '水位严重偏离',
            'detail': f"{', '.join(devs)} 水位偏差 > 1.5m，建议立即安排CCTV检测确认管网状况",
            'devices': devs, 'priority': 'high'
        })

    overflow = [a for a in anomalies['rainfall'] if a.get('type') == 'rainfall_overflow_risk']
    if overflow:
        devs = list(set(a['device_id'] for a in overflow))
        emergency.append({
            'title': '雨水倒灌风险',
            'detail': f"{', '.join(devs)} 降雨期间水位超过地面高程，立即检查防倒灌设施",
            'devices': devs, 'priority': 'high'
        })

    medium_bias = [a for a in anomalies['consistency'] if 0.8 < abs(a.get('water_level_diff', 0)) <= 1.5]
    if medium_bias:
        devs = list(set(a['device_id'] for a in medium_bias))
        short_term.append({
            'title': '中等级偏差设备',
            'detail': f"{', '.join(devs)} 水位偏差 0.8~1.5m，计划内安排管道检测",
            'devices': devs, 'priority': 'medium'
        })

    if anomalies['frozen']:
        devs = [a['device_id'] for a in anomalies['frozen']]
        short_term.append({
            'title': '数据冻结',
            'detail': f"{', '.join(devs)} 液位长期不变，需现场检修传感器",
            'devices': devs, 'priority': 'medium'
        })

    if anomalies['sudden']:
        devs = list(set(a['device_id'] for a in anomalies['sudden']))
        short_term.append({
            'title': '液位突变',
            'detail': f"{', '.join(devs)} 存在突升/突降，需排除传感器故障",
            'devices': devs, 'priority': 'medium'
        })

    long_term = [
        {'title': '建立降雨-水位响应模型', 'detail': '预测强降雨期间的重点防范区域', 'devices': [], 'priority': 'low'},
        {'title': '管网修复或改造', 'detail': '对连通器原理检测出的水位长期不一致管段进行修复', 'devices': [], 'priority': 'low'},
        {'title': '增加监测密度', 'detail': '在低洼/易涝点位增加液位监测设备', 'devices': [], 'priority': 'low'},
        {'title': '管道清淤计划', 'detail': '重点优先处理排水不畅的管段', 'devices': [], 'priority': 'low'},
    ]

    return {
        'emergency': emergency,
        'short_term': short_term,
        'long_term': long_term,
        'generated_at': datetime.now().isoformat(),
        'active_device_count': data['active_device_count'],
        'total_anomalies': data['total_anomalies'],
        'is_fallback': True,
    }


HOURLY_REPORT_PROMPT = """你是城西片区给排水管网工程专家。根据以下实时监测数据，生成一份完整的管网巡检报告。

## 参数含义
- 液位(liquid_level): 管道内水面距管底的高度(m)，正常范围0.1-2.0m
- 充满度(fullness): 液位/管径×100%，>80%为高风险
- 水位标高(water_level): 井底高程+液位
- 水位偏差(water_level_diff): 同组设备水位标高差异
- gap_to_ground: 地面高程-水位标高，负值=已溢出，<0.3=即将溢出

## 重要规则
- 只基于提供的数据分析，绝对不要编造数据
- 拓扑上下文(topology_context)已由系统预计算，标注为"正常"的设备严禁定性为"异常"
- ground_proximity 数据中的 gap_to_ground 值是计算好的，直接引用

## 输出格式
严格JSON:
{
  "summary": {
    "report_time": "YYYY-MM-DD HH:MM",
    "active_devices": N,
    "total_anomalies": N,
    "rainfall_mm": N,
    "system_status": "正常/关注/异常"
  },
  "device_status": [
    {"device_id": "...", "node_id": "...", "liquid_level": N, "water_level": N, "ground_elev": N, "gap_to_ground": N, "fullness": N, "status": "正常/关注/异常", "note": "..."}
  ],
  "component_analysis": [
    {"component_id": N, "device_count": N, "mean_water_level": N, "elev_range": N, "topology_note": "...", "devices": ["..."]}
  ],
  "anomaly_warnings": [
    {"device_id": "...", "type": "水位偏差/降雨响应/突变/冻结/地面接近", "severity": "critical/high/medium", "description": "..."}
  ],
  "recommendations": {
    "emergency": [{"title": "...", "detail": "...", "devices": ["..."]}],
    "short_term": [{"title": "...", "detail": "...", "devices": ["..."]}],
    "long_term": [{"title": "...", "detail": "...", "devices": []}]
  }
}

如果没有某类别数据，返回空数组[]。"""


def call_mimo_hourly(data):
    """调用 MiMo API 生成完整巡检报告"""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=config_secrets.MIMO_API_KEY,
            base_url=config_secrets.MIMO_API_BASE,
        )
        user_msg = json.dumps(data, ensure_ascii=False, indent=2)
        response = client.chat.completions.create(
            model=config_secrets.MIMO_MODEL,
            messages=[
                {"role": "system", "content": HOURLY_REPORT_PROMPT},
                {"role": "user", "content": user_msg}
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Try to fix common JSON issues (missing quotes around keys)
            import re
            fixed = re.sub(r'(\{|,)\s*(\w+)\s*:', r'\1"\2":', content)
            return json.loads(fixed)
    except Exception as e:
        logger.error("MiMo hourly report API call failed: %s", e)
        return None


def generate_hourly_report(cache):
    """每小时生成完整巡检报告"""
    logger.info("[AI-HOURLY] Collecting data...")
    data = collect_report_data(cache)

    logger.info("[AI-HOURLY] Calling MiMo AI (active=%d, anomalies=%d)...",
                data['active_device_count'], data['total_anomalies'])
    report = call_mimo_hourly(data)

    if report:
        report['generated_at'] = datetime.now().isoformat()
        report['active_device_count'] = data['active_device_count']
        report['total_anomalies'] = data['total_anomalies']
        cache['ai_hourly_report'] = report
        logger.info("[AI-HOURLY] Generated successfully")
    else:
        logger.warning("[AI-HOURLY] MiMo API failed, using fallback")
        cache['ai_hourly_report'] = _fallback_hourly_report(data)

    return cache.get('ai_hourly_report')


def _fallback_hourly_report(data):
    """MiMo API 不可用时的 fallback 报告"""
    anomalies = data['anomaly_summary']
    device_info = data.get('device_status', data.get('device_info', []))
    ground_prox = anomalies.get('ground_proximity', [])

    device_status = []
    for d in device_info[:30]:
        gap = next((g['gap_to_ground'] for g in ground_prox if g['device_id'] == d['device_id']), None)
        status = '正常'
        if gap is not None:
            if gap < 0.3: status = '异常'
            elif gap < 1.0: status = '关注'
        device_status.append({
            'device_id': d['device_id'],
            'node_id': d.get('node_id', ''),
            'liquid_level': d.get('latest_level'),
            'ground_elev': d.get('ground_elev'),
            'gap_to_ground': gap,
            'status': status,
        })

    warnings = []
    for a in anomalies.get('consistency', []):
        if a.get('is_terrain_anomaly') is False:
            continue
        warnings.append({
            'device_id': a['device_id'],
            'type': '水位偏差',
            'severity': a.get('severity', 'medium'),
            'description': a.get('reason', ''),
        })
    for g in ground_prox:
        if g.get('severity') in ('critical', 'high'):
            warnings.append({
                'device_id': g['device_id'],
                'type': '地面接近',
                'severity': g['severity'],
                'description': f"水位距地面{g['gap_to_ground']}m",
            })

    return {
        'summary': {
            'report_time': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'active_devices': data['active_device_count'],
            'total_anomalies': data['total_anomalies'],
            'rainfall_mm': data.get('rainfall_data', {}).get('total_rainfall_mm', 0),
            'system_status': '异常' if any(w['severity'] == 'critical' for w in warnings) else '关注' if warnings else '正常',
        },
        'device_status': device_status,
        'component_analysis': [],
        'anomaly_warnings': warnings,
        'recommendations': {
            'emergency': [{'title': w['description'], 'detail': w['description'], 'devices': [w['device_id']]}
                          for w in warnings if w['severity'] == 'critical'][:3],
            'short_term': [{'title': w['description'], 'detail': w['description'], 'devices': [w['device_id']]}
                           for w in warnings if w['severity'] == 'high'][:5],
            'long_term': [{'title': '建立降雨-水位响应模型', 'detail': '预测强降雨期间的重点防范区域', 'devices': []}],
        },
        'generated_at': datetime.now().isoformat(),
        'active_device_count': data['active_device_count'],
        'total_anomalies': data['total_anomalies'],
        'is_fallback': True,
    }
