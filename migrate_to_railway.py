"""
迁移 Supabase 数据到 Railway PostgreSQL
"""
import os
import requests
import psycopg2
from datetime import datetime

# Railway PostgreSQL
RAILWAY_DB_URL = os.getenv('DATABASE_URL', '')

# Supabase REST API
SUPABASE_URL = os.getenv('SUPABASE_URL', '')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
HEADERS = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}

def supabase_get(table, limit=1000):
    """从 Supabase 读取数据"""
    url = f'{SUPABASE_URL}/rest/v1/{table}?limit={limit}'
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def create_tables(conn):
    """创建数据库表"""
    cur = conn.cursor()
    
    cur.execute("""
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
        )
    """)
    
    cur.execute("""
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
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_readings_device ON readings(device_id, recorded_at DESC)")
    
    cur.execute("""
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
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            id SERIAL PRIMARY KEY,
            started_at TEXT,
            time_start TEXT,
            time_end TEXT,
            records_fetched INTEGER DEFAULT 0,
            records_inserted INTEGER DEFAULT 0,
            status TEXT,
            error_msg TEXT
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS backfill_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_synced_time TEXT,
            last_run_at TEXT,
            total_fetched INTEGER DEFAULT 0,
            total_inserted INTEGER DEFAULT 0,
            status TEXT DEFAULT 'idle'
        )
    """)
    
    cur.execute("""
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
        )
    """)
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pipe_segments (
            id SERIAL PRIMARY KEY,
            start_id TEXT,
            end_id TEXT,
            sub_type TEXT,
            diameter TEXT,
            length REAL
        )
    """)
    
    conn.commit()
    print("Tables created!")

def migrate_devices(conn, devices):
    """迁移设备数据"""
    cur = conn.cursor()
    inserted = 0
    for d in devices:
        try:
            cur.execute("""
                INSERT INTO devices (device_id, name, area_name, device_type, manufacturers, address, longitude, latitude, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (device_id) DO UPDATE SET 
                    name=EXCLUDED.name, longitude=EXCLUDED.longitude, latitude=EXCLUDED.latitude
            """, (d.get('device_id'), d.get('name'), d.get('area_name'), d.get('device_type'),
                  d.get('manufacturers'), d.get('address'), d.get('longitude'), d.get('latitude'),
                  d.get('first_seen'), d.get('last_seen')))
            inserted += 1
        except Exception as e:
            print(f"Skip device {d.get('device_id')}: {e}")
    conn.commit()
    return inserted

def migrate_readings(conn, readings):
    """迁移读数数据"""
    cur = conn.cursor()
    inserted = 0
    for r in readings:
        try:
            cur.execute("""
                INSERT INTO readings (device_id, recorded_at, liquid_level, ammonia_n, cod, voltage, isonline, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (r.get('device_id'), r.get('recorded_at'), r.get('liquid_level'), r.get('ammonia_n'),
                  r.get('cod'), r.get('voltage'), r.get('isonline'), r.get('created_at')))
            inserted += 1
        except Exception as e:
            pass
    conn.commit()
    return inserted

def main():
    print("Connecting to Railway PostgreSQL...")
    conn = psycopg2.connect(RAILWAY_DB_URL)
    
    print("Creating tables...")
    create_tables(conn)
    
    print("Fetching devices from Supabase...")
    devices = supabase_get('devices', 1000)
    print(f"  Got {len(devices)} devices")
    
    print("Migrating devices...")
    n = migrate_devices(conn, devices)
    print(f"  Migrated {n} devices")
    
    print("Fetching readings from Supabase...")
    readings = supabase_get('readings', 5000)
    print(f"  Got {len(readings)} readings")
    
    print("Migrating readings...")
    n = migrate_readings(conn, readings)
    print(f"  Migrated {n} readings")
    
    conn.close()
    print("Migration complete!")

if __name__ == '__main__':
    main()
