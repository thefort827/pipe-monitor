"""
数据回填模块
- 全量历史回填（从指定日期开始）
- 增量同步（从上次同步时间到现在）
- 支持断点续传，记录到 backfill_state 和 fetch_log 表
- 依赖注入：通过 get_conn() 连接工厂获取数据库连接，不依赖 config.DB_PATH
"""
import time
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

BJ_TZ = ZoneInfo('Asia/Shanghai')


class DataBackfill:
    def __init__(self, kcgis_service, get_conn=None):
        """
        Args:
            kcgis_service: KCGIS API 客户端
            get_conn: 连接工厂函数，返回一个数据库连接（需支持 .cursor() / .commit() / .close()）
                      如果为 None，则回退到 sqlite3 连接本地数据库
        """
        self.kcgis = kcgis_service
        self._get_conn = get_conn or self._default_sqlite_conn
        self._ensure_tables()

    @staticmethod
    def _default_sqlite_conn():
        return __import__('sqlite3').connect(config.DB_PATH, timeout=10)

    def _ensure_tables(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backfill_state (
                id INTEGER PRIMARY KEY,
                last_synced_time TEXT,
                last_run_at TEXT,
                total_fetched INTEGER DEFAULT 0,
                total_inserted INTEGER DEFAULT 0,
                status TEXT DEFAULT 'idle'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                finished_at TEXT,
                time_start TEXT,
                time_end TEXT,
                records_fetched INTEGER,
                records_inserted INTEGER,
                status TEXT,
                error_msg TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _get_state(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM backfill_state ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'id': row[0],
                'last_synced_time': row[1],
                'last_run_at': row[2],
                'total_fetched': row[3],
                'total_inserted': row[4],
                'status': row[5]
            }
        return None

    def _save_state(self, state):
        conn = self._get_conn()
        cursor = conn.cursor()
        existing = self._get_state()
        now_str = datetime.now(BJ_TZ).isoformat()
        if existing:
            cursor.execute("""
                UPDATE backfill_state
                SET last_synced_time=?, last_run_at=?, total_fetched=?, total_inserted=?, status=?
                WHERE id=?
            """, (state.get('last_synced_time'), now_str,
                  state.get('total_fetched', 0), state.get('total_inserted', 0),
                  state.get('status', 'idle'), existing['id']))
        else:
            cursor.execute("""
                INSERT INTO backfill_state (last_synced_time, last_run_at, total_fetched, total_inserted, status)
                VALUES (?, ?, ?, ?, ?)
            """, (state.get('last_synced_time'), now_str,
                  state.get('total_fetched', 0), state.get('total_inserted', 0),
                  state.get('status', 'idle')))
        conn.commit()
        conn.close()

    def _get_latest_reading_time(self):
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(recorded_at) FROM readings")
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            try:
                return datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ_TZ)
            except ValueError:
                try:
                    return datetime.fromisoformat(row[0][:19]).replace(tzinfo=BJ_TZ)
                except:
                    pass
        return None

    def _log_fetch(self, time_start, time_end, fetched, inserted, status, error=''):
        conn = self._get_conn()
        cursor = conn.cursor()
        now_str = datetime.now(BJ_TZ).isoformat()
        cursor.execute("""
            INSERT INTO fetch_log (started_at, time_start, time_end, records_fetched, records_inserted, status, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (now_str, str(time_start), str(time_end), fetched, inserted, status, error))
        conn.commit()
        conn.close()

    def _fetch_chunk(self, start_dt, end_dt):
        """调用StreamServer拉取一个时间窗口的所有设备数据"""
        try:
            readings = self.kcgis.get_all_devices_history(start_dt, end_dt)
            return readings or []
        except Exception as e:
            logger.error("[BACKFILL] Fetch chunk error: %s", e)
            return []

    def _insert_batch(self, readings):
        """批量INSERT OR REPLACE到readings表"""
        if not readings:
            return 0

        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            inserted = 0
            now_str = datetime.now(BJ_TZ).isoformat()

            for i in range(0, len(readings), config.BACKFILL_BATCH_SIZE):
                batch = readings[i:i + config.BACKFILL_BATCH_SIZE]
                try:
                    cursor.executemany("""
                        INSERT OR REPLACE INTO readings
                        (device_id, recorded_at, liquid_level, ammonia_n, cod, voltage,
                         isonline, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, [
                        (r.get('device_id'), r.get('recorded_at'),
                         r.get('liquid_level'), r.get('ammonia_n'),
                         r.get('cod'), r.get('voltage'),
                         r.get('isonline', ''), now_str)
                        for r in batch
                    ])
                    inserted += len(batch)
                except Exception as e:
                    logger.error("[BACKFILL] Insert batch error: %s", e)

            conn.commit()
            conn.close()
            return inserted
        except Exception as e:
            logger.warning(f"Direct DB insert failed, trying REST API: {e}")
            return self._insert_batch_rest(readings)

    def _insert_batch_rest(self, readings):
        """Fallback: insert readings via Supabase REST API"""
        from data_processor import _rest_upsert_batch
        now_str = datetime.now(BJ_TZ).isoformat()
        records = []
        for r in readings:
            records.append({
                'device_id': r.get('device_id'),
                'recorded_at': r.get('recorded_at'),
                'liquid_level': r.get('liquid_level'),
                'ammonia_n': r.get('ammonia_n'),
                'cod': r.get('cod'),
                'voltage': r.get('voltage'),
                'isonline': r.get('isonline', ''),
                'created_at': now_str
            })
        if records:
            return _rest_upsert_batch('readings', records)
        return 0

    def backfill_full(self, start_date=None):
        """全量历史回填"""
        start_date = start_date or config.BACKFILL_START_DATE
        try:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d').replace(tzinfo=BJ_TZ)
        except ValueError:
            start_dt = datetime.strptime(start_date, '%Y/%m/%d').replace(tzinfo=BJ_TZ)

        now = datetime.now(BJ_TZ)
        self._log_fetch(start_dt, now, 0, 0, 'running')

        state = {
            'last_synced_time': start_dt.strftime('%Y-%m-%d %H:%M:%S'),
            'status': 'running',
            'total_fetched': 0,
            'total_inserted': 0
        }

        chunk_days = config.BACKFILL_CHUNK_DAYS
        current_start = start_dt
        chunk_count = 0

        while current_start < now:
            current_end = min(current_start + timedelta(days=chunk_days), now)

            logger.info("[BACKFILL] Chunk %d: %s ~ %s",
                       chunk_count + 1,
                       current_start.strftime('%Y-%m-%d'),
                       current_end.strftime('%Y-%m-%d'))

            readings = self._fetch_chunk(current_start, current_end)
            inserted = self._insert_batch(readings) if readings else 0

            state['total_fetched'] += len(readings) if readings else 0
            state['total_inserted'] += inserted
            state['last_synced_time'] = current_end.strftime('%Y-%m-%d %H:%M:%S')

            self._save_state(state)
            self._log_fetch(current_start, current_end,
                          len(readings) if readings else 0, inserted, 'chunk_done')

            chunk_count += 1
            current_start = current_end

            if chunk_count >= config.BACKFILL_MAX_CHUNKS_PER_CYCLE:
                logger.info("[BACKFILL] Reached max chunks per cycle (%d), pausing",
                           config.BACKFILL_MAX_CHUNKS_PER_CYCLE)
                break

            time.sleep(config.BACKFILL_SLEEP_BETWEEN)

        if current_start >= now:
            state['status'] = 'done'
            self._save_state(state)
            self._log_fetch(start_dt, now,
                          state['total_fetched'], state['total_inserted'], 'done',
                          f'chunks={chunk_count}')
            logger.info("[BACKFILL] Full backfill complete: %d fetched, %d inserted, %d chunks",
                       state['total_fetched'], state['total_inserted'], chunk_count)
        else:
            state['status'] = 'paused'
            self._save_state(state)
            logger.info("[BACKFILL] Backfill paused at chunk %d, will continue next cycle", chunk_count)

        return state

    def backfill_incremental(self):
        """增量同步（从上次同步时间到现在）"""
        state = self._get_state()
        if not state or not state.get('last_synced_time'):
            return self.backfill_full()

        try:
            last_time = datetime.strptime(state['last_synced_time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ_TZ)
        except ValueError:
            try:
                last_time = datetime.fromisoformat(state['last_synced_time'][:19]).replace(tzinfo=BJ_TZ)
            except:
                last_time = self._get_latest_reading_time()
                if not last_time:
                    return self.backfill_full()

        now = datetime.now(BJ_TZ)
        if (now - last_time).total_seconds() < 300:
            logger.info("[BACKFILL] Last sync was <5 min ago, skipping")
            return state

        state['status'] = 'running'
        self._save_state(state)
        self._log_fetch(last_time, now, 0, 0, 'incremental_started')

        chunk_days = config.BACKFILL_CHUNK_DAYS
        current_start = last_time
        chunk_count = 0

        while current_start < now:
            current_end = min(current_start + timedelta(days=chunk_days), now)

            readings = self._fetch_chunk(current_start, current_end)
            inserted = self._insert_batch(readings) if readings else 0

            state['total_fetched'] += len(readings) if readings else 0
            state['total_inserted'] += inserted
            state['last_synced_time'] = current_end.strftime('%Y-%m-%d %H:%M:%S')

            self._save_state(state)

            chunk_count += 1
            current_start = current_end

            if chunk_count >= config.BACKFILL_MAX_CHUNKS_PER_CYCLE:
                logger.info("[BACKFILL] Incremental: reached max chunks (%d), pausing",
                           config.BACKFILL_MAX_CHUNKS_PER_CYCLE)
                break

            time.sleep(config.BACKFILL_SLEEP_BETWEEN)

        if current_start >= now:
            state['status'] = 'done'
        else:
            state['status'] = 'paused'

        self._save_state(state)
        self._log_fetch(last_time, now,
                       state['total_fetched'], state['total_inserted'],
                       'incremental_done' if current_start >= now else 'incremental_paused',
                       f'chunks={chunk_count}')

        logger.info("[BACKFILL] Incremental sync: %d fetched, %d inserted, %d chunks",
                   state['total_fetched'], state['total_inserted'], chunk_count)
        return state

    def backfill_incremental_light(self, hours=6):
        """轻量增量同步：只拉最近 N 小时数据，适用于每小时定时任务"""
        now = datetime.now(BJ_TZ)
        default_start = now - timedelta(hours=hours)

        state = self._get_state()
        if state and state.get('last_synced_time'):
            try:
                last_time = datetime.strptime(state['last_synced_time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJ_TZ)
            except ValueError:
                try:
                    last_time = datetime.fromisoformat(state['last_synced_time'][:19]).replace(tzinfo=BJ_TZ)
                except:
                    last_time = default_start
            start = max(default_start, last_time)
        else:
            return self.backfill_full()

        state['status'] = 'running'
        self._save_state(state)

        chunk_days = config.BACKFILL_CHUNK_DAYS
        current_start = start
        chunk_count = 0

        while current_start < now:
            current_end = min(current_start + timedelta(days=chunk_days), now)

            readings = self._fetch_chunk(current_start, current_end)
            inserted = self._insert_batch(readings) if readings else 0

            state['total_fetched'] += len(readings) if readings else 0
            state['total_inserted'] += inserted
            state['last_synced_time'] = current_end.strftime('%Y-%m-%d %H:%M:%S')

            self._save_state(state)

            chunk_count += 1
            current_start = current_end

            if chunk_count >= config.BACKFILL_MAX_CHUNKS_PER_CYCLE:
                break

            time.sleep(config.BACKFILL_SLEEP_BETWEEN)

        state['status'] = 'done' if current_start >= now else 'paused'
        self._save_state(state)

        logger.info("[BACKFILL-LIGHT] %d fetched, %d inserted, %d chunks",
                   state['total_fetched'], state['total_inserted'], chunk_count)
        return state


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    from kcgis_service import KCGIService
    from db import get_conn
    import app as app_module

    logging.basicConfig(level=logging.INFO)

    kcgis = app_module.kcgis_service
    backfill = DataBackfill(kcgis, get_conn=get_conn)

    state = backfill._get_state()
    if state and state.get('last_synced_time'):
        print(f"Found existing state, last synced: {state['last_synced_time']}")
        print("Running incremental backfill...")
        result = backfill.backfill_incremental()
    else:
        print("No existing state, running full backfill...")
        result = backfill.backfill_full()

    print(f"Result: {result}")
