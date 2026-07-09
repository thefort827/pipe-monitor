"""
液位数据分析模块 - 基于连通器原理（连通器原则）

核心逻辑：
1. 通过管线拓扑构建管网连通图
2. 找出所有连通分量（子管网）
3. 对每个连通分量内的活跃设备，计算水位绝对标高 = 井底高程 + 液位
4. 在连通分量内比较各设备水位一致性
5. 检测异常：水位偏离组内均值、违背连通器原理（上游<下游）、数据冻结等
6. 聚焦降雨期分析：降雨前-中-后的水位变化
"""
import os
import sqlite3
import math
from collections import defaultdict
from datetime import datetime, timedelta
import config
import data_processor
from data_processor import haversine_distance


def get_active_devices(bindings, hours=48):
    """获取活跃设备：有液位读数的设备"""
    import requests as _requests
    active_ids = set()
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()

        if config.DB_TYPE == 'postgresql':
            cursor.execute("SELECT MAX(recorded_at) FROM readings")
        else:
            cursor.execute("SELECT MAX(recorded_at) FROM readings")
        max_recorded = cursor.fetchone()[0]

        if max_recorded:
            if config.DB_TYPE == 'postgresql':
                cursor.execute(f"""
                    SELECT DISTINCT device_id
                    FROM readings
                    WHERE liquid_level IS NOT NULL
                    AND recorded_at >= %s::timestamp - INTERVAL '{hours} hours'
                """, (max_recorded,))
            else:
                cursor.execute("""
                    SELECT DISTINCT device_id
                    FROM readings
                    WHERE liquid_level IS NOT NULL
                    AND recorded_at >= datetime(?, ? || ' hours')
                """, (max_recorded, f'-{hours}'))
            active_ids = set(row[0] for row in cursor.fetchall())
        conn.close()
    except Exception:
        try:
            REST_URL = os.environ.get('SUPABASE_URL', '')
            REST_KEY = os.environ.get('SUPABASE_KEY', '')
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            r = _requests.get(f'{REST_URL}/rest/v1/readings',
                params={'select': 'device_id', 'liquid_level': 'not.is.null',
                        'recorded_at': f'gte.{cutoff}'},
                headers={'apikey': REST_KEY, 'Authorization': f'Bearer {REST_KEY}'},
                timeout=30, verify=False)
            rows = r.json()
            active_ids = set(row['device_id'] for row in rows)
        except Exception:
            pass

    active_bindings = []
    inactive_bindings = []
    for b in bindings:
        if b['device_id'] in active_ids and b.get('bound_node'):
            active_bindings.append(b)
        else:
            inactive_bindings.append(b)
    return active_bindings, inactive_bindings


def build_pipe_graph(pipes, node_map):
    """构建管线图结构，找出连通分量"""
    adjacency = defaultdict(list)
    for pipe in pipes:
        s = pipe['start_id']
        e = pipe['end_id']
        if s in node_map and e in node_map:
            adjacency[s].append((e, pipe))
            adjacency[e].append((s, pipe))

    visited = set()
    components = []
    for node_id in adjacency:
        if node_id in visited:
            continue
        queue = [node_id]
        component = set()
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            component.add(cur)
            for neighbor, _ in adjacency[cur]:
                if neighbor not in visited:
                    queue.append(neighbor)
        components.append(component)
    return adjacency, components


def assign_devices_to_components(active_bindings, node_map, components):
    """将活跃设备分配到对应的连通分量"""
    node_to_component = {}
    for ci, comp in enumerate(components):
        for node_id in comp:
            node_to_component[node_id] = ci

    component_devices = defaultdict(list)
    unconnected_devices = []
    for b in active_bindings:
        node = b['bound_node']
        node_id = node['point_id']
        if node_id in node_to_component:
            ci = node_to_component[node_id]
            component_devices[ci].append(b)
        else:
            unconnected_devices.append(b)
    return component_devices, unconnected_devices


_kcgis_instance = None

def _get_kcgis_instance():
    """获取共享的KCGIService实例，避免每次调用都创建新连接"""
    global _kcgis_instance
    if _kcgis_instance is None:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        from kcgis_service import KCGIService
        KCGIS_TOKEN = os.getenv('KCGIS_TOKEN', '')
        _kcgis_instance = KCGIService(token=KCGIS_TOKEN)
    return _kcgis_instance


def get_device_recent_readings(device_id, hours=72, limit=50):
    """获取设备最近N小时的液位读数（优先从本地数据库，无数据时调KCGIS API）"""
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()
        time_cond = hours_ago(hours)
        if config.DB_TYPE == 'postgresql':
            cursor.execute(f"""
                SELECT recorded_at, liquid_level, cod, ammonia_n, voltage, isonline
                FROM readings
                WHERE device_id = %s
                AND recorded_at >= {time_cond}
                ORDER BY recorded_at DESC
                LIMIT %s
            """, (device_id, limit))
        else:
            cursor.execute("""
                SELECT recorded_at, liquid_level, cod, ammonia_n, voltage, isonline
                FROM readings
                WHERE device_id = ?
                AND recorded_at >= datetime('now', ? || ' hours', 'localtime')
                ORDER BY recorded_at DESC
                LIMIT ?
            """, (device_id, f'-{hours}', limit))
        rows = fetch_all(cursor, conn)
        conn.close()
        if rows:
            return rows
    except Exception:
        pass

    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        from kcgis_service import KCGIService
        KCGIS_TOKEN = os.getenv('KCGIS_TOKEN', '')
        kcgis = _get_kcgis_instance()
        readings = kcgis.get_device_history_data(device_id, hours=hours)
        return readings[:limit] if readings else []
    except Exception as e:
        print(f"Failed to get readings for {device_id}: {e}")
        return []


def analyze_component_water_levels(component_devices, node_map):
    """
    对每个连通分量内的设备进行水位一致性分析（连通器原理）
    """
    anomalies = []
    component_stats = {}

    for ci, devices in component_devices.items():
        if len(devices) < 2:
            continue

        all_water_levels = []
        device_data = {}

        for b in devices:
            did = b['device_id']
            node = b['bound_node']
            well_bottom = node.get('well_bottom_elev')
            ground_elev = node.get('ground_elev')
            if well_bottom is None:
                continue
            readings = get_device_recent_readings(did, hours=72, limit=30)
            if not readings:
                continue
            device_data[did] = {'binding': b, 'node': node, 'well_bottom_elev': well_bottom, 'ground_elev': ground_elev, 'readings': readings}
            for r in readings:
                level = r.get('liquid_level')
                if level is None:
                    continue
                water_level = well_bottom + level
                all_water_levels.append({'device_id': did, 'water_level': water_level, 'liquid_level': level, 'recorded_at': r['recorded_at'], 'ground_elev': ground_elev, 'well_bottom_elev': well_bottom})
            import time
            time.sleep(0.3)

        if len(device_data) < 2:
            continue

        time_buckets = defaultdict(list)
        for wl in all_water_levels:
            try:
                dt = datetime.fromisoformat(wl['recorded_at'])
                bucket_key = dt.strftime('%Y-%m-%d %H:00:00')
                time_buckets[bucket_key].append(wl)
            except (ValueError, TypeError):
                continue

        for bucket_key, bucket_data in time_buckets.items():
            if len(set(d['device_id'] for d in bucket_data)) < 2:
                continue

            water_levels_val = [d['water_level'] for d in bucket_data]
            mean_wl = sum(water_levels_val) / len(water_levels_val)
            variance = sum((wl - mean_wl) ** 2 for wl in water_levels_val) / len(water_levels_val)
            std_wl = math.sqrt(variance) if variance > 0 else 0

            device_bucket_avg = defaultdict(list)
            for d in bucket_data:
                device_bucket_avg[d['device_id']].append(d['water_level'])

            for did, wls in device_bucket_avg.items():
                dev_avg_wl = sum(wls) / len(wls)
                deviation = (dev_avg_wl - mean_wl) / std_wl if std_wl > 0.01 else 0

                dev_info = device_data[did]
                ground_elev = dev_info['ground_elev']
                node_id = dev_info['node']['point_id']

                is_statistical_anomaly = abs(dev_avg_wl - mean_wl) > 0.3 and abs(deviation) > 1.5
                is_absolute_anomaly_high = (dev_avg_wl - mean_wl) > 0.8
                is_absolute_anomaly_low = (mean_wl - dev_avg_wl) > 0.8

                if is_statistical_anomaly or is_absolute_anomaly_high or is_absolute_anomaly_low:
                    anomaly = {
                        'device_id': did, 'node_id': node_id, 'recorded_at': bucket_key,
                        'type': 'water_level_inconsistency',
                        'severity': 'high' if (abs(deviation) > 2.5 or abs(dev_avg_wl - mean_wl) > 1.0) else 'medium',
                        'component_index': ci, 'avg_water_level': round(dev_avg_wl, 3),
                        'component_mean_water_level': round(mean_wl, 3),
                        'water_level_diff': round(dev_avg_wl - mean_wl, 3),
                        'std_deviation': round(std_wl, 3), 'deviation_sigma': round(deviation, 2),
                        'well_bottom_elev': dev_info['well_bottom_elev'], 'ground_elev': ground_elev,
                        'liquid_level': round(sum(d['liquid_level'] for d in bucket_data if d['device_id'] == did) / len([d for d in bucket_data if d['device_id'] == did]), 3),
                        'device_count_in_component': len(device_data), 'reason': ''
                    }
                    if dev_avg_wl > mean_wl:
                        anomaly['direction'] = '偏高'
                        anomaly['reason'] = f"设备水位({dev_avg_wl:.2f}m)高于连通组均值({mean_wl:.2f}m)偏差{dev_avg_wl - mean_wl:.2f}m，可能上游来水增大或设备异常"
                    else:
                        anomaly['direction'] = '偏低'
                        anomaly['reason'] = f"设备水位({dev_avg_wl:.2f}m)低于连通组均值({mean_wl:.2f}m)偏差{mean_wl - dev_avg_wl:.2f}m，可能管道渗漏或设备堵塞/故障"
                    if ground_elev and dev_avg_wl > ground_elev:
                        anomaly['severity'] = 'critical'
                        anomaly['reason'] += f"，水位已超过地面高程({ground_elev:.2f}m)，存在溢出风险！"
                    anomalies.append(anomaly)

        device_avg_water_levels = {}
        for did, info in device_data.items():
            wls = [r['liquid_level'] for r in info['readings'] if r.get('liquid_level') is not None]
            if wls:
                device_avg_water_levels[did] = {
                    'avg_water_level': round(info['well_bottom_elev'] + sum(wls) / len(wls), 3),
                    'avg_liquid_level': round(sum(wls) / len(wls), 3),
                    'sample_count': len(wls), 'node_id': info['node']['point_id'],
                    'ground_elev': info['ground_elev'], 'well_bottom_elev': info['well_bottom_elev']
                }
        if device_avg_water_levels:
            component_stats[ci] = {'device_count': len(device_avg_water_levels), 'devices': device_avg_water_levels}

    return anomalies, component_stats


def detect_water_reversal(bindings, pipes, node_map):
    """检测上下游水位倒置异常（违背连通器原理）"""
    anomalies = []
    device_to_node = {}
    for b in bindings:
        if b.get('bound_node'):
            device_to_node[b['device_id']] = b['bound_node']

    device_readings = {}
    for did in device_to_node:
        readings = get_device_recent_readings(did, hours=72, limit=20)
        if readings:
            device_readings[did] = readings

    for pipe in pipes:
        start_id, end_id = pipe['start_id'], pipe['end_id']
        start_node = node_map.get(start_id)
        end_node = node_map.get(end_id)
        if not start_node or not end_node:
            continue
        start_elev = start_node.get('well_bottom_elev')
        end_elev = end_node.get('well_bottom_elev')
        if start_elev is None or end_elev is None:
            continue

        devices_at_start = [did for did, n in device_to_node.items() if n['point_id'] == start_id]
        devices_at_end = [did for did, n in device_to_node.items() if n['point_id'] == end_id]

        for d_up in devices_at_start:
            for d_down in devices_at_end:
                up_readings = device_readings.get(d_up, [])
                down_readings = device_readings.get(d_down, [])
                if not up_readings or not down_readings:
                    continue
                for u_row in up_readings[:10]:
                    best_match, best_diff = None, float('inf')
                    for d_row in down_readings:
                        try:
                            ut = datetime.fromisoformat(u_row['recorded_at'])
                            dt = datetime.fromisoformat(d_row['recorded_at'])
                            diff = abs((ut - dt).total_seconds())
                            if diff < best_diff:
                                best_diff = diff
                                best_match = d_row
                        except: continue
                    if not best_match or best_diff > 3600:
                        continue
                    up_level = u_row.get('liquid_level')
                    down_level = best_match.get('liquid_level')
                    if up_level is None or down_level is None:
                        continue
                    up_water = start_elev + up_level
                    down_water = end_elev + down_level
                    if start_elev > end_elev and up_water < down_water - 0.15:
                        anomalies.append({
                            'device_id': d_up, 'paired_device_id': d_down, 'node_id': start_id,
                            'recorded_at': u_row['recorded_at'], 'type': 'water_reversal', 'severity': 'high',
                            'pipe_info': f"{start_id} -> {end_id}",
                            'start_elev': round(start_elev, 2), 'end_elev': round(end_elev, 2),
                            'up_water_level': round(up_water, 2), 'down_water_level': round(down_water, 2),
                            'water_diff': round(up_water - down_water, 2),
                            'up_liquid_level': round(up_level, 3), 'down_liquid_level': round(down_level, 3),
                            'reason': f"上游({start_id})水位({up_water:.2f}m)低于下游({end_id})水位({down_water:.2f}m)，上下游落差{down_water - up_water:.2f}m，违背连通器原理，可能下游严重堵塞"
                        })
    return anomalies


def detect_frozen_data(bindings, hours=72):
    """检测数据冻结异常"""
    import requests as _requests
    anomalies = []
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()
        time_cond = hours_ago(hours)
        for b in bindings:
            if not b.get('bound_node'):
                continue
            did = b['device_id']
            if config.DB_TYPE == 'postgresql':
                cursor.execute(f"""
                    SELECT recorded_at, liquid_level FROM readings
                    WHERE device_id = %s AND liquid_level IS NOT NULL
                    AND recorded_at >= {time_cond}
                    ORDER BY recorded_at DESC LIMIT 15
                """, (did,))
            else:
                cursor.execute("""
                    SELECT recorded_at, liquid_level FROM readings
                    WHERE device_id = ? AND liquid_level IS NOT NULL
                    AND recorded_at >= datetime('now', ? || ' hours', 'localtime')
                    ORDER BY recorded_at DESC LIMIT 15
                """, (did, f'-{hours}'))
            rows = fetch_all(cursor, conn)
            if len(rows) < 5:
                continue
            unique_levels = set(round(r['liquid_level'], 4) for r in rows)
            if len(unique_levels) <= 1:
                anomalies.append({
                    'device_id': did, 'node_id': b['bound_node']['point_id'],
                    'recorded_at': rows[0]['recorded_at'], 'type': 'frozen_data', 'severity': 'medium',
                    'frozen_value': round(rows[0]['liquid_level'], 4), 'sample_count': len(rows),
                    'reason': f"设备数据冻结：连续{len(rows)}个读数的液位值均为{round(rows[0]['liquid_level'], 4)}m"
                })
        conn.close()
    except Exception:
        try:
            REST_URL = os.environ.get('SUPABASE_URL', '')
            REST_KEY = os.environ.get('SUPABASE_KEY', '')
            hdrs = {'apikey': REST_KEY, 'Authorization': f'Bearer {REST_KEY}'}
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            for b in bindings:
                if not b.get('bound_node'):
                    continue
                did = b['device_id']
                r = _requests.get(f'{REST_URL}/rest/v1/readings',
                    params={'select': 'recorded_at,liquid_level', 'device_id': f'eq.{did}',
                            'liquid_level': 'not.is.null', 'recorded_at': f'gte.{cutoff}',
                            'order': 'recorded_at.desc', 'limit': 15},
                    headers=hdrs, timeout=30, verify=False)
                rows = r.json()
                if len(rows) < 5:
                    continue
                unique_levels = set(round(row['liquid_level'], 4) for row in rows)
                if len(unique_levels) <= 1:
                    anomalies.append({
                        'device_id': did, 'node_id': b['bound_node']['point_id'],
                        'recorded_at': rows[0]['recorded_at'], 'type': 'frozen_data', 'severity': 'medium',
                        'frozen_value': round(rows[0]['liquid_level'], 4), 'sample_count': len(rows),
                        'reason': f"设备数据冻结：连续{len(rows)}个读数的液位值均为{round(rows[0]['liquid_level'], 4)}m"
                    })
        except Exception:
            pass
    return anomalies


def detect_level_sudden_change(bindings, hours=72):
    """检测液位突变异常"""
    import requests as _requests
    anomalies = []
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()
        time_cond = hours_ago(hours)
        for b in bindings:
            if not b.get('bound_node'):
                continue
            did = b['device_id']
            if config.DB_TYPE == 'postgresql':
                cursor.execute(f"""
                    SELECT recorded_at, liquid_level FROM readings
                    WHERE device_id = %s AND liquid_level IS NOT NULL
                    AND recorded_at >= {time_cond}
                    ORDER BY recorded_at DESC LIMIT 30
                """, (did,))
            else:
                cursor.execute("""
                    SELECT recorded_at, liquid_level FROM readings
                    WHERE device_id = ? AND liquid_level IS NOT NULL
                    AND recorded_at >= datetime('now', ? || ' hours', 'localtime')
                    ORDER BY recorded_at DESC LIMIT 30
                """, (did, f'-{hours}'))
            rows = fetch_all(cursor, conn)
            anomalies.extend(_analyze_sudden_changes(rows, b))
        conn.close()
    except Exception:
        try:
            REST_URL = os.environ.get('SUPABASE_URL', '')
            REST_KEY = os.environ.get('SUPABASE_KEY', '')
            hdrs = {'apikey': REST_KEY, 'Authorization': f'Bearer {REST_KEY}'}
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            for b in bindings:
                if not b.get('bound_node'):
                    continue
                did = b['device_id']
                r = _requests.get(f'{REST_URL}/rest/v1/readings',
                    params={'select': 'recorded_at,liquid_level', 'device_id': f'eq.{did}',
                            'liquid_level': 'not.is.null', 'recorded_at': f'gte.{cutoff}',
                            'order': 'recorded_at.desc', 'limit': 30},
                    headers=hdrs, timeout=30, verify=False)
                rows = r.json()
                anomalies.extend(_analyze_sudden_changes(rows, b))
        except Exception:
            pass
    return anomalies


def _analyze_sudden_changes(rows, b):
    anomalies = []
    if len(rows) < 5:
        return anomalies
    did = b['device_id']
    rows.reverse()
    for i in range(1, len(rows)):
        try:
            t1 = datetime.fromisoformat(rows[i - 1]['recorded_at'])
            t2 = datetime.fromisoformat(rows[i]['recorded_at'])
            time_diff = (t2 - t1).total_seconds() / 3600
            if time_diff <= 0: continue
            l1 = rows[i - 1].get('liquid_level') or 0
            l2 = rows[i].get('liquid_level') or 0
            change_rate = abs(l2 - l1) / time_diff
            if change_rate > 0.5 and abs(l2 - l1) > 0.3:
                anomalies.append({
                    'device_id': did, 'node_id': b['bound_node']['point_id'],
                    'recorded_at': rows[i]['recorded_at'], 'type': 'level_sudden_change', 'severity': 'medium',
                    'from_level': round(rows[i - 1]['liquid_level'], 3),
                    'to_level': round(rows[i]['liquid_level'], 3),
                    'change': round(l2 - l1, 3), 'time_span_hours': round(time_diff, 1),
                    'change_rate': round(change_rate, 3),
                    'reason': f"液位突变：{round(l2 - l1, 3):+.3f}m（{rows[i - 1]['recorded_at']} → {rows[i]['recorded_at']}，速率{change_rate:.2f}m/h）"
                })
        except: continue
    return anomalies


def get_rainfall_periods(hours_back=168):
    """从天气数据中识别降雨时段"""
    import requests as _requests
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()
        time_cond = hours_ago(hours_back)
        if config.DB_TYPE == 'postgresql':
            cursor.execute(f"""
                SELECT recorded_at, rainfall_mm FROM weather_data
                WHERE rainfall_mm IS NOT NULL
                AND recorded_at >= {time_cond}
                ORDER BY recorded_at
            """)
        else:
            cursor.execute("""
                SELECT recorded_at, rainfall_mm FROM weather_data
                WHERE rainfall_mm IS NOT NULL
                AND recorded_at >= datetime('now', ? || ' hours', 'localtime')
                ORDER BY recorded_at
            """, (f'-{hours_back}',))
        rows = fetch_all(cursor, conn)
        conn.close()
    except Exception:
        try:
            from datetime import timedelta
            cutoff = (config.now_sh() - timedelta(hours=hours_back)).isoformat()
            REST_URL = os.environ.get('SUPABASE_URL', '')
            REST_KEY = os.environ.get('SUPABASE_KEY', '')
            r = _requests.get(f'{REST_URL}/rest/v1/weather_data',
                params={'select': 'recorded_at,rainfall_mm', 'rainfall_mm': 'not.is.null',
                        'recorded_at': f'gte.{cutoff}', 'order': 'recorded_at'},
                headers={'apikey': REST_KEY, 'Authorization': f'Bearer {REST_KEY}'},
                timeout=30)
            rows = r.json()
        except Exception:
            rows = []

    rainfall_hours = set()
    heavy_rain_hours = set()
    hourly_rain = defaultdict(float)

    for r in rows:
        try:
            t_str = str(r['recorded_at']).replace('T', ' ')
            t = t_str[:13]
            mm = float(r['rainfall_mm'] or 0)
            if mm > 0.1:
                rainfall_hours.add(t)
                hourly_rain[t] += mm
                if mm > 2.0:
                    heavy_rain_hours.add(t)
        except: continue

    sorted_hours = sorted(rainfall_hours)
    rainfall_periods = []
    if sorted_hours:
        current_start = sorted_hours[0]
        current_end = sorted_hours[0]
        current_total = hourly_rain.get(current_start, 0)
        for h in sorted_hours[1:]:
            try:
                h_dt = datetime.strptime(h, '%Y-%m-%d %H')
                end_dt = datetime.strptime(current_end, '%Y-%m-%d %H')
                gap = (h_dt - end_dt).total_seconds() / 3600
                if gap <= 3:
                    current_end = h
                    current_total += hourly_rain.get(h, 0)
                else:
                    if current_total > 1.0:
                        rainfall_periods.append((current_start, current_end, round(current_total, 1)))
                    current_start, current_end, current_total = h, h, hourly_rain.get(h, 0)
            except: continue
        if current_total > 1.0:
            rainfall_periods.append((current_start, current_end, round(current_total, 1)))
    return rainfall_hours, heavy_rain_hours, rainfall_periods


def analyze_rainfall_response(active_bindings, pipes, node_map, rainfall_hours):
    """分析降雨期间/之后设备的水位响应异常"""
    anomalies = []
    if not rainfall_hours:
        return anomalies

    try:
        sorted_rains = sorted(rainfall_hours)
        first_rain = datetime.strptime(sorted_rains[0], '%Y-%m-%d %H')
        last_rain = datetime.strptime(sorted_rains[-1], '%Y-%m-%d %H')
    except:
        return anomalies

    pre_rain_start = first_rain - timedelta(hours=12)

    for b in active_bindings:
        if not b.get('bound_node'):
            continue
        did = b['device_id']
        readings = get_device_recent_readings(did, hours=72, limit=50)
        if len(readings) < 10:
            continue

        well_bottom = b['bound_node'].get('well_bottom_elev')
        ground_elev = b['bound_node'].get('ground_elev')

        pre_rain_levels, during_rain_levels, post_rain_levels = [], [], []
        for r in readings:
            try:
                t = datetime.fromisoformat(r['recorded_at'])
                level = r.get('liquid_level')
                if level is None: continue
                if t < pre_rain_start: continue
                elif t < first_rain: pre_rain_levels.append(level)
                elif t <= last_rain + timedelta(hours=24):
                    # Check if this reading is during a rainfall hour
                    h_key = t.strftime('%Y-%m-%d %H')
                    if h_key in rainfall_hours:
                        during_rain_levels.append(level)
                    elif pre_rain_levels:
                        post_rain_levels.append(level)
                else:
                    post_rain_levels.append(level)
            except: continue

        pre_avg = sum(pre_rain_levels) / len(pre_rain_levels) if pre_rain_levels else None
        during_avg = sum(during_rain_levels) / len(during_rain_levels) if during_rain_levels else None
        post_avg = sum(post_rain_levels) / len(post_rain_levels) if post_rain_levels else None

        # 异常1：降雨期间水位不升反降
        if pre_avg is not None and during_avg is not None and during_avg < pre_avg - 0.3:
            anomalies.append({
                'device_id': did, 'node_id': b['bound_node']['point_id'],
                'recorded_at': str(last_rain), 'type': 'rainfall_response_abnormal', 'severity': 'high',
                'well_bottom_elev': well_bottom, 'ground_elev': ground_elev,
                'pre_rain_avg_level': round(pre_avg, 3), 'during_rain_avg_level': round(during_avg, 3),
                'level_change': round(during_avg - pre_avg, 3),
                'reason': f"降雨期间液位({during_avg:.3f}m)低于雨前({pre_avg:.3f}m)不升反降{abs(during_avg - pre_avg):.3f}m，可能传感器异常或管道倒灌"
            })

        # 异常2：降雨结束后排水不畅
        if pre_avg is not None and post_avg is not None and post_avg > pre_avg + 0.5:
            latest_levels = [r['liquid_level'] for r in readings[:5] if r.get('liquid_level') is not None]
            if latest_levels and sum(latest_levels)/len(latest_levels) > pre_avg + 0.5:
                anomalies.append({
                    'device_id': did, 'node_id': b['bound_node']['point_id'],
                    'recorded_at': readings[0]['recorded_at'], 'type': 'rainfall_drain_slow', 'severity': 'medium',
                    'well_bottom_elev': well_bottom, 'ground_elev': ground_elev,
                    'pre_rain_avg_level': round(pre_avg, 3), 'post_rain_avg_level': round(post_avg, 3),
                    'level_change': round(post_avg - pre_avg, 3),
                    'reason': f"降雨结束后液位({post_avg:.3f}m)仍高于雨前({pre_avg:.3f}m)差值{post_avg - pre_avg:.3f}m，排水不畅"
                })

        # 异常3：水位超过地面高程
        if during_avg and ground_elev and well_bottom:
            water_level = well_bottom + during_avg
            if water_level > ground_elev:
                anomalies.append({
                    'device_id': did, 'node_id': b['bound_node']['point_id'],
                    'recorded_at': str(first_rain), 'type': 'rainfall_overflow_risk', 'severity': 'critical',
                    'well_bottom_elev': well_bottom, 'ground_elev': ground_elev,
                    'water_level': round(water_level, 2), 'during_rain_avg_level': round(during_avg, 3),
                    'reason': f"降雨期间水位({water_level:.2f}m)超过地面高程({ground_elev:.2f}m)，存在倒灌/积水风险！"
                })

    return anomalies


def generate_liquid_analysis_report(bindings, pipes, node_map):
    """生成完整的液位分析报告 - 聚焦降雨期"""
    print("=" * 60)
    print("液位数据分析报告（基于连通器原理 - 聚焦降雨期）")
    print("=" * 60)

    # 0. 获取降雨时段（回看30天以捕获足够多的降雨事件）
    rainfall_hours, heavy_rain_hours, rainfall_periods = get_rainfall_periods(hours_back=720)
    print(f"\n[0] 降雨时段识别（近30天）：")
    print(f"    降雨小时数: {len(rainfall_hours)}")
    print(f"    强降雨(>2mm/h)小时数: {len(heavy_rain_hours)}")
    print(f"    连续降雨时段:")
    for start, end, total in rainfall_periods:
        print(f"      {start} → {end}  总量{total:.1f}mm")

    active_bindings, inactive_bindings = get_active_devices(bindings, hours=48)
    print(f"\n[1] 活跃设备筛选（48小时内有数据）：")
    print(f"    总设备数: {len(bindings)}，活跃设备(有绑定): {len(active_bindings)}，不活跃/无绑定: {len(inactive_bindings)}")

    adjacency, components = build_pipe_graph(pipes, node_map)
    print(f"\n[2] 管线拓扑分析：{len(pipes)}段管线，{len(components)}个连通分量")

    component_devices, unconnected_devices = assign_devices_to_components(active_bindings, node_map, components)
    print(f"\n[3] 分配到连通分量的设备: {sum(len(v) for v in component_devices.values())}")

    # 4. 水位一致性分析
    print(f"\n[4] 连通器水位一致性分析：")
    consistency_anomalies, component_stats = analyze_component_water_levels(component_devices, node_map)
    print(f"    发现{len(consistency_anomalies)}个异常（严重{len([a for a in consistency_anomalies if a['severity']=='critical'])}高{len([a for a in consistency_anomalies if a['severity']=='high'])}中{len([a for a in consistency_anomalies if a['severity']=='medium'])}）")

    for ci, stats in component_stats.items():
        devs = stats['devices']
        water_levels = [v['avg_water_level'] for v in devs.values()]
        if water_levels:
            mean_wl = sum(water_levels) / len(water_levels)
            print(f"    连通组{ci}（{len(devs)}台设备）平均水位: {mean_wl:.2f}m")
            for did, info in devs.items():
                print(f"      {did}: 水位={info['avg_water_level']:.2f}m 液位={info['avg_liquid_level']:.3f}m 井底={info['well_bottom_elev']:.2f}m")

    # 5. 水位倒置检测
    print(f"\n[5] 上下游水位倒置：")
    reversal_anomalies = detect_water_reversal(active_bindings, pipes, node_map)
    print(f"    发现{len(reversal_anomalies)}个")

    # 6. 降雨响应分析
    print(f"\n[6] 降雨响应分析：")
    rainfall_anomalies = analyze_rainfall_response(active_bindings, pipes, node_map, rainfall_hours)
    print(f"    发现{len(rainfall_anomalies)}个异常")
    for a in rainfall_anomalies[:5]:
        print(f"    {a['device_id']}: {a['reason']}")

    # 7. 数据冻结
    print(f"\n[7] 数据冻结：")
    frozen_anomalies = detect_frozen_data(active_bindings)
    print(f"    发现{len(frozen_anomalies)}个")

    # 8. 液位突变
    print(f"\n[8] 液位突变：")
    sudden_anomalies = detect_level_sudden_change(active_bindings)
    print(f"    发现{len(sudden_anomalies)}个")
    for a in sudden_anomalies[:5]:
        print(f"    {a['device_id']}: {a['reason']}")

    all_anomalies = consistency_anomalies + reversal_anomalies + rainfall_anomalies + frozen_anomalies + sudden_anomalies
    print(f"\n{'=' * 60}")
    print(f"分析总结：总异常 {len(all_anomalies)}")
    print(f"  水位一致性: {len(consistency_anomalies)}")
    print(f"  水位倒置: {len(reversal_anomalies)}")
    print(f"  降雨响应: {len(rainfall_anomalies)}")
    print(f"  数据冻结: {len(frozen_anomalies)}")
    print(f"  液位突变: {len(sudden_anomalies)}")
    print(f"{'=' * 60}")

    # 构建 component_stats 输出格式
    comp_stats_output = {}
    for ci, stats in component_stats.items():
        devs = stats['devices']
        water_levels = [v['avg_water_level'] for v in devs.values()]
        if water_levels:
            comp_stats_output[str(ci)] = {
                'device_count': len(devs),
                'mean_water_level': round(sum(water_levels) / len(water_levels), 3),
                'devices': {
                    did: {
                        'avg_water_level': info['avg_water_level'],
                        'avg_liquid_level': info['avg_liquid_level'],
                        'sample_count': info['sample_count'],
                        'node_id': info['node_id'],
                        'ground_elev': info['ground_elev'],
                        'well_bottom_elev': info['well_bottom_elev']
                    }
                    for did, info in devs.items()
                }
            }

    # 取最近10次降雨事件（按结束时间倒序）
    sorted_periods = sorted(rainfall_periods, key=lambda x: x[1], reverse=True)[:10]

    return {
        'active_device_count': len(active_bindings),
        'inactive_device_count': len(inactive_bindings),
        'component_count': len(components),
        'component_device_count': sum(len(v) for v in component_devices.values()),
        'rainfall_periods': [{'start': s, 'end': e, 'total_mm': t} for s, e, t in sorted_periods],
        'component_stats': comp_stats_output,
        'consistency_anomalies': consistency_anomalies,
        'reversal_anomalies': reversal_anomalies,
        'rainfall_anomalies': rainfall_anomalies,
        'frozen_anomalies': frozen_anomalies,
        'sudden_anomalies': sudden_anomalies,
        'total': len(all_anomalies)
    }


if __name__ == '__main__':
    import urllib3
    urllib3.disable_warnings()
    print("Loading data...")
    from data_processor import load_devices, load_pipe_nodes, load_pipes, bind_devices_to_nodes, build_topology
    devices = load_devices()
    nodes = load_pipe_nodes()
    pipes = load_pipes()
    node_map, adjacency = build_topology(nodes, pipes)
    bindings = bind_devices_to_nodes(devices, nodes)
    report = generate_liquid_analysis_report(bindings, pipes, node_map)


def calculate_ground_proximity(bindings, hours=24):
    """纯计算器：计算每个设备水位与地面高程的距离，返回原始数据（不含 severity）"""
    results = []
    for b in bindings:
        node = b.get('bound_node') or {}
        well_bottom = node.get('well_bottom_elev')
        ground_elev = node.get('ground_elev')
        if well_bottom is None or ground_elev is None:
            continue
        readings = get_device_recent_readings(b['device_id'], hours=hours, limit=10)
        if not readings:
            continue
        levels = [r['liquid_level'] for r in readings if r.get('liquid_level') is not None]
        if not levels:
            continue
        avg_level = sum(levels) / len(levels)
        water_level = well_bottom + avg_level
        gap = ground_elev - water_level

        results.append({
            'device_id': b['device_id'],
            'node_id': node.get('point_id', ''),
            'water_level': round(water_level, 2),
            'ground_elev': ground_elev,
            'well_bottom_elev': well_bottom,
            'liquid_level': round(avg_level, 3),
            'gap_to_ground': round(gap, 2),
        })
    return results


def classify_ground_proximity(items):
    """配置驱动：根据阈值为每个设备分配 severity"""
    thresholds = config.GROUND_PROXIMITY_THRESHOLDS
    for item in items:
        gap = item['gap_to_ground']
        item['severity'] = 'low'
        item['label'] = '正常'
        for t in thresholds:
            if gap < t['max_gap']:
                item['severity'] = t['severity']
                item['label'] = t['label']
                break
    return items


def build_topology_context(component_devices, node_map):
    """预计算拓扑上下文标签，供 AI 使用"""
    import statistics
    topology = {}
    for ci, devs in component_devices.items():
        if len(devs) < 2:
            continue
        dev_list = []
        for b in devs:
            node_id = b.get('bound_node', {}).get('point_id', '')
            node = node_map.get(node_id, {})
            well_bottom = node.get('well_bottom_elev')
            dev_list.append({
                'device_id': b['device_id'],
                'node_id': node_id,
                'well_bottom_elev': well_bottom,
                'ground_elev': node.get('ground_elev'),
            })

        elevations = [d['well_bottom_elev'] for d in dev_list if d['well_bottom_elev'] is not None]
        if not elevations:
            continue
        avg_elev = statistics.mean(elevations)

        for d in dev_list:
            if d['well_bottom_elev'] is not None:
                diff = d['well_bottom_elev'] - avg_elev
                d['elev_diff_from_mean'] = round(diff, 2)
                if diff > 1.0:
                    d['topology_context'] = '上游高位设备（地形抬升，水位高属正常）'
                    d['is_terrain_anomaly'] = False
                elif diff < -1.0:
                    d['topology_context'] = '下游低位设备（地形下降，水位低属正常）'
                    d['is_terrain_anomaly'] = False
                else:
                    d['topology_context'] = '同组同高程设备'
                    d['is_terrain_anomaly'] = True
            else:
                d['topology_context'] = '高程数据缺失'
                d['is_terrain_anomaly'] = None

        topology[f'component_{ci}'] = {
            'device_count': len(dev_list),
            'mean_well_bottom': round(avg_elev, 2),
            'elev_range': round(max(elevations) - min(elevations), 2),
            'devices': dev_list,
        }
    return topology