-- Supabase 数据库初始化脚本
-- 在 Supabase Dashboard → SQL Editor 中运行此脚本

-- 设备表
CREATE TABLE IF NOT EXISTS devices (
    id SERIAL PRIMARY KEY,
    device_id TEXT UNIQUE,
    name TEXT,
    area_name TEXT,
    device_type TEXT,
    manufacturers TEXT,
    address TEXT,
    longitude REAL,
    latitude REAL,
    first_seen TEXT,
    last_seen TEXT
);

-- 读数表
CREATE TABLE IF NOT EXISTS readings (
    id SERIAL PRIMARY KEY,
    device_id TEXT,
    recorded_at TEXT,
    liquid_level REAL,
    ammonia_n REAL,
    cod REAL,
    voltage REAL,
    isonline TEXT,
    created_at TEXT,
    temperature REAL,
    status INTEGER,
    threshold_exceed TEXT,
    getvaluetime TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id, recorded_at DESC);

-- 天气数据表
CREATE TABLE IF NOT EXISTS weather_data (
    id SERIAL PRIMARY KEY,
    recorded_at TEXT,
    latitude REAL,
    longitude REAL,
    rainfall_mm REAL,
    temp_c REAL,
    humidity INTEGER,
    source TEXT,
    created_at TEXT
);

-- 获取日志表
CREATE TABLE IF NOT EXISTS fetch_log (
    id SERIAL PRIMARY KEY,
    started_at TEXT,
    time_start TEXT,
    time_end TEXT,
    records_fetched INTEGER DEFAULT 0,
    records_inserted INTEGER DEFAULT 0,
    status TEXT,
    error_msg TEXT
);

-- 回填状态表
CREATE TABLE IF NOT EXISTS backfill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_synced_time TEXT,
    last_run_at TEXT,
    total_fetched INTEGER DEFAULT 0,
    total_inserted INTEGER DEFAULT 0,
    status TEXT DEFAULT 'idle'
);

-- 管点表
CREATE TABLE IF NOT EXISTS pipe_nodes (
    id SERIAL PRIMARY KEY,
    point_id TEXT UNIQUE,
    pipe_type TEXT,
    sub_type TEXT,
    feature TEXT,
    ground_elev REAL,
    well_bottom_elev REAL,
    depth REAL,
    lon REAL,
    lat REAL
);

-- 管段表
CREATE TABLE IF NOT EXISTS pipe_segments (
    id SERIAL PRIMARY KEY,
    start_id TEXT,
    end_id TEXT,
    sub_type TEXT,
    diameter TEXT,
    length REAL
);
