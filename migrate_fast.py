"""
Fast migration: SQLite -> Supabase PostgreSQL
"""
import os
import sqlite3
import psycopg2

DATABASE_URL = os.getenv('DATABASE_URL', '')
SQLITE_PATH = 'pipe_device.db'

pg = psycopg2.connect(DATABASE_URL)
pg_cur = pg.cursor()
sq = sqlite3.connect(SQLITE_PATH)
sq_cur = sq.cursor()

# Create tables
print("Creating tables...")
for ddl in [
    "DROP TABLE IF EXISTS readings CASCADE",
    "DROP TABLE IF EXISTS devices CASCADE",
    "CREATE TABLE devices (id SERIAL PRIMARY KEY, device_id TEXT UNIQUE, name TEXT, area_name TEXT, device_type TEXT, manufacturers TEXT, address TEXT, longitude REAL, latitude REAL, first_seen TEXT, last_seen TEXT)",
    "CREATE TABLE readings (id SERIAL PRIMARY KEY, device_id TEXT, recorded_at TEXT, liquid_level REAL, ammonia_n REAL, cod REAL, voltage REAL, isonline TEXT, created_at TEXT, temperature REAL)",
    "CREATE INDEX idx_readings_device ON readings(device_id, recorded_at DESC)"
]:
    pg_cur.execute(ddl)
pg.commit()

# Migrate devices
print("Migrating devices...")
for row in sq_cur.execute("SELECT device_id, name, area_name, device_type, manufacturers, address, longitude, latitude, first_seen, last_seen FROM devices"):
    pg_cur.execute("INSERT INTO devices (device_id, name, area_name, device_type, manufacturers, address, longitude, latitude, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", row)
pg.commit()
print(f"  Done: {sq_cur.execute('SELECT COUNT(*) FROM devices').fetchone()[0]} devices")

# Migrate readings in large batches
print("Migrating readings...")
total = sq_cur.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
sq_cur.execute("SELECT device_id, recorded_at, liquid_level, ammonia_n, cod, voltage, isonline, created_at FROM readings")
batch = []
count = 0
for row in sq_cur:
    batch.append(row)
    if len(batch) >= 5000:
        args_str = ','.join(pg_cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s)", r).decode() for r in batch)
        pg_cur.execute(f"INSERT INTO readings (device_id, recorded_at, liquid_level, ammonia_n, cod, voltage, isonline, created_at) VALUES {args_str}")
        pg.commit()
        count += len(batch)
        print(f"  {count}/{total}")
        batch = []
if batch:
    args_str = ','.join(pg_cur.mogrify("(%s,%s,%s,%s,%s,%s,%s,%s)", r).decode() for r in batch)
    pg_cur.execute(f"INSERT INTO readings (device_id, recorded_at, liquid_level, ammonia_n, cod, voltage, isonline, created_at) VALUES {args_str}")
    pg.commit()
    count += len(batch)
    print(f"  {count}/{total}")

pg.close()
sq.close()
print("Migration complete!")
