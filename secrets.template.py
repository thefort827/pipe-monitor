"""
敏感配置项模板 - 复制为 config_secrets.py 并填写实际值
"""
import os

# ===== KCGIS API 配置 =====
KCGIS_API_BASE = os.getenv("KCGIS_API_BASE", "")
KCGIS_LOGIN_URL = os.getenv("KCGIS_LOGIN_URL", "")
KCGIS_TENANT = os.getenv("KCGIS_TENANT", "")
KCGIS_ACCOUNT = os.getenv("KCGIS_ACCOUNT", "")
KCGIS_PASSWORD = os.getenv("KCGIS_PASSWORD", "")

# ===== KCGIS 底图瓦片 =====
KCGIS_TILE_URL = os.getenv("KCGIS_TILE_URL", "")
