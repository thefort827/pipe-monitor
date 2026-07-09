import os

# Database: use PostgreSQL if DATABASE_URL is set, otherwise SQLite
DATABASE_URL = os.getenv('DATABASE_URL', '')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pipe_device.db')
if DATABASE_URL and DATABASE_URL.startswith('postgres'):
    DB_TYPE = 'postgresql'
else:
    DB_TYPE = 'sqlite'

XLSX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '城西闪传_修复版_去重.xlsx')

THRESHOLDS = {
    'liquid_level': 5.0,
    'cod': 500.0,
    'ammonia_n': 100.0,
}

BIND_DISTANCE_WARN = 100
BIND_DISTANCE_MAX = 200

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

OVERFLOW_THRESHOLDS = {
    'critical': 0.0,
    'high': 0.5,
    'medium': 1.0,
}

HYDRAULIC_SLOPE_THRESHOLD = -0.001

PIPE_FULLNESS_THRESHOLDS = {
    'high': 0.8,
    'medium': 0.5,
}

BACKFILL_CHUNK_DAYS = 30
BACKFILL_BATCH_SIZE = 5000
BACKFILL_MAX_CHUNKS_PER_CYCLE = 10
BACKFILL_START_DATE = '2025-07-09'
BACKFILL_SLEEP_BETWEEN = 1

PUSH_LEVEL_CHANGE_THRESHOLD = 0.3
PUSH_RAINFALL_FORECAST_THRESHOLD = 10.0
PUSH_MIN_INTERVAL = 300

TIMEZONE = 'Asia/Shanghai'

from datetime import datetime, timezone, timedelta
SH_TZ = timezone(timedelta(hours=8))

def now_sh():
    """Return current time in Asia/Shanghai as naive datetime."""
    return datetime.now(SH_TZ).replace(tzinfo=None)

# 水位接近地面高程预警阈值（gap = ground_elev - water_level_elev）
# 正值=安全距离，负值=已溢出
GROUND_PROXIMITY_THRESHOLDS = [
    {'max_gap': 0.0, 'severity': 'critical', 'label': '已溢出'},
    {'max_gap': 0.3, 'severity': 'critical', 'label': '即将溢出'},
    {'max_gap': 0.5, 'severity': 'high',     'label': '高风险'},
    {'max_gap': 1.0, 'severity': 'medium',   'label': '需关注'},
]

# 媒体处理配置
MEDIA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media')
MEDIA_CONFIG = {
    'storage_path': MEDIA_ROOT,
    'max_image_size': 10 * 1024 * 1024,    # 10MB
    'max_voice_size': 5 * 1024 * 1024,     # 5MB
    'max_video_size': 50 * 1024 * 1024,    # 50MB
    'max_file_size': 20 * 1024 * 1024,     # 20MB
    'retention_days': 30,                   # 媒体文件保留天数
}
