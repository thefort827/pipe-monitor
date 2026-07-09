"""
敏感配置项 - 从环境变量读取，不上传 GitHub。
"""
import os

# ===== KCGIS API 配置 =====
KCGIS_API_BASE = os.getenv("KCGIS_API_BASE", "")
KCGIS_LOGIN_URL = os.getenv("KCGIS_LOGIN_URL", "")
KCGIS_TENANT = os.getenv("KCGIS_TENANT", "")
KCGIS_ACCOUNT = os.getenv("KCGIS_ACCOUNT", "")
KCGIS_PASSWORD = os.getenv("KCGIS_PASSWORD", "")
KCGIS_TOKEN = os.getenv("KCGIS_TOKEN", "")

# ===== KCGIS 底图瓦片 =====
KCGIS_TILE_URL = os.getenv("KCGIS_TILE_URL", "")

# ===== KCGIS 矢量瓦片服务 =====
KCGIS_VECTOR_TILE_URL = os.getenv("KCGIS_VECTOR_TILE_URL", "")

# ===== KCGIS 图片瓦片服务 =====
KCGIS_IMG_TILE_URL = os.getenv("KCGIS_IMG_TILE_URL", "")

# ===== MiMo-V2.5 AI 模型配置 =====
MIMO_API_BASE = os.getenv("MIMO_API_BASE", "")
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_MODEL = os.getenv("MIMO_MODEL", "mimo-v2.5")

# ===== iLink Bot API 配置 =====
ILINK_API_BASE = os.getenv("ILINK_API_BASE", "")
ILINK_TOKEN = os.getenv("ILINK_TOKEN", "")

# ===== 应用自调用地址 =====
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
