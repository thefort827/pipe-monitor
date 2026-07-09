"""
Database adapter - handles SQLite and PostgreSQL compatibility
"""
import os
from datetime import timedelta
import config


def get_conn():
    """Get database connection with fallback"""
    if config.DB_TYPE == 'postgresql':
        try:
            import psycopg2
            conn = psycopg2.connect(config.DATABASE_URL)
            conn.autocommit = False
            return conn
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "PostgreSQL connection failed (%s), falling back to SQLite", e)
            # 降级时更新全局数据库类型，确保后续 SQL 查询使用正确的语法
            config.DB_TYPE = 'sqlite'
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def fetch_all(cursor, conn):
    """Fetch all results as list of dicts"""
    if config.DB_TYPE == 'postgresql':
        cols = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    else:
        return [dict(row) for row in cursor.fetchall()]


def fetch_one(cursor):
    """Fetch one result as dict"""
    if config.DB_TYPE == 'postgresql':
        cols = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else None
    else:
        row = cursor.fetchone()
        return dict(row) if row else None


def hours_ago(hours):
    """Return SQL expression for N hours ago (Asia/Shanghai)"""
    if config.DB_TYPE == 'postgresql':
        return f"timezone('Asia/Shanghai', NOW()) - INTERVAL '{hours} hours'"
    else:
        cutoff = config.now_sh() - timedelta(hours=hours)
        return f"'{cutoff.strftime('%Y-%m-%d %H:%M:%S')}'"


def param_placeholder(idx=1):
    """Return parameter placeholder"""
    if config.DB_TYPE == 'postgresql':
        return f'%{idx}'
    else:
        return '?'


def now():
    """Return current timestamp SQL (Asia/Shanghai)"""
    if config.DB_TYPE == 'postgresql':
        return "timezone('Asia/Shanghai', NOW())"
    else:
        return f"'{config.now_sh().strftime('%Y-%m-%d %H:%M:%S')}'"
