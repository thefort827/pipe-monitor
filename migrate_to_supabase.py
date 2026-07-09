"""
Migrate SQLite data to Supabase PostgreSQL
"""
import sqlite3
import psycopg2
import os

DATABASE_URL = os.getenv('DATABASE_URL', '')
SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pipe_device.db')

print("Connecting to Supabase...")
pg_conn = psycopg2.connect(DATABASE_URL)
pg_cur = pg_conn.cursor()

print("Connecting to SQLite...")
sq_conn = sqlite3.connect(SQLITE_PATH)
sq_cur = sq_conn.cursor()

# Create tables
print("Creating tables...")

pg_cur.execute("DROP TABLE IF EXISTS readings CASCADE")
pg_cur.execute("DROP TABLE IF EXISTS devices CASCADE")
pg_cur.execute("DROP TABLE IF EXISTS weather_data CASCADE")
pg_cur.execute("DROP TABLE IF EXISTS fetch_log CASCADE")
pg_cur.execute("DROP TABLE IF EXISTS backfill_state CASCADE")

pg_cur.execute("""
CREATE TABLE devices (
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

pg_cur.execute("""
CREATE TABLE readings (
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
pg_cur.execute("CREATE INDEX idx_readings_device ON readings(device_id, recorded_at DESC)")

pg_cur.execute("""
CREATE TABLE weather_data (
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

pg_cur.execute("""
CREATE TABLE fetch_log (
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

pg_cur.execute("""
CREATE TABLE backfill_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_synced_time TEXT,
    last_run_at TEXT,
    total_fetched INTEGER DEFAULT 0,
    total_inserted INTEGER DEFAULT 0,
    status TEXT DEFAULT 'idle'
)
""")

pg_conn.commit()
print("Tables created!")

# Migrate devices
print("Migrating devices...")
devices = sq_cur.execute("SELECT * FROM devices").fetchall()
cols = [d[1] for d in sq_cur.execute("PRAGMA table_info(devices)").fetchall()]
for row in devices:
    vals = dict(zip([d[1] for d in sq_cur.execute("PRAGMA table_info(devices)").fetchall()], row))
    pg_cur.execute("""
        INSERT INTO devices (device_id, name, area_name, device_type, manufacturers, address, longitude, latitude, first_seen, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_id) DO NOTHING
    """, (vals['device_id'], vals['name'], vals['area_name'], vals['device_type'], 
          vals['manufacturers'], vals['address'], vals['longitude'], vals['latitude'],
          vals['first_seen'], vals['last_seen']))
pg_conn.commit()
print(f"  Migrated {len(devices)} devices")

# Migrate readings in batches
print("Migrating readings...")
sq_cur.execute("SELECT COUNT(*) FROM readings")
total = sq_cur.fetchone()[0]
print(f"  Total readings: {total}")

batch_size = 10000
migrated = 0
sq_cur.execute("SELECT * FROM readings")
while True:
    rows = sq_cur.fetchmany(batch_size)
    if not rows:
        break
    for row in rows:
        pg_cur.execute("""
            INSERT INTO readings (device_id, recorded_at, liquid_level, ammonia_n, cod, voltage, isonline, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, row[1:9])  # Skip id, skip extra columns
    pg_conn.commit()
    migrated += len(rows)
    print(f"  Progress: {migrated}/{total}")

print(f"  Migrated {migrated} readings")

# Migrate weather_data
print("Migrating weather_data...")
try:
    weather = sq_cur.execute("SELECT * FROM weather_data").fetchall()
    for row in weather:
        pg_cur.execute("""
            INSERT INTO weather_data (recorded_at, latitude, longitude, rainfall_mm, temp_c, humidity, source, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, row[1:9])
    pg_conn.commit()
    print(f"  Migrated {len(weather)} weather records")
except Exception as e:
    print(f"  Skipped weather_data: {e}")

pg_conn.close()
sq_conn.close()
print("\nMigration complete!")
