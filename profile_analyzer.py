import sqlite3
import math
import config
from collections import defaultdict
import weather_service


def parse_diameter(diameter_str):
    """解析管径字符串为数值（mm）"""
    if not diameter_str:
        return 300
    s = str(diameter_str).upper().strip()
    if s.startswith('DN'):
        try:
            return int(s[2:])
        except:
            pass
    if s.startswith('D'):
        try:
            return int(s[1:])
        except:
            pass
    try:
        return int(float(s))
    except:
        return 300


def calculate_fullness(level, diameter_mm):
    """计算充满度"""
    if not level or not diameter_mm or diameter_mm <= 0:
        return 0
    return round(level / (diameter_mm / 1000) * 100, 1)


def get_device_profile(device_id, bindings, pipes, node_map, readings):
    """获取设备的管线剖面数据（专业版）"""
    try:
        device_binding = next((b for b in bindings if b['device_id'] == device_id), None)
        if not device_binding or not device_binding.get('bound_node'):
            return None

        bound_node_id = device_binding['bound_node']['point_id']

        adjacency = defaultdict(list)
        # 有向邻接表：start→end（顺流/下游方向）
        downstream_adj = defaultdict(list)
        # 有向邻接表：end→start（逆流/上游方向）
        upstream_adj = defaultdict(list)
        pipe_info = {}
        for pipe in pipes:
            adjacency[pipe['start_id']].append((pipe['end_id'], pipe))
            adjacency[pipe['end_id']].append((pipe['start_id'], pipe))
            downstream_adj[pipe['start_id']].append((pipe['end_id'], pipe))
            upstream_adj[pipe['end_id']].append((pipe['start_id'], pipe))
            key = tuple(sorted([pipe['start_id'], pipe['end_id']]))
            pipe_info[key] = pipe

        upstream_nodes = get_upstream_nodes(bound_node_id, upstream_adj, node_map, depth=3)
        downstream_nodes = get_downstream_nodes(bound_node_id, downstream_adj, node_map, depth=3)

        # 从KCGIS StreamServer获取历史数据
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            from kcgis_service import KCGIService
            kcgis = KCGIService()
            device_readings = kcgis.get_device_history_data(device_id, hours=168)
        except Exception as e:
            print(f"Failed to get KCGIS history data: {e}")
            device_readings = []

        if not device_readings:
            device_readings = get_device_readings(device_id)

        upstream_readings = {}
        for node in upstream_nodes:
            up_device = find_device_at_node(node['point_id'], bindings)
            if up_device:
                upstream_readings[node['point_id']] = get_device_readings(up_device['device_id'])

        downstream_readings = {}
        for node in downstream_nodes:
            down_device = find_device_at_node(node['point_id'], bindings)
            if down_device:
                downstream_readings[node['point_id']] = get_device_readings(down_device['device_id'])

        all_nodes = upstream_nodes + [device_binding['bound_node']] + downstream_nodes

        node_details = {}
        for node in all_nodes:
            nid = node['point_id']
            readings_for_node = []
            if nid == bound_node_id:
                readings_for_node = device_readings
            elif nid in upstream_readings:
                readings_for_node = upstream_readings[nid]
            elif nid in downstream_readings:
                readings_for_node = downstream_readings[nid]

            level = readings_for_node[0]['liquid_level'] if readings_for_node else 0

            connected_pipes = []
            for n, p in adjacency.get(nid, []):
                if n != nid:
                    connected_pipes.append(p)

            diameter_str = connected_pipes[0].get('diameter', '') if connected_pipes else ''
            diameter_mm = parse_diameter(diameter_str)
            fullness = calculate_fullness(level, diameter_mm)

            node_details[nid] = {
                'point_id': nid,
                'ground_elev': node.get('ground_elev'),
                'well_bottom_elev': node.get('well_bottom_elev'),
                'level': level,
                'diameter': diameter_str,
                'diameter_mm': diameter_mm,
                'fullness': fullness,
                'has_device': nid == bound_node_id
            }

        pipe_segments = []
        cumulative_dist = [0]
        for i in range(len(all_nodes) - 1):
            n1 = all_nodes[i]
            n2 = all_nodes[i + 1]
            key = tuple(sorted([n1['point_id'], n2['point_id']]))
            pipe = pipe_info.get(key, {})

            dist = calculate_distance(n1, n2)
            elev_diff = (n2.get('well_bottom_elev') or 0) - (n1.get('well_bottom_elev') or 0)
            slope = (elev_diff / dist * 100) if dist > 0 else 0

            pipe_segments.append({
                'from': n1['point_id'],
                'to': n2['point_id'],
                'diameter': pipe.get('diameter', ''),
                'length': round(dist, 1),
                'slope': round(slope, 2),
                'sub_type': pipe.get('sub_type', ''),
                'direction': '↓顺坡' if slope <= 0 else '↑逆坡'
            })
            cumulative_dist.append(cumulative_dist[-1] + dist)

        result = {
            'device_id': device_id,
            'bound_node': device_binding['bound_node'],
            'device_level': device_readings[0]['liquid_level'] if device_readings else None,
            'upstream': upstream_nodes,
            'downstream': downstream_nodes,
            'upstream_readings': upstream_readings,
            'downstream_readings': downstream_readings,
            'pipe_segments': pipe_segments,
            'node_details': node_details,
            'cumulative_dist': cumulative_dist,
            'total_length': cumulative_dist[-1],
            'device_readings': device_readings
        }

        lat = device_binding.get('latitude')
        lon = device_binding.get('longitude')
        if lat and lon:
            try:
                precip_data = weather_service.get_recent_precipitation_summary(lat, lon, hours=24)
                result['precipitation'] = precip_data
                result['precipitation_hourly'] = precip_data.get('hourly', [])
            except Exception as e:
                print(f"Weather data failed for {device_id}: {e}")
                result['precipitation'] = {"total_24h": 0, "total_7d": 0, "hourly": []}
                result['precipitation_hourly'] = []

        return result
    except Exception as e:
        print(f"Profile error for {device_id}: {e}")
        return None


def calculate_distance(node1, node2):
    """计算两点间的距离（米）"""
    lon1 = node1.get('lon', 0)
    lat1 = node1.get('lat', 0)
    lon2 = node2.get('lon', 0)
    lat2 = node2.get('lat', 0)
    
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def get_upstream_nodes(start_node, adjacency, node_map, depth=3):
    """获取上游节点"""
    visited = set()
    result = []
    queue = [(start_node, 0)]
    
    while queue and len(result) < depth:
        node_id, dist = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        
        if node_id != start_node and node_id in node_map:
            result.append(node_map[node_id])
        
        for neighbor_id, pipe in adjacency.get(node_id, []):
            if neighbor_id not in visited:
                queue.append((neighbor_id, dist + 1))
    
    return list(reversed(result))


def get_downstream_nodes(start_node, adjacency, node_map, depth=3):
    """获取下游节点"""
    visited = set()
    result = []
    queue = [(start_node, 0)]
    
    while queue and len(result) < depth:
        node_id, dist = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        
        if node_id != start_node and node_id in node_map:
            result.append(node_map[node_id])
        
        for neighbor_id, pipe in adjacency.get(node_id, []):
            if neighbor_id not in visited:
                queue.append((neighbor_id, dist + 1))
    
    return result


def find_device_at_node(node_id, bindings):
    """找到绑定到指定管点的设备"""
    for b in bindings:
        if b.get('bound_node') and b['bound_node']['point_id'] == node_id:
            return b
    return None


def get_device_readings(device_id, limit=200):
    """获取设备最近读数，优先DB，失败则回退Supabase REST API"""
    try:
        from db import get_conn, fetch_all
        conn = get_conn()
        cursor = conn.cursor()
        if config.DB_TYPE == 'postgresql':
            cursor.execute("""
                SELECT recorded_at, liquid_level, ammonia_n, cod, voltage
                FROM readings
                WHERE device_id = %s
                AND liquid_level IS NOT NULL
                ORDER BY recorded_at DESC
                LIMIT %s
            """, (device_id, limit))
        else:
            cursor.execute("""
                SELECT recorded_at, liquid_level, ammonia_n, cod, voltage
                FROM readings
                WHERE device_id = ?
                AND liquid_level IS NOT NULL
                ORDER BY recorded_at DESC
                LIMIT ?
            """, (device_id, limit))
        rows = fetch_all(cursor, conn)
        conn.close()
        return rows
    except Exception as e:
        print(f"DB failed for {device_id}, using REST API: {e}")
        try:
            import requests as req
            import os
            url = f"{os.getenv('SUPABASE_URL', '')}/rest/v1/readings"
            headers = {
                "apikey": os.getenv('SUPABASE_KEY', ''),
                "Authorization": f"Bearer {os.getenv('SUPABASE_KEY', '')}"
            }
            params = {
                "select": "recorded_at,liquid_level,ammonia_n,cod,voltage",
                "device_id": f"eq.{device_id}",
                "liquid_level": "not.is.null",
                "order": "recorded_at.desc",
                "limit": str(limit)
            }
            r = req.get(url, headers=headers, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e2:
            print(f"REST API also failed for {device_id}: {e2}")
            return []
