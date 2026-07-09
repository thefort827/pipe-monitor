"""
Vercel Serverless Entry Point - Supabase REST API version
"""
import os
import math
import requests
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates'),
            static_folder=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static'))

SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
HEADERS = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}


def rest_get(table, query=''):
    url = f'{SUPABASE_URL}/rest/v1/{table}'
    if query:
        url += f'?{query}'
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def wgs84_to_gcj02(lon, lat):
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/report')
def report():
    return render_template('liquid_report.html')


@app.route('/api/health')
def health():
    try:
        devices = rest_get('devices', 'select=device_id&limit=1')
        return jsonify({'status': 'ok', 'devices': len(devices)})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500


@app.route('/api/mapdata')
def map_data():
    try:
        devices_raw = rest_get('devices', 'select=device_id,name,area_name,device_type,longitude,latitude,last_seen')

        features = []
        now = datetime.utcnow()
        for d in devices_raw:
            if d.get('longitude') and d.get('latitude'):
                lon, lat = wgs84_to_gcj02(d['longitude'], d['latitude'])
                status = 'normal'
                if d.get('last_seen'):
                    try:
                        last = datetime.fromisoformat(str(d['last_seen']).replace('Z', '+00:00'))
                        if (now - last.replace(tzinfo=None)) > timedelta(days=7):
                            status = 'inactive'
                    except Exception:
                        pass

                features.append({
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                    'properties': {
                        'device_id': d['device_id'],
                        'name': d['name'] or d['device_id'],
                        'area_name': d['area_name'] or '',
                        'device_type': d['device_type'] or '',
                        'status': status,
                        'bound_node_id': None,
                        'bind_distance': None,
                        'details': []
                    }
                })

        try:
            cutoff = (now - timedelta(hours=48)).isoformat()
            readings = rest_get('readings', f'select=device_id,liquid_level,cod,ammonia_n,voltage&recorded_at=gte.{cutoff}&limit=1000')
            device_readings = {}
            for r in readings:
                did = r['device_id']
                if did not in device_readings:
                    device_readings[did] = r

            for f in features:
                did = f['properties']['device_id']
                if did in device_readings:
                    rd = device_readings[did]
                    if rd.get('liquid_level') and rd['liquid_level'] > 5.0:
                        f['properties']['status'] = 'critical'
                        f['properties']['details'] = [f'液位{rd["liquid_level"]:.2f}m(超阈值)']
                    elif rd.get('liquid_level') and rd['liquid_level'] > 3.0:
                        f['properties']['status'] = 'warning'
                        f['properties']['details'] = [f'液位{rd["liquid_level"]:.2f}m(偏高)']
        except Exception:
            pass

        node_features = []
        node_map = {}
        try:
            nodes = rest_get('pipe_nodes', 'select=*&limit=5000')
            for n in nodes:
                node_map[n['point_id']] = n
                node_features.append({
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [n['lon'], n['lat']]},
                    'properties': {
                        'point_id': n['point_id'],
                        'pipe_type': n.get('pipe_type', ''),
                        'sub_type': n.get('sub_type', ''),
                        'feature': n.get('feature', ''),
                        'ground_elev': n.get('ground_elev'),
                        'well_bottom_elev': n.get('well_bottom_elev'),
                        'depth': n.get('depth')
                    }
                })
        except Exception:
            pass

        pipe_features = []
        try:
            pipes = rest_get('pipe_segments', 'select=*&limit=5000')
            for i, p in enumerate(pipes):
                start = node_map.get(p['start_id'])
                end = node_map.get(p['end_id'])
                if start and end:
                    pipe_features.append({
                        'type': 'Feature',
                        'geometry': {
                            'type': 'LineString',
                            'coordinates': [
                                [start['lon'], start['lat']],
                                [end['lon'], end['lat']]
                            ]
                        },
                        'properties': {
                            'pipe_index': i,
                            'sub_type': p.get('sub_type', ''),
                            'diameter': p.get('diameter', ''),
                            'start_id': p['start_id'],
                            'end_id': p['end_id']
                        }
                    })
        except Exception:
            pass

        return jsonify({
            'devices': {'type': 'FeatureCollection', 'features': features},
            'nodes': {'type': 'FeatureCollection', 'features': node_features},
            'pipes': {'type': 'FeatureCollection', 'features': pipe_features}
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/device/<device_id>/readings')
def device_readings(device_id):
    hours = request.args.get('hours', 168, type=int)
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        rows = rest_get('readings', f'select=recorded_at,liquid_level,ammonia_n,cod,voltage,isonline&device_id=eq.{device_id}&recorded_at=gte.{cutoff}&order=recorded_at.desc&limit=1000')
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/profile')
def device_profile(device_id):
    try:
        devices = rest_get('devices', f'select=device_id,name,longitude,latitude&device_id=eq.{device_id}')
        device = devices[0] if devices else None

        if not device:
            return jsonify({'error': 'Device not found'}), 404

        readings = rest_get('readings', f'select=liquid_level,recorded_at&device_id=eq.{device_id}&liquid_level=not.is.null&order=recorded_at.desc&limit=1')
        reading = readings[0] if readings else None

        profile = {
            'device_id': device_id,
            'device_name': device.get('name', device_id),
            'liquid_level': reading.get('liquid_level') if reading else None,
            'recorded_at': reading.get('recorded_at') if reading else None,
            'node_details': {},
            'bound_node': None,
            'upstream': [],
            'downstream': [],
            'pipe_segments': [],
            'total_length': 0
        }

        return jsonify(profile)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/liquid-analysis')
def liquid_analysis():
    return jsonify({'anomalies': [], 'total': 0, 'components': {}})


@app.route('/api/liquid-analysis/timeseries')
def liquid_timeseries():
    try:
        hours = request.args.get('hours', 168, type=int)
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        readings = rest_get('readings',
            f'select=device_id,recorded_at,liquid_level&recorded_at=gte.{cutoff}&order=recorded_at.asc&limit=5000')

        time_set = sorted(set(r['recorded_at'][:16] for r in readings if r.get('recorded_at')))
        time_labels = time_set[-168:] if len(time_set) > 168 else time_set

        device_data = {}
        for r in readings:
            did = r.get('device_id')
            ts = r.get('recorded_at', '')[:16]
            if did and ts in time_labels:
                if did not in device_data:
                    device_data[did] = {}
                device_data[did][ts] = r.get('liquid_level')

        devices = {}
        for did, ts_map in device_data.items():
            devices[did] = [ts_map.get(t) for t in time_labels]

        return jsonify({
            'time_labels': time_labels,
            'rainfall_mm': [0] * len(time_labels),
            'devices': devices
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


application = app
