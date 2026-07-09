import requests
import sqlite3
from datetime import datetime, timedelta
import config

CACHE_HOURS = 6


def get_precipitation(lat, lon, past_days=7):
    db_result = get_from_database(lat, lon, past_days)
    if db_result:
        return db_result

    api_result = get_from_api(lat, lon, past_days)
    if api_result:
        save_to_database(lat, lon, api_result)
    return api_result


def get_from_database(lat, lon, past_days=7):
    try:
        from db import get_conn, fetch_all
        conn = get_conn()
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=CACHE_HOURS)).isoformat()
        if config.DB_TYPE == 'postgresql':
            cursor.execute("""
                SELECT recorded_at, rainfall_mm, temp_c, humidity
                FROM weather_data
                WHERE latitude BETWEEN %s AND %s
                AND longitude BETWEEN %s AND %s
                AND recorded_at >= %s
                ORDER BY recorded_at DESC
            """, (lat - 0.1, lat + 0.1, lon - 0.1, lon + 0.1, cutoff))
        else:
            cursor.execute("""
                SELECT recorded_at, rainfall_mm, temp_c, humidity
                FROM weather_data
                WHERE latitude BETWEEN ? AND ?
                AND longitude BETWEEN ? AND ?
                AND recorded_at >= ?
                ORDER BY recorded_at DESC
            """, (lat - 0.1, lat + 0.1, lon - 0.1, lon + 0.1, cutoff))
        rows = cursor.fetchall()
        conn.close()

        if rows:
            result = []
            for row in rows:
                if config.DB_TYPE == 'postgresql':
                    result.append({
                        "time": row[0],
                        "precipitation": row[1] or 0.0,
                        "temperature": row[2],
                        "humidity": row[3]
                    })
                else:
                    result.append({
                        "time": row[0],
                        "precipitation": row[1] or 0.0,
                        "temperature": row[2],
                        "humidity": row[3]
                    })
            return result
        return None
    except Exception as e:
        print(f"Database read error: {e}")
        return None


def save_to_database(lat, lon, precip_data):
    try:
        from db import get_conn
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weather_data (
                id SERIAL PRIMARY KEY,
                recorded_at TIMESTAMP,
                latitude REAL,
                longitude REAL,
                rainfall_mm REAL,
                temp_c REAL,
                humidity INTEGER,
                source TEXT,
                created_at TEXT
            )
        """)

        for item in precip_data:
            time_str = item.get("time", "")
            precip = item.get("precipitation", 0.0)
            temp = item.get("temperature")
            humidity = item.get("humidity")

            if config.DB_TYPE == 'postgresql':
                cursor.execute("""
                    INSERT INTO weather_data (recorded_at, latitude, longitude, rainfall_mm, temp_c, humidity, source, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (time_str, lat, lon, precip, temp, humidity, "open-meteo", config.now_sh().isoformat()))
            else:
                cursor.execute("""
                    INSERT OR REPLACE INTO weather_data
                    (recorded_at, latitude, longitude, rainfall_mm, temp_c, humidity, source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (time_str, lat, lon, precip, temp, humidity, "open-meteo", config.now_sh().isoformat()))

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Database save error: {e}")


def get_from_api(lat, lon, past_days=7):
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation,temperature_2m,relative_humidity_2m",
            "past_days": past_days,
            "timezone": "Asia/Shanghai"
        }
        resp = requests.get(config.OPEN_METEO_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        precip = hourly.get("precipitation", [])
        temps = hourly.get("temperature_2m", [])
        humidity = hourly.get("relative_humidity_2m", [])

        result = []
        for i, t in enumerate(times):
            result.append({
                "time": t,
                "precipitation": precip[i] if i < len(precip) else 0.0,
                "temperature": temps[i] if i < len(temps) else None,
                "humidity": humidity[i] if i < len(humidity) else None
            })
        return result
    except Exception as e:
        print(f"API call error: {e}")
        return []


def get_recent_precipitation_summary(lat, lon, hours=24):
    precip_data = get_precipitation(lat, lon, past_days=7)
    if not precip_data:
        return {"total_24h": 0, "total_7d": 0, "hourly": []}

    now = datetime.now()
    cutoff_24h = now - timedelta(hours=hours)

    total_24h = 0
    total_7d = 0
    hourly_recent = []

    for item in precip_data:
        try:
            t_str = item["time"].replace("T", " ")
            t = datetime.fromisoformat(t_str[:19])
            p = item["precipitation"]
            total_7d += p
            if t >= cutoff_24h:
                total_24h += p
                hourly_recent.append(item)
        except:
            continue

    return {
        "total_24h": round(total_24h, 2),
        "total_7d": round(total_7d, 2),
        "hourly": hourly_recent[-24:]
    }


def find_precipitation_for_time(precip_data, target_time_str):
    try:
        target_str = target_time_str.replace(" ", "T")[:13]
        for item in precip_data:
            item_time = item["time"][:13]
            if item_time == target_str:
                return item["precipitation"]
        return 0.0
    except:
        return 0.0


def get_forecast_next_hours(lat, lon, hours=6):
    """获取未来几小时天气预报（用于预测降雨触发推送）"""
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "precipitation,temperature_2m,relative_humidity_2m",
            "forecast_hours": hours,
            "timezone": "Asia/Shanghai"
        }
        resp = requests.get(config.OPEN_METEO_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        precip = hourly.get("precipitation", [])
        temps = hourly.get("temperature_2m", [])
        humidity = hourly.get("relative_humidity_2m", [])

        result = []
        for i, t in enumerate(times):
            result.append({
                "time": t,
                "precipitation": precip[i] if i < len(precip) else 0.0,
                "temperature": temps[i] if i < len(temps) else None,
                "humidity": humidity[i] if i < len(humidity) else None
            })
        return result
    except Exception as e:
        print(f"Forecast API error: {e}")
        return []


def check_heavy_rain_forecast(lat, lon, threshold=10.0):
    """检查未来1小时是否有大型降雨预报，返回 (is_heavy, max_precip, forecast_data)"""
    forecast = get_forecast_next_hours(lat, lon, hours=2)
    if not forecast:
        return False, 0.0, []

    now = datetime.now()
    max_precip = 0.0
    heavy_found = False
    for item in forecast:
        try:
            t_str = item["time"].replace("T", " ")
            t = datetime.fromisoformat(t_str[:19])
            p = item["precipitation"]
            if t >= now and p > max_precip:
                max_precip = p
            if t >= now and p >= threshold:
                heavy_found = True
        except:
            continue
    return heavy_found, max_precip, forecast
