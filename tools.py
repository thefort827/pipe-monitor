"""
管网监测 Agent 工具集
基于 smolagents 框架定义的自定义工具
"""
import json
import logging
from smolagents import Tool

logger = logging.getLogger(__name__)


class GetDeviceReadingsTool(Tool):
    name = "get_device_readings"
    description = "获取指定设备的最近液位读数。返回设备ID、时间和液位数据。"
    inputs = {
        "device_id": {"type": "string", "description": "设备ID，如 HSTX_TSPS_TX11"},
        "hours": {"type": "integer", "description": "查询最近几小时，默认24", "nullable": True}
    }
    output_type = "string"

    def forward(self, device_id: str, hours: int = 24) -> str:
        try:
            import data_processor
            readings = data_processor.get_device_readings(device_id, hours=hours)
            if not readings:
                return json.dumps({"error": f"设备 {device_id} 无读数数据"}, ensure_ascii=False)
            result = []
            for r in readings[:20]:
                result.append({
                    "time": str(r.get("recorded_at", ""))[:16],
                    "liquid_level": r.get("liquid_level"),
                    "cod": r.get("cod"),
                    "ammonia_n": r.get("ammonia_n"),
                    "voltage": r.get("voltage"),
                })
            return json.dumps({
                "device_id": device_id,
                "readings": result,
                "total": len(readings),
                "latest_level": result[0]["liquid_level"] if result else None
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class GetDeviceInfoTool(Tool):
    name = "get_device_info"
    description = "获取设备基本信息，包括名称、区域、绑定管点、地面高程等。"
    inputs = {
        "device_id": {"type": "string", "description": "设备ID"}
    }
    output_type = "string"

    def forward(self, device_id: str) -> str:
        try:
            from wechat_bot import _get_cache
            cache = _get_cache()
            bindings = cache.get("bindings", [])
            for b in bindings:
                if b.get("device_id") == device_id:
                    node = b.get("bound_node", {}) or {}
                    return json.dumps({
                        "device_id": device_id,
                        "name": b.get("name", device_id),
                        "area": b.get("area_name", ""),
                        "node_id": node.get("point_id", ""),
                        "ground_elev": node.get("ground_elev"),
                        "well_bottom_elev": node.get("well_bottom_elev"),
                        "bind_distance": b.get("distance"),
                    }, ensure_ascii=False)
            return json.dumps({"error": f"未找到设备 {device_id}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class GetWeatherTool(Tool):
    name = "get_weather"
    description = "获取当前天气和降雨数据，包括近24h降雨量和未来3h预报。"
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        try:
            import weather_service
            from wechat_bot import _get_cache
            cache = _get_cache()
            devices = cache.get("devices", [])
            lat, lon = 29.7, 118.3
            for d in devices:
                if d.get("latitude") and d.get("longitude"):
                    lat, lon = d["latitude"], d["longitude"]
                    break
            summary = weather_service.get_recent_precipitation_summary(lat, lon, hours=24)
            forecast = weather_service.get_forecast_next_hours(lat, lon, hours=3)
            return json.dumps({
                "rainfall_24h_mm": summary.get("total_24h", 0),
                "rainfall_7d_mm": summary.get("total_7d", 0),
                "forecast": [
                    {"time": f.get("time", ""), "precipitation": f.get("precipitation", 0)}
                    for f in (forecast or [])[:4]
                ]
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class GenerateChartTool(Tool):
    name = "generate_chart"
    description = "生成设备液位趋势折线图并保存为图片文件。"
    inputs = {
        "device_id": {"type": "string", "description": "设备ID"},
        "hours": {"type": "integer", "description": "显示最近几小时，默认168（7天）", "nullable": True}
    }
    output_type = "string"

    def forward(self, device_id: str, hours: int = 168) -> str:
        try:
            import data_processor
            from wechat_bot import _generate_device_trend_chart
            readings = data_processor.get_device_readings(device_id, hours=hours)
            if not readings:
                return json.dumps({"error": f"设备 {device_id} 无数据"}, ensure_ascii=False)
            chart_path = _generate_device_trend_chart(device_id, readings)
            if chart_path:
                return json.dumps({
                    "chart_path": chart_path,
                    "message": f"已生成 {device_id} 液位趋势图"
                }, ensure_ascii=False)
            return json.dumps({"error": "图表生成失败"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


class GetDevicesSummaryTool(Tool):
    name = "get_devices_summary"
    description = "获取所有设备的摘要列表，包括设备ID、区域和当前液位。"
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        try:
            import data_processor
            from wechat_bot import _get_cache
            cache = _get_cache()
            bindings = cache.get("bindings", [])
            result = []
            for b in bindings[:30]:
                did = b.get("device_id", "")
                readings = data_processor.get_device_readings(did, hours=6)
                level = readings[0].get("liquid_level") if readings else None
                result.append({
                    "device_id": did,
                    "area": b.get("area_name", ""),
                    "liquid_level": level,
                })
            return json.dumps({
                "devices": result,
                "total": len(bindings)
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


ALL_TOOLS = [
    GetDeviceReadingsTool(),
    GetDeviceInfoTool(),
    GetWeatherTool(),
    GenerateChartTool(),
    GetDevicesSummaryTool(),
]
