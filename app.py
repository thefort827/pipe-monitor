import os
import threading
import json
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, jsonify, request
from flask_compress import Compress

import sqlite3

import data_processor
import profile_analyzer
import liquid_analysis
import ai_report_generator
import config
import config_secrets
from datetime import datetime, timedelta

import logging

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
Compress(app)


# ===== 数据库初始化 =====

def get_admin_db():
    """获取管理数据库连接"""
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_admin_db():
    """初始化管理数据库表"""
    conn = get_admin_db()
    cursor = conn.cursor()
    
    # 用户表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            wechat_id TEXT,
            display_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    ''')
    
    # 消息日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT,
            receiver TEXT,
            receiver_name TEXT,
            message TEXT,
            message_type TEXT DEFAULT 'text',
            status TEXT DEFAULT 'sent',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 告警联系人表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            wechat_id TEXT NOT NULL,
            alert_types TEXT DEFAULT '["device_alert","daily_report"]',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 系统配置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_config (
            key TEXT PRIMARY KEY,
            value TEXT,
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 定时任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            task_type TEXT,
            cron_expression TEXT,
            is_active INTEGER DEFAULT 1,
            last_run TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 不再需要默认管理员账户，用户通过二维码扫描添加
    
    # 插入默认配置
    default_configs = [
        ('ai_model', 'mimo-v2.5', 'AI分析模型'),
        ('alert_threshold_liquid', '0.8', '液位告警阈值(米)'),
        ('alert_threshold_cod', '40', 'COD告警阈值(mg/L)'),
        ('alert_threshold_nh3n', '5', '氨氮告警阈值(mg/L)'),
        ('report_interval', '1', '报告生成间隔(小时)'),
    ]
    for key, value, desc in default_configs:
        cursor.execute(
            'INSERT OR IGNORE INTO system_config (key, value, description) VALUES (?, ?, ?)',
            (key, value, desc)
        )
    
    conn.commit()
    conn.close()
    logger.info("Admin database initialized")


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@app.after_request
def add_no_cache(response):
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        # Inject Chart.js bootstrap into <head> so charts work even if
        # the browser serves a stale cached template without Chart.js
        try:
            body = response.get_data(as_text=True)
            inject = '<script src="/static/js/chart-bootstrap.js"></script>'
            if 'chart-bootstrap.js' not in body and '<head>' in body:
                body = body.replace('<head>', '<head>\n    ' + inject, 1)
                response.set_data(body)
        except Exception:
            pass
    return response

data_cache = {}
app_start_time = datetime.now()
_data_initialized = False
_backfill_lock = threading.Lock()


@app.before_request
def before_request():
    """Lazy-load data on first request (for Vercel serverless)"""
    global _data_initialized
    if not _data_initialized:
        init_data()
        data_cache['last_update'] = datetime.now().isoformat()
        _data_initialized = True

# Initialize KCGIS Service
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from kcgis_service import KCGIService

# 从环境变量读取 KCGIS Token
KCGIS_TOKEN = os.getenv('KCGIS_TOKEN', '')
kcgis_service = KCGIService(token=KCGIS_TOKEN)

# Initialize APScheduler
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()

def refresh_device_data():
    """Periodically refresh device data from KCGIS API"""
    try:
        logger.info("Starting periodic device data refresh...")

        old_total = data_cache.get('liquid_report', {}).get('total', 0)

        # 先清理影子设备
        cleaned = kcgis_service.cleanup_shadow_devices()
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} shadow devices")

        kcgis_devices = kcgis_service.get_devices()
        if kcgis_devices:
            synced = kcgis_service.sync_to_database(kcgis_devices)
            logger.info(f"Device refresh: {synced} devices synced")
            # Reload cache
            data_cache['devices'] = data_processor.load_devices()
            data_cache['bindings'] = data_processor.bind_devices_to_nodes(
                data_cache['devices'], data_cache['nodes']
            )
            data_cache['last_update'] = datetime.now().isoformat()
        else:
            logger.info("Device refresh: KCGIS unavailable")

        # 重新运行液位分析
        try:
            new_report = liquid_analysis.generate_liquid_analysis_report(
                data_cache['bindings'], data_cache['pipes'], data_cache['node_map']
            )
            new_total = new_report.get('total', 0)
            data_cache['liquid_report'] = new_report

            # 检测新增异常并推送告警
            if new_total > old_total:
                new_anomalies = new_report.get('anomalies', [])[:new_total - old_total]
                try:
                    from wechat_bot import push_alert_to_contacts
                    alert_text = "=== 管网异常告警 ===\n"
                    for a in new_anomalies:
                        alert_text += f"[{a.get('severity', '')}] {a.get('device_id', '')}: {a.get('description', '')}\n"
                    alert_text += f"\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    push_alert_to_contacts(alert_text)
                except Exception as e:
                    logger.error(f"Alert push failed: {e}")
        except Exception as e:
            logger.error(f"Liquid analysis refresh failed: {e}")

    except Exception as e:
        logger.error("Device refresh failed: %s", e)

def refresh_weather_data():
    """Periodically refresh weather data"""
    try:
        logger.info("Starting periodic weather data refresh...")
        import weather_service
        sample_lat, sample_lon = 30.0, 118.0
        for d in data_cache.get('devices', []):
            if d.get('latitude') and d.get('longitude'):
                sample_lat = d['latitude']
                sample_lon = d['longitude']
                break
        weather_data = weather_service.get_from_api(sample_lat, sample_lon)
        if weather_data:
            weather_service.save_to_database(sample_lat, sample_lon, weather_data)
        logger.info("Weather data refreshed successfully")
    except Exception as e:
        logger.error(f"Weather refresh failed: {e}")

def refresh_ai_report():
    """Periodically refresh AI engineering report"""
    try:
        logger.info("Starting AI report refresh...")
        ai_report_generator.generate_report(data_cache)
        logger.info("AI report refresh complete")
    except Exception as e:
        logger.error("AI report refresh failed: %s", e)


def refresh_and_push_hourly_report():
    """Generate hourly report and push to WeChat"""
    try:
        logger.info("Starting hourly report generation...")
        ai_report_generator.generate_hourly_report(data_cache)
        logger.info("Hourly report generated, pushing to WeChat...")
        from wechat_bot import push_hourly_report
        push_hourly_report(data_cache)
        logger.info("Hourly report push complete")
    except Exception as e:
        logger.error("Hourly report push failed: %s", e)


def _refresh_after_backfill():
    """回填完成后刷新缓存：重新加载 readings + 重跑液位分析"""
    try:
        data_cache['readings'] = data_processor.get_all_recent_readings(hours=168)
        liquid_report = liquid_analysis.generate_liquid_analysis_report(
            data_cache['bindings'], data_cache['pipes'], data_cache['node_map']
        )
        data_cache['liquid_report'] = liquid_report
        active_b, inactive_b = liquid_analysis.get_active_devices(data_cache['bindings'], hours=48)
        data_cache['active_bindings'] = active_b
        data_cache['inactive_bindings'] = inactive_b
        data_cache['last_update'] = datetime.now().isoformat()
        logger.info("[BACKFILL-REFRESH] Analysis refreshed: %d anomalies", liquid_report.get('total', 0))
    except Exception as e:
        logger.error("[BACKFILL-REFRESH] Refresh failed: %s", e)


def startup_backfill_and_analyze():
    """启动时后台线程：增量回填 + 刷新液位分析"""
    if not _backfill_lock.acquire(blocking=False):
        logger.info("[STARTUP-BACKFILL] Skipped: another backfill is running")
        return
    try:
        from data_backfill import DataBackfill
        from db import get_conn
        backfill = DataBackfill(kcgis_service, get_conn=get_conn)
        logger.info("[STARTUP-BACKFILL] Starting incremental backfill...")
        result = backfill.backfill_incremental()
        logger.info("[STARTUP-BACKFILL] Backfill done: %d fetched, %d inserted",
                     result.get('total_fetched', 0), result.get('total_inserted', 0))
        _refresh_after_backfill()
    except Exception as e:
        logger.error("[STARTUP-BACKFILL] Failed: %s", e)
    finally:
        _backfill_lock.release()


def refresh_backfill_data():
    """APScheduler 每小时任务：轻量增量回填 + 刷新液位分析"""
    if not _backfill_lock.acquire(blocking=False):
        logger.info("[BACKFILL-HOURLY] Skipped: another backfill is running")
        return
    try:
        from data_backfill import DataBackfill
        from db import get_conn
        backfill = DataBackfill(kcgis_service, get_conn=get_conn)
        result = backfill.backfill_incremental_light(hours=6)
        inserted = result.get('total_inserted', 0)
        if inserted > 0:
            logger.info("[BACKFILL-HOURLY] Synced %d records, refreshing analysis", inserted)
            _refresh_after_backfill()
        else:
            logger.info("[BACKFILL-HOURLY] No new data")
    except Exception as e:
        logger.error("[BACKFILL-HOURLY] Failed: %s", e)
    finally:
        _backfill_lock.release()


# Schedule periodic tasks
scheduler.add_job(refresh_device_data, 'interval', hours=1, id='device_refresh')
scheduler.add_job(refresh_weather_data, 'interval', hours=6, id='weather_refresh')
scheduler.add_job(refresh_ai_report, 'interval', hours=1, id='ai_report_refresh')
scheduler.add_job(refresh_backfill_data, 'interval', hours=1, id='backfill_refresh')
scheduler.add_job(refresh_and_push_hourly_report, 'interval', hours=1, id='hourly_report_push')


def init_pg_tables():
    """如果使用 PostgreSQL，确保表结构存在"""
    if config.DB_TYPE != 'postgresql':
        return
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id SERIAL PRIMARY KEY,
                device_id TEXT UNIQUE,
                name TEXT, area_name TEXT, device_type TEXT,
                manufacturers TEXT, address TEXT,
                longitude REAL, latitude REAL,
                first_seen TEXT, last_seen TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id SERIAL PRIMARY KEY,
                device_id TEXT, recorded_at TIMESTAMP,
                liquid_level REAL, ammonia_n REAL, cod REAL, voltage REAL,
                isonline TEXT, created_at TEXT, temperature REAL,
                status INTEGER, threshold_exceed TEXT, getvaluetime TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id, recorded_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_data (
                id SERIAL PRIMARY KEY, recorded_at TEXT,
                latitude REAL, longitude REAL, rainfall_mm REAL,
                temp_c REAL, humidity INTEGER, source TEXT, created_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fetch_log (
                id SERIAL PRIMARY KEY, started_at TIMESTAMP, time_start TEXT, time_end TEXT,
                records_fetched INTEGER DEFAULT 0, records_inserted INTEGER DEFAULT 0,
                status TEXT, error_msg TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backfill_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_synced_time TIMESTAMP, last_run_at TIMESTAMP,
                total_fetched INTEGER DEFAULT 0, total_inserted INTEGER DEFAULT 0,
                status TEXT DEFAULT 'idle'
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pipe_nodes (
                id SERIAL PRIMARY KEY, point_id TEXT UNIQUE,
                pipe_type TEXT, sub_type TEXT, feature TEXT,
                ground_elev REAL, well_bottom_elev REAL, depth REAL,
                lon REAL, lat REAL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pipe_segments (
                id SERIAL PRIMARY KEY, start_id TEXT, end_id TEXT,
                sub_type TEXT, diameter TEXT, length REAL
            )
        """)
        
        conn.commit()
        conn.close()
        print("PostgreSQL tables initialized")
    except Exception as e:
        print(f"PostgreSQL table init error: {e}")


def init_data():
    # 初始化管理数据库
    print("Initializing admin database...")
    try:
        init_admin_db()
        print("Admin database initialized")
    except Exception as e:
        print(f"Admin DB init error: {e}")

    # 初始化 PostgreSQL 表
    init_pg_tables()

    # 先清理影子设备
    print("Cleaning up shadow devices...")
    try:
        cleaned = kcgis_service.cleanup_shadow_devices()
        print(f"Cleaned up {cleaned} shadow devices")
    except Exception as e:
        print(f"Cleanup error: {e}")

    # 从 KCGIS API 同步设备数据到数据库
    print("Syncing devices from KCGIS API...")
    try:
        kcgis_devices = kcgis_service.get_devices()
        if kcgis_devices:
            synced = kcgis_service.sync_to_database(kcgis_devices)
            print(f"KCGIS sync: {synced} devices added/updated")
        else:
            print("KCGIS sync: unavailable, using cached data")
    except Exception as e:
        print(f"KCGIS sync: unavailable, using cached data")

    print("Loading devices...")
    data_cache['devices'] = data_processor.load_devices()
    print(f"Loaded {len(data_cache['devices'])} devices")

    print("Loading pipe nodes...")
    data_cache['nodes'] = data_processor.load_pipe_nodes()
    print(f"Loaded {len(data_cache['nodes'])} pipe nodes")

    print("Loading pipes...")
    data_cache['pipes'] = data_processor.load_pipes()
    print(f"Loaded {len(data_cache['pipes'])} pipes")

    print("Binding devices to nodes...")
    data_cache['bindings'] = data_processor.bind_devices_to_nodes(
        data_cache['devices'], data_cache['nodes']
    )

    print("Building topology...")
    data_cache['node_map'], data_cache['adjacency'] = data_processor.build_topology(
        data_cache['nodes'], data_cache['pipes']
    )

    print("Loading recent readings...")
    data_cache['readings'] = data_processor.get_all_recent_readings(hours=168)
    print(f"Loaded {len(data_cache['readings'])} recent readings")

    # 液态连通器分析
    print("Running liquid analysis (communicating vessels)...")
    liquid_report = liquid_analysis.generate_liquid_analysis_report(
        data_cache['bindings'], data_cache['pipes'], data_cache['node_map']
    )
    data_cache['liquid_report'] = liquid_report
    print(f"Liquid analysis complete: {liquid_report.get('total', 0)} anomalies found")

    # 缓存活跃设备（48h内有液位数据），用于地图过滤
    active_b, inactive_b = liquid_analysis.get_active_devices(data_cache['bindings'], hours=48)
    data_cache['active_bindings'] = active_b
    data_cache['inactive_bindings'] = inactive_b
    print(f"Active devices: {len(active_b)}, inactive/hidden: {len(inactive_b)}")

    print("Data initialization complete.")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/mapdata')
def map_data():
    # 返回全部设备，不活跃设备在GeoJSON中标记为inactive（灰色显示）
    all_bindings = data_cache['bindings']
    active_bindings = data_cache.get('active_bindings', data_cache['bindings'])
    active_ids = {b['device_id'] for b in active_bindings}
    return jsonify({
        'devices': data_processor.devices_to_geojson(all_bindings, active_device_ids=active_ids),
        'nodes': data_processor.nodes_to_geojson(data_cache['nodes']),
        'pipes': data_processor.pipes_to_geojson(data_cache['pipes'], data_cache['node_map'])
    })


@app.route('/api/device/<device_id>/readings')
def device_readings(device_id):
    hours = request.args.get('hours', 168, type=int)
    try:
        return jsonify(data_processor.get_device_readings(device_id, hours))
    except Exception as e:
        logger.error("Readings API error for %s: %s", device_id, e)
        return jsonify({'error': str(e), 'device_id': device_id}), 500


@app.route('/api/device/<device_id>/realtime')
def device_realtime(device_id):
    """从KCGIS获取设备实时数据"""
    try:
        realtime_data = kcgis_service.get_device_realtime_data(device_id)
        if realtime_data:
            return jsonify(realtime_data)
        return jsonify({'error': 'No realtime data available'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/history')
def device_history(device_id):
    """从KCGIS StreamServer获取设备历史数据"""
    try:
        hours = request.args.get('hours', 168, type=int)
        history_data = kcgis_service.get_device_history_data(device_id, hours)
        return jsonify(history_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/device/<device_id>/profile')
def device_profile(device_id):
    try:
        profile = profile_analyzer.get_device_profile(
            device_id,
            data_cache['bindings'],
            data_cache['pipes'],
            data_cache['node_map'],
            data_cache['readings']
        )
        if profile:
            return jsonify(profile)
        return jsonify({'error': 'Device not found or no profile data'}), 404
    except Exception as e:
        logger.error(f"Profile error for {device_id}: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/report')
def liquid_report():
    return render_template('liquid_report.html')


@app.route('/api/liquid-analysis')
def api_liquid_analysis():
    """返回液位连通器分析结果（含连通组详细信息）"""
    report = data_cache.get('liquid_report', {})
    # 尝试重新构建 component_stats（如果缓存中为空）
    if not report.get('component_stats') and data_cache.get('bindings'):
        active_b, _ = liquid_analysis.get_active_devices(data_cache['bindings'], hours=48)
        adj, comps = liquid_analysis.build_pipe_graph(data_cache['pipes'], data_cache['node_map'])
        comp_devs, _ = liquid_analysis.assign_devices_to_components(active_b, data_cache['node_map'], comps)
        _, comp_stats = liquid_analysis.analyze_component_water_levels(comp_devs, data_cache['node_map'])
        comp_stats_output = {}
        for ci, stats in comp_stats.items():
            devs = stats['devices']
            wls = [v['avg_water_level'] for v in devs.values()]
            if wls:
                comp_stats_output[str(ci)] = {
                    'device_count': len(devs),
                    'mean_water_level': round(sum(wls) / len(wls), 3),
                    'devices': {
                        did: {
                            'avg_water_level': info['avg_water_level'],
                            'avg_liquid_level': info['avg_liquid_level'],
                            'node_id': info['node_id'],
                            'ground_elev': info['ground_elev'],
                            'well_bottom_elev': info['well_bottom_elev']
                        } for did, info in devs.items()
                    }
                }
        report['component_stats'] = comp_stats_output

    # 添加水位接近地面高程预警数据
    if 'ground_proximity' not in report and data_cache.get('bindings'):
        try:
            raw = liquid_analysis.calculate_ground_proximity(data_cache['bindings'], hours=24)
            report['ground_proximity'] = liquid_analysis.classify_ground_proximity(raw)
        except Exception as e:
            logger.warning("Ground proximity calculation failed: %s", e)
            report['ground_proximity'] = []

    return jsonify(report)


@app.route('/api/engineering-report')
def api_engineering_report():
    """返回 AI 生成的给排水工程建议报告"""
    report = data_cache.get('ai_engineering_report')
    if not report:
        try:
            report = ai_report_generator.generate_report(data_cache)
        except Exception as e:
            logger.error("AI report generation failed: %s", e)
            report = {'emergency': [], 'short_term': [], 'long_term': [],
                      'generated_at': datetime.now().isoformat(), 'error': str(e)}
    return jsonify(report)


@app.route('/api/hourly-report')
def api_hourly_report():
    """返回每小时巡检报告"""
    report = data_cache.get('ai_hourly_report')
    if not report:
        try:
            report = ai_report_generator.generate_hourly_report(data_cache)
        except Exception as e:
            logger.error("Hourly report generation failed: %s", e)
            report = {'summary': {}, 'device_status': [], 'anomaly_warnings': [],
                      'recommendations': {'emergency': [], 'short_term': [], 'long_term': []},
                      'generated_at': datetime.now().isoformat(), 'error': str(e)}
    return jsonify(report)


def _interpolate_gaps(series, max_gap=3):
    """线性插值填充小间隙：连续None不超过max_gap小时的用前后值插值"""
    n = len(series)
    result = list(series)
    i = 0
    while i < n:
        if result[i] is None:
            left = i - 1
            right = i
            while right < n and result[right] is None:
                right += 1
            gap_len = right - left - 1
            if left >= 0 and right < n and gap_len <= max_gap:
                left_val = result[left]
                right_val = result[right]
                for j in range(1, gap_len + 1):
                    t = j / (gap_len + 1)
                    result[left + j] = round(left_val + t * (right_val - left_val), 2)
            i = right
        else:
            i += 1
    return result


@app.route('/api/liquid-analysis/timeseries')
def api_liquid_timeseries():
    """返回过去72小时活跃设备的逐小时水位和对应降雨量时序数据"""
    bindings = data_cache.get('bindings', [])
    node_map = data_cache.get('node_map', {})
    pipes = data_cache.get('pipes', [])

    # 获取活跃设备及其绑定管点
    active_bindings, _ = liquid_analysis.get_active_devices(bindings, hours=48)

    # 获取降雨时段（只取set部分，忽略heavy_rain_hours和periods_list）
    rain_hours_set, _, _ = liquid_analysis.get_rainfall_periods(hours_back=72)

    # 从数据库获取过去72小时的降雨数据，构建逐小时降雨量字典
    rain_format_hours = {}
    try:
        from db import get_conn, fetch_all, hours_ago
        conn = get_conn()
        cursor = conn.cursor()
        time_cond = hours_ago(72)
        if config.DB_TYPE == 'postgresql':
            cursor.execute(f"""
                SELECT recorded_at, rainfall_mm FROM weather_data
                WHERE recorded_at >= {time_cond}
                ORDER BY recorded_at
            """)
        else:
            cursor.execute(f"""
                SELECT recorded_at, rainfall_mm FROM weather_data
                WHERE recorded_at >= {time_cond}
                ORDER BY recorded_at
            """)
        for row in fetch_all(cursor, conn):
            t_str = str(row['recorded_at']).replace('T', ' ')[:13]
            mm = float(row['rainfall_mm'] or 0)
            if mm > 0:
                rain_format_hours[t_str] = rain_format_hours.get(t_str, 0) + mm
        conn.close()
    except Exception as e:
        logger.warning(f"Weather DB query failed, using REST API: {e}")
        try:
            import requests as req
            url = f"{os.getenv('SUPABASE_URL', '')}/rest/v1/weather_data"
            headers = {
                "apikey": os.getenv('SUPABASE_KEY', ''),
                "Authorization": f"Bearer {os.getenv('SUPABASE_KEY', '')}"
            }
            params = {
                "select": "recorded_at,rainfall_mm",
                "order": "recorded_at.desc",
                "limit": "2000"
            }
            r = req.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            for row in r.json():
                t_str = str(row.get('recorded_at', '')).replace('T', ' ')[:13]
                mm = float(row.get('rainfall_mm') or 0)
                if mm > 0:
                    rain_format_hours[t_str] = rain_format_hours.get(t_str, 0) + mm
        except Exception as e2:
            logger.error(f"Weather REST fallback also failed: {e2}")

    # 构建72小时时间轴
    now = config.now_sh()
    time_labels = []
    rainfall_data = []
    for i in range(71, -1, -1):
        t = now - timedelta(hours=i)
        label = t.strftime('%Y-%m-%d %H:00')
        hour_key = t.strftime('%Y-%m-%d %H')
        time_labels.append(label)
        rainfall_data.append(round(rain_format_hours.get(hour_key, 0), 1))

    # 收集每个活跃设备的水位数据
    device_water_levels = {}
    for b in active_bindings:
        did = b['device_id']
        node = b['bound_node']
        well_bottom = node.get('well_bottom_elev')
        if well_bottom is None:
            continue

        readings = liquid_analysis.get_device_recent_readings(did, hours=72, limit=500)
        if not readings:
            continue

        # 按小时聚合
        hourly_levels = {}
        for r in readings:
            try:
                t = datetime.fromisoformat(r['recorded_at'])
                hour_key = t.strftime('%Y-%m-%d %H')
                level = r.get('liquid_level')
                if level is not None:
                    wl = well_bottom + level
                    if hour_key not in hourly_levels:
                        hourly_levels[hour_key] = []
                    hourly_levels[hour_key].append(wl)
            except:
                continue

        # 计算每小平均值，映射到72小时间轴
        series = []
        for i in range(71, -1, -1):
            t = now - timedelta(hours=i)
            hour_key = t.strftime('%Y-%m-%d %H')
            if hour_key in hourly_levels:
                vals = hourly_levels[hour_key]
                series.append(round(sum(vals) / len(vals), 2))
            else:
                series.append(None)

        device_water_levels[did.replace('HSTX_TSPS_', '')] = _interpolate_gaps(series)

    # 找到连通组信息，为每个设备标注所属组
    adjacency, components = liquid_analysis.build_pipe_graph(pipes, node_map)
    component_devices, _ = liquid_analysis.assign_devices_to_components(active_bindings, node_map, components)
    device_component = {}
    for ci, devs in component_devices.items():
        for b in devs:
            short_id = b['device_id'].replace('HSTX_TSPS_', '')
            device_component[short_id] = ci

    return jsonify({
        'time_labels': time_labels,
        'rainfall_mm': rainfall_data,
        'devices': device_water_levels,
        'device_component': device_component,
        'rainfall_periods': None  # 前端已从 /api/liquid-analysis 获取
    })


@app.route('/api/liquid-analysis/device/<device_id>')
def api_liquid_device_analysis(device_id):
    """返回单个设备在液位连通器分析中的详情"""
    liquid_report = data_cache.get('liquid_report', {})
    bindings = data_cache['bindings']

    # 查找该设备在连通组中的信息
    active_bindings, _ = liquid_analysis.get_active_devices(bindings, hours=48)
    adjacency, components = liquid_analysis.build_pipe_graph(
        data_cache['pipes'], data_cache['node_map']
    )
    component_devices, _ = liquid_analysis.assign_devices_to_components(
        active_bindings, data_cache['node_map'], components
    )

    # 找到设备所在连通组
    for ci, devices in component_devices.items():
        for b in devices:
            if b['device_id'] == device_id:
                # 获取最近的读数
                readings = liquid_analysis.get_device_recent_readings(device_id, hours=72, limit=48)

                # 获取同组其他设备
                group_devices = []
                for other_b in devices:
                    if other_b['device_id'] != device_id:
                        other_readings = liquid_analysis.get_device_recent_readings(
                            other_b['device_id'], hours=72, limit=10
                        )
                        group_devices.append({
                            'device_id': other_b['device_id'],
                            'name': other_b.get('name', ''),
                            'bound_node': other_b['bound_node']['point_id'],
                            'readings': other_readings[:5] if other_readings else [],
                            'distance': other_b.get('distance')
                        })

                return jsonify({
                    'device_id': device_id,
                    'component_index': ci,
                    'component_device_count': len(devices),
                    'readings': readings,
                    'group_devices': group_devices,
                    'binding': {
                        'bound_node': b['bound_node']['point_id'],
                        'ground_elev': b['bound_node'].get('ground_elev'),
                        'well_bottom_elev': b['bound_node'].get('well_bottom_elev'),
                        'distance': b.get('distance')
                    }
                })

    return jsonify({'error': 'Device not found in any connected component'}), 404


@app.route('/proxy/kcgis-tile/<path:tile_path>')
def proxy_kcgis_tile(tile_path):
    try:
        import requests
        base = config_secrets.KCGIS_TILE_URL
        url = f'{base.rsplit("/tile", 1)[0]}/tile/{tile_path}'
        response = requests.get(url, verify=False, timeout=10)
        return response.content, response.status_code, {'Content-Type': response.headers.get('content-type', 'image/png')}
    except Exception as e:
        print(f'KCGIS tile proxy error: {e}')
        return '', 503


@app.route('/proxy/kcgis-export')
def proxy_kcgis_export():
    try:
        import requests
        bbox = request.args.get('bbox', '')
        if not bbox:
            return '', 400
        base = config_secrets.KCGIS_TILE_URL
        url = f'{base.rsplit("/tile", 1)[0]}/export?bbox={bbox}&format=png&size=256,256&f=image'
        response = requests.get(url, verify=False, timeout=10)
        if response.status_code == 200 and response.text:
            data = json.loads(response.text)
            if 'href' in data:
                img_response = requests.get(data['href'], verify=False, timeout=10)
                return img_response.content, 200, {'Content-Type': 'image/png'}
        return '', 503
    except Exception as e:
        print(f'KCGIS export proxy error: {e}')
        return '', 503


@app.route('/proxy/kcgis-img-tile/<int:z>/<int:y>/<int:x>')
def proxy_kcgis_img_tile(z, y, x):
    try:
        import requests
        base = config_secrets.KCGIS_IMG_TILE_URL
        url = f'{base}/{z}/{y}/{x}?f=pjson'
        response = requests.get(url, verify=False, timeout=10)
        return response.content, response.status_code, {
            'Content-Type': response.headers.get('content-type', 'image/png'),
            'Cache-Control': 'public, max-age=86400'
        }
    except Exception as e:
        print(f'KCGIS image tile proxy error: {e}')
        return '', 503


@app.route('/proxy/kcgis-vector/<path:tile_path>')
def proxy_kcgis_vector(tile_path):
    try:
        import requests
        base = config_secrets.KCGIS_VECTOR_TILE_URL
        url = f'{base.rsplit("/tile", 1)[0]}/tile/{tile_path}'
        response = requests.get(url, verify=False, timeout=10)
        return response.content, response.status_code, {'Content-Type': 'application/x-protobuf'}
    except Exception as e:
        print(f'KCGIS vector proxy error: {e}')
        return '', 503


@app.route('/api/health')
def health_check():
    """Health check endpoint with data freshness information"""
    try:
        from db import get_conn, fetch_one
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) as cnt FROM devices')
        device_count = fetch_one(cursor)['cnt']
        cursor.execute('SELECT COUNT(*) as cnt FROM readings')
        reading_count = fetch_one(cursor)['cnt']
        cursor.execute('SELECT MAX(recorded_at) as last_r FROM readings')
        last_reading = fetch_one(cursor)['last_r']
        cursor.execute('SELECT MAX(recorded_at) as last_w FROM weather_data')
        last_weather = fetch_one(cursor)['last_w']

        conn.close()

        uptime_seconds = (datetime.now() - app_start_time).total_seconds()

        return jsonify({
            'status': 'ok',
            'devices': device_count,
            'readings': reading_count,
            'last_reading': last_reading,
            'last_weather': last_weather,
            'cache_last_update': data_cache.get('last_update', 'unknown'),
            'uptime_seconds': int(uptime_seconds),
            'uptime_human': f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"
        })
    except Exception as e:
        logger.warning(f"Health check DB query failed (non-critical): {e}")
        uptime_seconds = (datetime.now() - app_start_time).total_seconds()
        return jsonify({
            'status': 'degraded',
            'message': str(e),
            'devices': len(data_cache.get('devices', [])),
            'cache_last_update': data_cache.get('last_update', 'unknown'),
            'uptime_seconds': int(uptime_seconds),
            'uptime_human': f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"
        })


@app.route('/api/status')
def system_status():
    """System status endpoint showing scheduler and cache info"""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'next_run': str(job.next_run_time) if job.next_run_time else 'paused'
        })
    
    return jsonify({
        'scheduler_running': scheduler.running,
        'jobs': jobs,
        'cache_keys': list(data_cache.keys()),
        'app_start_time': app_start_time.isoformat()
    })


@app.route('/api/monitor/status')
def monitor_status():
    from monitor_service import get_monitor_status
    return jsonify(get_monitor_status())


@app.route('/api/monitor/metrics')
def monitor_metrics():
    from monitor_service import get_monitor_metrics
    return get_monitor_metrics(), 200, {'Content-Type': 'text/plain; charset=utf-8'}


@app.route('/api/monitor/trace')
def monitor_traces():
    from monitor_service import get_traces
    return jsonify(get_traces())


@app.route('/api/monitor/trigger', methods=['POST'])
def monitor_trigger():
    from monitor_service import run_monitor_cycle
    result = run_monitor_cycle(data_cache)
    return jsonify(result)


# ===== 用户管理页面 =====

@app.route('/admin')
def admin_page():
    return render_template('admin.html')


@app.route('/api/ilink/qrcode', methods=['POST'])
def get_ilink_qrcode():
    """获取 iLink 登录二维码"""
    import requests
    import time
    
    # 重试逻辑
    for attempt in range(3):
        try:
            resp = requests.get(
                f"{config_secrets.ILINK_API_BASE}/ilink/bot/get_bot_qrcode",
                params={"bot_type": 3},
                timeout=30
            )
            data = resp.json()
            if data.get('ret') == 0 or data.get('qrcode'):
                return jsonify({
                    'qrcode_id': data.get('qrcode'),
                    'qrcode_url': data.get('qrcode_img_content', '')
                })
            else:
                logger.warning("QR code API returned: %s", data)
        except requests.Timeout:
            logger.warning("QR code API timeout, attempt %d/3", attempt + 1)
        except Exception as e:
            logger.warning("QR code API error: %s, attempt %d/3", e, attempt + 1)
        
        if attempt < 2:
            time.sleep(1)
    
    return jsonify({'error': '获取二维码失败，请重试'}), 500


@app.route('/api/ilink/status/<qrcode_id>')
def check_ilink_status(qrcode_id):
    """轮询扫码状态"""
    import requests
    try:
        resp = requests.get(
            f"{config_secrets.ILINK_API_BASE}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_id},
            headers={"iLink-App-ClientVersion": "1"},
            timeout=60
        )
        data = resp.json()
        result = {'status': data.get('status', '')}
        if data.get('status') == 'confirmed':
            result['bot_token'] = data.get('bot_token', '')
            result['base_url'] = data.get('baseurl', '')
            result['user_id'] = data.get('ilink_user_id', '')
        return jsonify(result)
    except requests.Timeout:
        return jsonify({'status': 'pending'})
    except Exception as e:
        logger.error("check_ilink_status error: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/ilink/verify')
def verify_ilink_connection():
    """验证当前连接状态 - 通过检查 bot 客户端状态，避免与轮询线程冲突"""
    from wechat_bot import _ilink_client
    token = os.getenv('ILINK_TOKEN', '')

    if not token:
        return jsonify({'connected': False, 'token_preview': '未设置', 'error': 'Token not configured'})

    bot_running = _ilink_client is not None and _ilink_client._running
    thread_alive = (bot_running
                    and _ilink_client._poll_thread is not None
                    and _ilink_client._poll_thread.is_alive())

    return jsonify({
        'connected': thread_alive,
        'bot_running': bot_running,
        'thread_alive': thread_alive,
        'token_preview': token[:20] + '...' if len(token) > 20 else token,
    })


@app.route('/api/ilink/update-token', methods=['POST'])
def update_ilink_token():
    """更新 .env 文件中的 ILINK_TOKEN"""
    try:
        data = request.json
        new_token = data.get('token', '').strip()
        
        if not new_token:
            logger.warning("update-token: empty token")
            return jsonify({'success': False, 'error': 'Token 不能为空'})
        
        logger.info("update-token: updating token...")
        
        # 更新 .env 文件
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 替换或添加 ILINK_TOKEN
            if 'ILINK_TOKEN=' in content:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if line.startswith('ILINK_TOKEN='):
                        lines[i] = f'ILINK_TOKEN={new_token}'
                        break
                content = '\n'.join(lines)
            else:
                content += f'\nILINK_TOKEN={new_token}\n'
            
            with open(env_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info("update-token: .env file updated")
        else:
            logger.warning("update-token: .env file not found")
        
        # 更新环境变量
        os.environ['ILINK_TOKEN'] = new_token
        logger.info("update-token: success")
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error("update-token error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ilink/test-send', methods=['POST'])
def test_ilink_send():
    """测试发送消息"""
    try:
        data = request.json
        message = data.get('message', '')
        user_id = data.get('user_id', '')
        
        if not message or not user_id:
            return jsonify({'success': False, 'error': '消息和用户 ID 不能为空'})
        
        from wechat_bot import _ilink_client
        if not _ilink_client:
            return jsonify({'success': False, 'error': 'iLink 客户端未初始化'})
        
        result = _ilink_client.send_message(user_id, message)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ilink/test-image', methods=['POST'])
def test_ilink_image():
    """测试发送图片"""
    try:
        data = request.json
        user_id = data.get('user_id', '')
        image_path = data.get('image_path', '')
        
        if not user_id or not image_path:
            return jsonify({'success': False, 'error': '用户 ID 和图片路径不能为空'})
        
        from wechat_bot import _ilink_client
        if not _ilink_client:
            return jsonify({'success': False, 'error': 'iLink 客户端未初始化'})
        
        import os
        if not os.path.exists(image_path):
            return jsonify({'success': False, 'error': f'图片不存在: {image_path}'})
        
        # 测试上传
        logger.info("Testing image upload: %s", image_path)
        encrypt_param, aes_key, file_size = _ilink_client.upload_image_to_cdn(image_path, user_id)
        logger.info("Upload result: encrypt_param=%s, aes_key=%s, file_size=%d",
                   encrypt_param, aes_key, file_size)
        
        # 测试发送
        result = _ilink_client.send_image(user_id, image_path)
        return jsonify({
            'success': True,
            'result': result,
            'upload_info': {
                'encrypt_param': encrypt_param,
                'aes_key': aes_key,
                'file_size': file_size,
            }
        })
    except Exception as e:
        logger.error("Test image error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


# ===== 用户管理 API =====

@app.route('/api/admin/users')
def get_users():
    """获取用户列表"""
    conn = get_admin_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, wechat_id, display_name, created_at, last_login FROM users WHERE username LIKE '%@im.wechat' OR wechat_id IS NOT NULL ORDER BY created_at DESC")
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'users': users})


@app.route('/api/admin/users', methods=['POST'])
def create_user():
    """创建用户（通过二维码扫描添加）"""
    try:
        data = request.json
        username = data.get('username', '').strip()  # 微信用户ID作为用户名
        role = data.get('role', 'user')
        display_name = data.get('display_name', username)
        
        if not username:
            return jsonify({'success': False, 'error': '用户ID不能为空'})
        
        conn = get_admin_db()
        cursor = conn.cursor()
        
        # 检查用户名是否已存在
        cursor.execute('SELECT COUNT(*) FROM users WHERE username = ?', (username,))
        if cursor.fetchone()[0] > 0:
            conn.close()
            return jsonify({'success': False, 'error': '用户已存在'})
        
        # 用户名就是微信ID，不需要密码
        cursor.execute(
            'INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
            (username, '', role, display_name)
        )
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        
        return jsonify({'success': True, 'user_id': user_id})
    except Exception as e:
        logger.error("Create user error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    """更新用户"""
    try:
        data = request.json
        conn = get_admin_db()
        cursor = conn.cursor()
        
        # 构建更新语句
        updates = []
        params = []
        
        if 'display_name' in data:
            updates.append('display_name = ?')
            params.append(data['display_name'])
        
        if updates:
            params.append(user_id)
            cursor.execute(f'UPDATE users SET {", ".join(updates)} WHERE id = ?', params)
            conn.commit()
        
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Update user error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    """删除用户"""
    try:
        conn = get_admin_db()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Delete user error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


# ===== 消息管理 API =====

@app.route('/api/admin/messages')
def get_messages():
    """获取消息日志"""
    conn = get_admin_db()
    cursor = conn.cursor()
    limit = request.args.get('limit', 50, type=int)
    cursor.execute('SELECT * FROM message_logs ORDER BY created_at DESC LIMIT ?', (limit,))
    messages = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'messages': messages})


@app.route('/api/admin/messages/send', methods=['POST'])
def send_message():
    """发送消息"""
    try:
        data = request.json
        receiver = data.get('receiver', '')
        message = data.get('message', '')
        message_type = data.get('type', 'text')
        
        if not receiver or not message:
            return jsonify({'success': False, 'error': '接收者和消息不能为空'})
        
        from wechat_bot import _ilink_client
        if not _ilink_client:
            return jsonify({'success': False, 'error': 'iLink 客户端未初始化'})
        
        result = _ilink_client.send_message(receiver, message)
        success = _ilink_client.is_success(result)
        status = 'sent' if success else 'failed'

        # 记录日志
        conn = get_admin_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO message_logs (sender, receiver, message, message_type, status) VALUES (?, ?, ?, ?, ?)',
            ('system', receiver, message, message_type, status)
        )
        conn.commit()
        conn.close()

        return jsonify({'success': success, 'result': result})
    except Exception as e:
        logger.error("Send message error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/messages/batch', methods=['POST'])
def batch_send_message():
    """批量发送消息"""
    try:
        data = request.json
        receivers = data.get('receivers', [])
        message = data.get('message', '')
        
        if not receivers or not message:
            return jsonify({'success': False, 'error': '接收者列表和消息不能为空'})
        
        from wechat_bot import _ilink_client
        if not _ilink_client:
            return jsonify({'success': False, 'error': 'iLink 客户端未初始化'})
        
        results = []
        success_count = 0
        fail_count = 0
        
        for receiver in receivers:
            try:
                result = _ilink_client.send_message(receiver, message)
                success = _ilink_client.is_success(result)

                if success:
                    success_count += 1
                    results.append({'receiver': receiver, 'success': True})
                else:
                    fail_count += 1
                    results.append({'receiver': receiver, 'success': False,
                                   'error': result.get('errmsg', f"ret={result.get('ret')}")})
                    continue

                # 记录日志
                conn = get_admin_db()
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT INTO message_logs (sender, receiver, message, message_type, status) VALUES (?, ?, ?, ?, ?)',
                    ('system', receiver, message, 'text', 'sent')
                )
                conn.commit()
                conn.close()
            except Exception as e:
                results.append({'receiver': receiver, 'success': False, 'error': str(e)})
                fail_count += 1
        
        return jsonify({
            'success': True,
            'total': len(receivers),
            'success_count': success_count,
            'fail_count': fail_count,
            'results': results
        })
    except Exception as e:
        logger.error("Batch send error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


# ===== 告警联系人 API =====

@app.route('/api/admin/contacts')
def get_contacts():
    """获取告警联系人列表"""
    conn = get_admin_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM alert_contacts ORDER BY created_at DESC')
    contacts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'contacts': contacts})


@app.route('/api/admin/contacts', methods=['POST'])
def create_contact():
    """添加告警联系人"""
    try:
        data = request.json
        name = data.get('name', '')
        wechat_id = data.get('wechat_id', '').strip()
        alert_types = json.dumps(data.get('alert_types', ['device_alert', 'daily_report']))
        
        if not wechat_id:
            return jsonify({'success': False, 'error': '微信ID不能为空'})
        
        conn = get_admin_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO alert_contacts (name, wechat_id, alert_types) VALUES (?, ?, ?)',
            (name, wechat_id, alert_types)
        )
        conn.commit()
        contact_id = cursor.lastrowid
        conn.close()
        
        return jsonify({'success': True, 'contact_id': contact_id})
    except Exception as e:
        logger.error("Create contact error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/contacts/<int:contact_id>', methods=['PUT'])
def update_contact(contact_id):
    """更新告警联系人"""
    try:
        data = request.json
        conn = get_admin_db()
        cursor = conn.cursor()
        
        updates = []
        params = []
        
        if 'name' in data:
            updates.append('name = ?')
            params.append(data['name'])
        
        if 'wechat_id' in data:
            updates.append('wechat_id = ?')
            params.append(data['wechat_id'])
        
        if 'alert_types' in data:
            updates.append('alert_types = ?')
            params.append(json.dumps(data['alert_types']))
        
        if 'is_active' in data:
            updates.append('is_active = ?')
            params.append(1 if data['is_active'] else 0)
        
        if updates:
            params.append(contact_id)
            cursor.execute(f'UPDATE alert_contacts SET {", ".join(updates)} WHERE id = ?', params)
            conn.commit()
        
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Update contact error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/contacts/<int:contact_id>', methods=['DELETE'])
def delete_contact(contact_id):
    """删除告警联系人"""
    try:
        conn = get_admin_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM alert_contacts WHERE id = ?', (contact_id,))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Delete contact error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


# ===== 系统配置 API =====

@app.route('/api/admin/config')
def get_config():
    """获取系统配置"""
    conn = get_admin_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM system_config ORDER BY key')
    configs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify({'configs': configs})


@app.route('/api/admin/config', methods=['PUT'])
def update_config():
    """更新系统配置"""
    try:
        data = request.json
        conn = get_admin_db()
        cursor = conn.cursor()
        
        for key, value in data.items():
            cursor.execute(
                'UPDATE system_config SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?',
                (str(value), key)
            )
        
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        logger.error("Update config error: %s", e)
        return jsonify({'success': False, 'error': str(e)})


# ===== 统计数据 API =====

@app.route('/api/admin/stats/messages')
def get_message_stats():
    """获取消息统计"""
    conn = get_admin_db()
    cursor = conn.cursor()
    
    # 今日消息数
    cursor.execute("SELECT COUNT(*) as cnt FROM message_logs WHERE date(created_at) = date('now')")
    today_count = cursor.fetchone()['cnt']
    
    # 总消息数
    cursor.execute('SELECT COUNT(*) as cnt FROM message_logs')
    total_count = cursor.fetchone()['cnt']
    
    # 按类型统计
    cursor.execute('SELECT message_type, COUNT(*) as cnt FROM message_logs GROUP BY message_type')
    type_stats = {row['message_type']: row['cnt'] for row in cursor.fetchall()}
    
    # 最近7天每天的消息数
    cursor.execute('''
        SELECT date(created_at) as day, COUNT(*) as cnt 
        FROM message_logs 
        WHERE created_at >= datetime('now', '-7 days')
        GROUP BY date(created_at)
        ORDER BY day
    ''')
    daily_stats = [{'date': row['day'], 'count': row['cnt']} for row in cursor.fetchall()]
    
    conn.close()
    
    return jsonify({
        'today_count': today_count,
        'total_count': total_count,
        'type_stats': type_stats,
        'daily_stats': daily_stats
    })


@app.route('/api/admin/stats/system')
def get_system_stats():
    """获取系统运行状态"""
    import psutil
    
    uptime_seconds = (datetime.now() - app_start_time).total_seconds()
    
    return jsonify({
        'uptime_seconds': int(uptime_seconds),
        'uptime_human': f"{int(uptime_seconds // 3600)}小时 {int((uptime_seconds % 3600) // 60)}分钟",
        'memory_usage': psutil.virtual_memory().percent if 'psutil' in dir() else 'N/A',
        'scheduler_running': scheduler.running,
        'cache_keys': list(data_cache.keys()),
        'device_count': len(data_cache.get('devices', [])),
        'last_update': data_cache.get('last_update', 'unknown')
    })


if __name__ == '__main__':
    init_data()
    data_cache['last_update'] = datetime.now().isoformat()
    scheduler.start()
    logger.info("Scheduler started with periodic refresh jobs")

    # 启动微信机器人（iLink Bot API）
    try:
        from wechat_bot import start_wechat_bot
        wechat_bot_thread = start_wechat_bot(data_cache)
        if wechat_bot_thread:
            logger.info("WeChat bot started via iLink API")
    except Exception as e:
        logger.error("WeChat bot failed to start: %s", e)

    # 启动自动监测服务
    try:
        from monitor_service import start_monitor
        monitor_interval = int(__import__('os').getenv('MONITOR_INTERVAL', '300'))
        start_monitor(data_cache, interval_seconds=monitor_interval)
        logger.info("Monitor service started (interval=%ds)", monitor_interval)
    except Exception as e:
        logger.error("Monitor service failed to start: %s", e)

    # 启动时后台增量回填 + 液位分析刷新
    backfill_thread = threading.Thread(target=startup_backfill_and_analyze, daemon=True, name="startup-backfill")
    backfill_thread.start()
    logger.info("Startup backfill thread started")

    app.run(debug=False, host=os.getenv('HOST', '0.0.0.0'), port=int(os.getenv('PORT', '5000')))
