"""
KCGIS API 服务模块
从 config_secrets.py 读取敏感配置，避免硬编码。
"""
import requests
import sqlite3
import json
import hashlib
from datetime import datetime
import config

try:
    import config_secrets as _secrets
    KCGIS_API_BASE = getattr(_secrets, "KCGIS_API_BASE", "")
    KCGIS_LOGIN_URL = getattr(_secrets, "KCGIS_LOGIN_URL", "")
    KCGIS_TENANT = getattr(_secrets, "KCGIS_TENANT", "")
    KCGIS_ACCOUNT = getattr(_secrets, "KCGIS_ACCOUNT", "")
    KCGIS_PASSWORD = getattr(_secrets, "KCGIS_PASSWORD", "")
except ImportError:
    print("[WARN] config_secrets.py not found, using env vars")
    import os
    KCGIS_API_BASE = os.getenv("KCGIS_API_BASE", "")
    KCGIS_LOGIN_URL = os.getenv("KCGIS_LOGIN_URL", "")
    KCGIS_TENANT = os.getenv("KCGIS_TENANT", "")
    KCGIS_ACCOUNT = os.getenv("KCGIS_ACCOUNT", "")
    KCGIS_PASSWORD = os.getenv("KCGIS_PASSWORD", "")


class KCGIService:
    def __init__(self, token=None):
        self.session = requests.Session()
        self.session.verify = False
        self.token = token  # 可以直接传入现有token

    def get_real_device_id(self, kcgis_device):
        """从KCGIS设备的name字段提取真实device_id"""
        name = kcgis_device.get("name", "")
        # name格式: HSTX_TSPS_TJQ08-W230 或 HSTX_WHKJ_XXX
        # 提取 "-" 前的部分作为device_id
        if "-" in name:
            return name.split("-")[0]
        return name

    def cleanup_shadow_devices(self):
        """清理KCGIS同步产生的影子设备（无读数的HSTX_xxx设备）"""
        try:
            from db import get_conn, fetch_all
            conn = get_conn()
            cursor = conn.cursor()

            # 找出所有HSTX_数字格式的设备（objectId拼接的影子设备）
            if config.DB_TYPE == 'postgresql':
                cursor.execute("""
                    SELECT device_id FROM devices
                    WHERE device_id ~ '^HSTX_[0-9]+$'
                """)
            else:
                cursor.execute("""
                    SELECT device_id FROM devices
                    WHERE device_id LIKE 'HSTX_%'
                    AND REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
                        SUBSTR(device_id, 6), '0', ''), '1', ''), '2', ''), '3', ''), '4', ''),
                        '5', ''), '6', ''), '7', ''), '8', ''), '9', '') = ''
                """)
            shadow_ids = [row[0] for row in cursor.fetchall()]

            if shadow_ids:
                print(f"Found {len(shadow_ids)} shadow devices to cleanup")
                if config.DB_TYPE == 'postgresql':
                    placeholders = ','.join(['%s'] * len(shadow_ids))
                    cursor.execute(f"DELETE FROM devices WHERE device_id IN ({placeholders})", shadow_ids)
                else:
                    placeholders = ','.join(['?'] * len(shadow_ids))
                    cursor.execute(f"DELETE FROM devices WHERE device_id IN ({placeholders})", shadow_ids)
                conn.commit()
                print(f"Deleted {len(shadow_ids)} shadow devices")
            else:
                print("No shadow devices found")

            conn.close()
            return len(shadow_ids)
        except Exception as e:
            print(f"Cleanup skipped (DB unavailable): {e}")
            return 0

    def login(self):
        """通过登录API获取token"""
        try:
            password_md5 = hashlib.md5(KCGIS_PASSWORD.encode()).hexdigest()
            payload = {
                "account": KCGIS_ACCOUNT,
                "password": password_md5
            }
            response = self.session.post(
                KCGIS_LOGIN_URL,
                json=payload,
                timeout=15
            )
            if response.status_code == 200:
                data = response.json()
                token = (data.get("token") or data.get("data", {}).get("token")
                         or data.get("access_token") or data.get("result", {}).get("token"))
                if token:
                    self.token = str(token)
                    return True
                else:
                    print("KCGIS login: no token in response")
                    return False
            else:
                print(f"KCGIS login failed: HTTP {response.status_code}")
                return False
        except Exception as e:
            print(f"KCGIS login unavailable: {e}")
            return False

    def get_devices(self):
        """获取设备列表，失败时自动重新登录重试"""
        if not self.token:
            if not self.login():
                return []

        token = self.token
        for attempt in range(2):
            try:
                params = {
                    "returnGeometry": "true",
                    "where": "1=1",
                    "outSr": "4326",
                    "outFields": "objectId,projid,isonline,type,name,longitude,latitude",
                    "resultOffset": 0,
                    "resultRecordCount": 1000,
                    "token": token,
                    "tenant": KCGIS_TENANT,
                    "f": "json"
                }
                response = self.session.get(KCGIS_API_BASE, params=params, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    ds = data.get("ds", [])
                    if ds:
                        devices = []
                        for row in ds:
                            if len(row) >= 8:
                                name = row[7] if len(row) > 7 else ""
                                real_device_id = name.split("-")[0] if "-" in name else name

                                device = {
                                    "objectId": row[0],
                                    "longitude": row[1],
                                    "latitude": row[2],
                                    "device_id": real_device_id,
                                    "isonline": row[5],
                                    "device_type": row[6],
                                    "name": name
                                }
                                if device["longitude"] and device["latitude"]:
                                    if device["longitude"] > 1 and device["latitude"] > 1:
                                        devices.append(device)
                        print(f"KCGIS: retrieved {len(devices)} devices")
                        return devices
                    else:
                        print("KCGIS: empty response")
                        return []
                elif response.status_code in (401, 403) and attempt == 0:
                    if self.login():
                        token = self.token
                        continue
                else:
                    print(f"KCGIS: API error HTTP {response.status_code}")
                    return []
            except Exception as e:
                print(f"KCGIS: connection error")
                return []
        return []

    def get_device_realtime_data(self, device_id):
        """获取单个设备的实时监测数据（从KCGIS API）"""
        if not self.token:
            if not self.login():
                return None

        token = self.token
        for attempt in range(2):
            try:
                params = {
                    "returnGeometry": "false",
                    "where": f'"deviceId"=\'{device_id}\' or "globalId"=\'{device_id}\'',
                    "outSR": "4326",
                    "outFields": "l,cod,nh,ev,getvaluetime,isonline,name,deviceId",
                    "token": token,
                    "tenant": KCGIS_TENANT,
                    "f": "json"
                }
                api_url = KCGIS_API_BASE.replace('queryex', 'query')
                response = self.session.get(api_url, params=params, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                    features = data.get("features", [])
                    if features:
                        attrs = features[0].get("attributes", {})
                        return {
                            "liquid_level": attrs.get("l"),
                            "cod": attrs.get("cod"),
                            "ammonia_n": attrs.get("nh"),
                            "voltage": attrs.get("ev"),
                            "recorded_at": attrs.get("getvaluetime"),
                            "isonline": attrs.get("isonline"),
                            "name": attrs.get("name")
                        }
                    return None
                elif response.status_code in (401, 403) and attempt == 0:
                    if self.login():
                        token = self.token
                        continue
                return None
            except Exception as e:
                print(f"KCGIS get_device_realtime_data error: {e}")
                return None
        return None

    def get_device_history_data(self, device_id, hours=168):
        """获取设备历史数据（从KCGIS StreamServer API）"""
        if not self.token:
            if not self.login():
                return []

        token = self.token
        for attempt in range(3):
            try:
                from datetime import datetime, timedelta
                import time
                now = datetime.now()
                start_time = now - timedelta(hours=hours)

                start_str = start_time.strftime('%Y/%m/%d %H:%M:%S')
                end_str = now.strftime('%Y/%m/%d %H:%M:%S')

                where_clause = f'"globalId"=\'{device_id}\' and last_edited_date >= timestamp \'{start_str}\' and last_edited_date<timestamp \'{end_str}\''

                params = {
                    "where": where_clause,
                    "outFields": "globalId,name,cod,ev,l,nh,isonline,created_date,last_edited_date",
                    "token": token,
                    "tenant": KCGIS_TENANT,
                    "f": "json"
                }

                api_url = KCGIS_API_BASE.replace('FeatureServer', 'StreamServer').replace('queryex', 'query')
                response = self.session.get(api_url, params=params, timeout=30)

                if response.status_code == 200:
                    data = response.json()
                    features = data.get("features") or []
                    readings = []
                    for feat in features:
                        attrs = feat.get("attributes", {})
                        readings.append({
                            "liquid_level": attrs.get("l"),
                            "cod": attrs.get("cod"),
                            "ammonia_n": attrs.get("nh"),
                            "voltage": attrs.get("ev"),
                            "recorded_at": attrs.get("last_edited_date"),
                            "isonline": attrs.get("isonline")
                        })
                    readings.sort(key=lambda x: x.get("recorded_at") or "", reverse=True)
                    return readings
                elif response.status_code in (401, 403) and attempt == 0:
                    if self.login():
                        token = self.token
                        continue
                return []
            except (ConnectionError, ConnectionResetError, OSError) as e:
                if attempt < 2:
                    wait = 1 + attempt
                    print(f"KCGIS connection error (attempt {attempt+1}/3), retrying in {wait}s: {e}")
                    time.sleep(wait)
                    continue
                print(f"KCGIS get_device_history_data error: {e}")
                return []
            except Exception as e:
                print(f"KCGIS get_device_history_data error: {e}")
                return []
        return []

    def get_all_devices_history(self, start_time, end_time):
        """批量查询所有设备历史数据（不指定globalId，一次查所有设备）"""
        if not self.token:
            if not self.login():
                return []

        token = self.token
        for attempt in range(2):
            try:
                start_str = start_time.strftime('%Y/%m/%d %H:%M:%S')
                end_str = end_time.strftime('%Y/%m/%d %H:%M:%S')

                where_clause = (
                    f"last_edited_date >= timestamp '{start_str}' "
                    f"and last_edited_date < timestamp '{end_str}'"
                )

                params = {
                    "where": where_clause,
                    "outFields": "globalId,name,cod,ev,l,nh,isonline,created_date,last_edited_date",
                    "token": token,
                    "tenant": KCGIS_TENANT,
                    "f": "json"
                }

                api_url = KCGIS_API_BASE.replace('FeatureServer', 'StreamServer').replace('queryex', 'query')
                response = self.session.get(api_url, params=params, timeout=60)

                if response.status_code == 200:
                    data = response.json()
                    features = data.get("features") or []
                    readings = []
                    for feat in features:
                        attrs = feat.get("attributes", {})
                        global_id = attrs.get("globalId", "")
                        device_id = global_id.split("-")[0] if "-" in global_id else global_id
                        readings.append({
                            "device_id": device_id,
                            "global_id": global_id,
                            "liquid_level": attrs.get("l"),
                            "cod": attrs.get("cod"),
                            "ammonia_n": attrs.get("nh"),
                            "voltage": attrs.get("ev"),
                            "recorded_at": attrs.get("last_edited_date"),
                            "isonline": attrs.get("isonline"),
                            "name": attrs.get("name", "")
                        })
                    return readings
                elif response.status_code in (401, 403) and attempt == 0:
                    if self.login():
                        token = self.token
                        continue
                return []
            except Exception as e:
                print(f"KCGIS get_all_devices_history error: {e}")
                return []
        return []

    def sync_to_database(self, kcgis_devices):
        if not kcgis_devices:
            return 0
        try:
            from db import get_conn
            conn = get_conn()
            cursor = conn.cursor()
            synced = 0
            for device in kcgis_devices:
                device_id = device.get("device_id")
                if not device_id:
                    continue
                name = device.get("name", "")
                longitude = device.get("longitude")
                latitude = device.get("latitude")
                device_type = device.get("device_type", "")
                try:
                    if config.DB_TYPE == 'postgresql':
                        cursor.execute("""
                            INSERT INTO devices (device_id, name, area_name, device_type, manufacturers, address, longitude, latitude)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (device_id) DO UPDATE SET name=EXCLUDED.name, longitude=EXCLUDED.longitude, latitude=EXCLUDED.latitude
                        """, (device_id, name, "城西片区", device_type, "沃环科技", "", longitude, latitude))
                    else:
                        cursor.execute("""
                            INSERT OR REPLACE INTO devices
                            (device_id, name, area_name, device_type, manufacturers, address,
                             longitude, latitude, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (device_id, name, "城西片区", device_type, "沃环科技", "", longitude, latitude, datetime.now().isoformat()))
                    synced += 1
                except Exception as e:
                    print(f"Sync device {device_id} error: {e}")
            conn.commit()
            conn.close()
            return synced
        except Exception as e:
            logger.warning(f"Direct DB sync failed, trying REST API: {e}")
            return self._sync_to_rest_api(kcgis_devices)

    def _sync_to_rest_api(self, kcgis_devices):
        """Fallback: sync devices via Supabase REST API"""
        from data_processor import _rest_upsert_batch
        records = []
        for device in kcgis_devices:
            device_id = device.get("device_id")
            if not device_id:
                continue
            records.append({
                'device_id': device_id,
                'name': device.get("name", ""),
                'area_name': '城西片区',
                'device_type': device.get("device_type", ""),
                'manufacturers': '沃环科技',
                'address': '',
                'longitude': device.get("longitude"),
                'latitude': device.get("latitude")
            })
        if records:
            synced = _rest_upsert_batch('devices', records)
            logger.info(f"REST API sync: {synced} devices synced")
            return synced
        return 0


def test_kcgis_connection():
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print("Testing KCGIS connection with login...")
    service = KCGIService()
    devices = service.get_devices()
    if devices:
        print(f"Retrieved {len(devices)} devices")
        print(f"Sample device: {devices[0]}")
        return devices
    else:
        print("No devices retrieved")
        return []


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    devices = test_kcgis_connection()
