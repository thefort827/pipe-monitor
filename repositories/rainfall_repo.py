"""
降雨数据仓库 — 封装数据库访问，自动降级
业务层只调 RainfallRepository.get_recent_periods()，不感知底层是 PG 还是 SQLite
"""
import sqlite3
import logging
from collections import defaultdict
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


class RainfallRepository:
    @staticmethod
    def get_recent_periods(hours=72):
        """获取近期降雨时段，返回 (rainfall_hours: set, periods: list)"""
        try:
            from db import get_conn, fetch_all, hours_ago
            conn = get_conn()
            cursor = conn.cursor()
            time_cond = hours_ago(hours)
            if config.DB_TYPE == 'postgresql':
                cursor.execute(f"""
                    SELECT recorded_at, rainfall_mm FROM weather_data
                    WHERE rainfall_mm IS NOT NULL AND rainfall_mm > 0.1
                    AND recorded_at >= {time_cond}
                    ORDER BY recorded_at
                """)
            else:
                cursor.execute("""
                    SELECT recorded_at, rainfall_mm FROM weather_data
                    WHERE rainfall_mm IS NOT NULL AND rainfall_mm > 0.1
                    AND recorded_at >= datetime('now', ? || ' hours', 'localtime')
                    ORDER BY recorded_at
                """, (f'-{hours}',))
            rows = fetch_all(cursor, conn)
            conn.close()
            if rows:
                return RainfallRepository._parse_rows(rows)
        except Exception as e:
            logger.debug("Primary DB rainfall query failed: %s", e)

        # 降级到 REST API
        try:
            import requests as req
            import os
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            url = f"{os.getenv('SUPABASE_URL', '')}/rest/v1/weather_data"
            headers = {
                "apikey": os.getenv('SUPABASE_KEY', ''),
                "Authorization": f"Bearer {os.getenv('SUPABASE_KEY', '')}"
            }
            params = {
                "select": "recorded_at,rainfall_mm",
                "rainfall_mm": "gt.0.1",
                "recorded_at": f"gte.{cutoff}",
                "order": "recorded_at"
            }
            r = req.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            rows = r.json()
            if rows:
                return RainfallRepository._parse_rows(rows)
        except Exception as e:
            logger.debug("REST API rainfall fallback failed: %s", e)

        # 降级到 SQLite 文件
        return RainfallRepository._sqlite_fallback(hours)

    @staticmethod
    def _sqlite_fallback(hours):
        """直接查 SQLite 文件作为最终降级"""
        try:
            import os
            db_path = getattr(config, 'DB_PATH', None) or os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'pipe_device.db')
            conn = sqlite3.connect(db_path, timeout=10)
            cursor = conn.cursor()
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
                SELECT recorded_at, rainfall_mm FROM weather_data
                WHERE rainfall_mm IS NOT NULL AND rainfall_mm > 0.1
                AND recorded_at >= ?
                ORDER BY recorded_at
            """, (cutoff,))
            rows = cursor.fetchall()
            conn.close()
            if rows:
                return RainfallRepository._parse_rows(
                    [{'recorded_at': r[0], 'rainfall_mm': r[1]} for r in rows]
                )
        except Exception as e:
            logger.error("SQLite rainfall fallback failed: %s", e)
        return set(), []

    @staticmethod
    def _parse_rows(rows):
        """统一解析降雨数据行"""
        rainfall_hours = set()
        hourly_rain = defaultdict(float)

        for r in rows:
            try:
                t_str = str(r.get('recorded_at', '')).replace('T', ' ')[:13]
                mm = float(r.get('rainfall_mm') or 0)
                if mm > 0.1:
                    rainfall_hours.add(t_str)
                    hourly_rain[t_str] += mm
            except Exception:
                continue

        sorted_hours = sorted(rainfall_hours)
        periods = []
        if sorted_hours:
            start = sorted_hours[0]
            end = sorted_hours[0]
            total = hourly_rain.get(sorted_hours[0], 0)
            for h in sorted_hours[1:]:
                if h <= end:
                    total += hourly_rain.get(h, 0)
                    end = h
                else:
                    if total > 1.0:
                        periods.append((start, end, round(total, 1)))
                    start = h
                    end = h
                    total = hourly_rain.get(h, 0)
            if total > 1.0:
                periods.append((start, end, round(total, 1)))

        return rainfall_hours, periods
