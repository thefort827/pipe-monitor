"""
管网监测自动响应服务
- 循环自动同步 KCGIS 设备数据
- 监测降水 + 设备液位
- 超阈值自动推送微信告警
- 全链路 Trace / Log / Metrics
"""
import logging
import time
import json
import threading
import traceback
from datetime import datetime, timedelta
from collections import defaultdict
from enum import Enum
from zoneinfo import ZoneInfo

import config
import data_processor
import liquid_analysis
import weather_service

logger = logging.getLogger(__name__)

BJ_TZ = ZoneInfo('Asia/Shanghai')


def now_bj():
    return datetime.now(BJ_TZ)

# ===== 告警级别定义 =====

class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(Enum):
    RAINFALL_HEAVY = "rainfall_heavy"
    RAINFALL_STORM = "rainfall_storm"
    LIQUID_LEVEL_HIGH = "liquid_level_high"
    LIQUID_LEVEL_CRITICAL = "liquid_level_critical"
    OVERFLOW_RISK = "overflow_risk"
    WATER_REVERSAL = "water_reversal"
    DEVICE_OFFLINE = "device_offline"
    DATA_FROZEN = "data_frozen"
    LEVEL_SUDDEN_CHANGE = "level_sudden_change"
    INCONSISTENCY = "water_level_inconsistency"
    COD_HIGH = "cod_high"
    NH3N_HIGH = "nh3n_high"


# ===== 监测阈值配置 =====

MONITOR_THRESHOLDS = {
    # 降雨阈值 (mm/h)
    'rainfall_warning': 5.0,
    'rainfall_critical': 20.0,
    'rainfall_storm': 50.0,
    # 液位阈值 (m) - 距离地面高程
    'overflow_critical': 0.0,
    'overflow_high': 0.3,
    'overflow_medium': 0.5,
    # 液位突变 (m/h)
    'level_change_rate_warning': 0.3,
    'level_change_rate_critical': 0.8,
    # 连通器偏差 (m)
    'inconsistency_warning': 0.5,
    'inconsistency_critical': 1.0,
    # 水质阈值（COD数据源可信度低，已禁用）
    # 'cod_warning': 400.0,
    # 'cod_critical': 500.0,
    'nh3n_warning': 50.0,
    'nh3n_critical': 100.0,
    # 设备离线
    'offline_hours': 24,
}

# ===== 告警去重/冷却 =====

ALERT_COOLDOWN = {
    AlertType.RAINFALL_HEAVY: 3600,
    AlertType.RAINFALL_STORM: 1800,
    AlertType.LIQUID_LEVEL_HIGH: 7200,
    AlertType.LIQUID_LEVEL_CRITICAL: 1800,
    AlertType.OVERFLOW_RISK: 1800,
    AlertType.WATER_REVERSAL: 7200,
    AlertType.DEVICE_OFFLINE: 86400,
    AlertType.DATA_FROZEN: 86400,
    AlertType.LEVEL_SUDDEN_CHANGE: 3600,
    AlertType.INCONSISTENCY: 7200,
    AlertType.COD_HIGH: 7200,
    AlertType.NH3N_HIGH: 7200,
}

# ===== Metrics 存储 =====

class MetricsCollector:
    """简单的 Metrics 收集器，支持计数器、直方图、gauge"""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._gauges = {}
        self._histograms = defaultdict(list)
        self._last_reset = now_bj()

    def inc(self, name, value=1):
        with self._lock:
            self._counters[name] += value

    def set(self, name, value):
        with self._lock:
            self._gauges[name] = value

    def observe(self, name, value):
        with self._lock:
            self._histograms[name].append(value)
            if len(self._histograms[name]) > 1000:
                self._histograms[name] = self._histograms[name][-500:]

    def get_snapshot(self):
        with self._lock:
            hist_summary = {}
            for name, vals in self._histograms.items():
                if vals:
                    hist_summary[name] = {
                        'count': len(vals),
                        'min': round(min(vals), 4),
                        'max': round(max(vals), 4),
                        'avg': round(sum(vals) / len(vals), 4),
                        'p95': round(sorted(vals)[int(len(vals) * 0.95)] if len(vals) > 1 else vals[0], 4),
                    }
            return {
                'counters': dict(self._counters),
                'gauges': dict(self._gauges),
                'histograms': hist_summary,
                'snapshot_time': now_bj().isoformat(),
            }

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._last_reset = now_bj()


metrics = MetricsCollector()


# ===== Trace 追踪 =====

class TraceSpan:
    """操作追踪 Span"""
    def __init__(self, operation, parent=None):
        self.operation = operation
        self.parent = parent
        self.start_time = time.time()
        self.end_time = None
        self.status = "ok"
        self.attributes = {}
        self.events = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, data=None):
        self.events.append({
            'name': name,
            'time': datetime.now().isoformat(),
            'data': data,
        })

    def finish(self, status="ok"):
        self.end_time = time.time()
        self.status = status

    @property
    def duration_ms(self):
        if self.end_time:
            return round((self.end_time - self.start_time) * 1000, 1)
        return round((time.time() - self.start_time) * 1000, 1)

    def to_dict(self):
        return {
            'operation': self.operation,
            'duration_ms': self.duration_ms,
            'status': self.status,
            'attributes': self.attributes,
            'events': self.events,
            'start_time': datetime.fromtimestamp(self.start_time, tz=BJ_TZ).isoformat(),
        }


_trace_store = []
_trace_lock = threading.Lock()
MAX_TRACES = 200


def record_trace(span):
    with _trace_lock:
        _trace_store.append(span.to_dict())
        if len(_trace_store) > MAX_TRACES:
            _trace_store.pop(0)


def get_traces():
    with _trace_lock:
        return list(_trace_store)


# ===== 告警状态管理 =====

class AlertState:
    """告警状态管理 - 去重、冷却、历史"""
    def __init__(self):
        self._lock = threading.Lock()
        self._last_alerts = {}
        self._alert_history = []

    def should_alert(self, alert_type, key):
        with self._lock:
            now = time.time()
            last_key = f"{alert_type.value}:{key}"
            last_time = self._last_alerts.get(last_key, 0)
            cooldown = ALERT_COOLDOWN.get(alert_type, 3600)
            if now - last_time >= cooldown:
                return True
            return False

    def record_alert(self, alert_type, key, alert_data):
        with self._lock:
            now = time.time()
            last_key = f"{alert_type.value}:{key}"
            self._last_alerts[last_key] = now
            self._alert_history.append({
            'time': now_bj().isoformat(),
                'type': alert_type.value,
                'key': key,
                'data': alert_data,
            })
            if len(self._alert_history) > 500:
                self._alert_history = self._alert_history[-250:]

    def get_history(self, limit=50):
        with self._lock:
            return list(self._alert_history[-limit:])


alert_state = AlertState()


# ===== 核心监测逻辑 =====

def _fetch_and_sync_data(trace):
    """从 KCGIS 同步设备数据"""
    try:
        from kcgis_service import KCGIService
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        import app as app_module
        kcgis = app_module.kcgis_service

        trace.add_event("kcgis_sync_start")
        devices = kcgis.get_devices()
        trace.set_attribute("kcgis_devices_fetched", len(devices) if devices else 0)
        metrics.inc("kcgis_fetch_total")
        metrics.observe("kcgis_fetch_count", len(devices) if devices else 0)

        if devices:
            synced = kcgis.sync_to_database(devices)
            trace.set_attribute("devices_synced", synced)
            metrics.set("devices_total", synced)
            logger.info("[SYNC] KCGIS: %d devices fetched, %d synced", len(devices), synced)
        else:
            trace.add_event("kcgis_fetch_empty")
            metrics.inc("kcgis_fetch_empty")
            logger.warning("[SYNC] KCGIS returned 0 devices")

        trace.add_event("kcgis_sync_done")
    except Exception as e:
        trace.finish("error")
        trace.add_event("kcgis_sync_error", {'error': str(e)})
        metrics.inc("kcgis_fetch_errors")
        logger.error("[SYNC] KCGIS sync failed: %s", e)


def _check_rainfall(trace, cache):
    """监测降雨情况"""
    alerts = []
    try:
        trace.add_event("rainfall_check_start")

        rainfall_hours, heavy_rain_hours, rainfall_periods = liquid_analysis.get_rainfall_periods(hours_back=48)
        trace.set_attribute("rainfall_hours_48h", len(rainfall_hours))
        trace.set_attribute("heavy_rain_hours_48h", len(heavy_rain_hours))
        metrics.set("rainfall_hours_48h", len(rainfall_hours))

        if not rainfall_periods:
            trace.add_event("no_rainfall")
            return alerts

        latest_period = rainfall_periods[-1] if rainfall_periods else None
        if latest_period:
            start, end, total = latest_period
            trace.set_attribute("latest_rainfall_total_mm", total)

            try:
                end_dt = datetime.strptime(end, '%Y-%m-%d %H')
                hours_ago = (datetime.now() - end_dt).total_seconds() / 3600
            except:
                hours_ago = 999

            key = f"rain_{start}_{end}"

            if total >= MONITOR_THRESHOLDS['rainfall_storm'] or (total >= MONITOR_THRESHOLDS['rainfall_critical'] and hours_ago < 6):
                if alert_state.should_alert(AlertType.RAINFALL_STORM, key):
                    alerts.append({
                        'type': AlertType.RAINFALL_STORM,
                        'level': AlertLevel.CRITICAL,
                        'title': '暴雨预警',
                        'message': f"检测到暴雨天气：{start} ~ {end}，累计降雨量 {total:.1f}mm\n建议立即启动应急响应，加密巡查频率",
                        'data': {'period': (start, end, total), 'hours_ago': hours_ago},
                    })
            elif total >= MONITOR_THRESHOLDS['rainfall_critical']:
                if alert_state.should_alert(AlertType.RAINFALL_HEAVY, key):
                    alerts.append({
                        'type': AlertType.RAINFALL_HEAVY,
                        'level': AlertLevel.WARNING,
                        'title': '大雨预警',
                        'message': f"检测到强降雨：{start} ~ {end}，累计降雨量 {total:.1f}mm\n建议关注管网液位变化",
                        'data': {'period': (start, end, total), 'hours_ago': hours_ago},
                    })
            elif total >= MONITOR_THRESHOLDS['rainfall_warning']:
                if alert_state.should_alert(AlertType.RAINFALL_HEAVY, key):
                    alerts.append({
                        'type': AlertType.RAINFALL_HEAVY,
                        'level': AlertLevel.INFO,
                        'title': '降雨提醒',
                        'message': f"检测到降雨：{start} ~ {end}，累计 {total:.1f}mm",
                        'data': {'period': (start, end, total), 'hours_ago': hours_ago},
                    })

        metrics.set("rainfall_alerts_generated", len(alerts))
        trace.add_event("rainfall_check_done", {'alerts': len(alerts)})
    except Exception as e:
        trace.add_event("rainfall_check_error", {'error': str(e)})
        logger.error("[MONITOR] Rainfall check error: %s", e)

    return alerts


def _check_liquid_levels(trace, cache, bindings, pipes, node_map):
    """监测液位异常"""
    alerts = []
    try:
        trace.add_event("liquid_level_check_start")

        active_bindings, inactive_bindings = liquid_analysis.get_active_devices(bindings, hours=48)
        trace.set_attribute("active_devices", len(active_bindings))
        metrics.set("active_devices", len(active_bindings))
        metrics.set("inactive_devices", len(inactive_bindings))

        device_readings = {}
        for b in active_bindings:
            did = b['device_id']
            readings = data_processor.get_device_readings(did, hours=24)
            if readings:
                device_readings[did] = readings

        trace.set_attribute("devices_with_readings", len(device_readings))

        for did, readings in device_readings.items():
            binding = next((b for b in active_bindings if b['device_id'] == did), None)
            if not binding or not binding.get('bound_node'):
                continue

            node = binding['bound_node']
            ground_elev = node.get('ground_elev')
            well_bottom = node.get('well_bottom_elev')
            if ground_elev is None or well_bottom is None:
                continue

            latest = readings[0]
            level = latest.get('liquid_level')
            if level is None:
                continue

            water_level = well_bottom + level
            distance_to_ground = ground_elev - water_level

            key = f"{did}"
            node_id = node.get('point_id', '')

            if distance_to_ground < MONITOR_THRESHOLDS['overflow_critical']:
                if alert_state.should_alert(AlertType.OVERFLOW_RISK, key):
                    alerts.append({
                        'type': AlertType.OVERFLOW_RISK,
                        'level': AlertLevel.CRITICAL,
                        'title': '溢出风险预警',
                        'message': f"设备 {did}（管点 {node_id}）水位 {water_level:.2f}m 已超过地面高程 {ground_elev:.2f}m，存在溢出风险！\n液位: {level:.3f}m, 井底高程: {well_bottom:.2f}m",
                        'data': {'device_id': did, 'water_level': water_level, 'ground_elev': ground_elev},
                    })
            elif distance_to_ground < MONITOR_THRESHOLDS['overflow_high']:
                if alert_state.should_alert(AlertType.LIQUID_LEVEL_CRITICAL, key):
                    alerts.append({
                        'type': AlertType.LIQUID_LEVEL_CRITICAL,
                        'level': AlertLevel.CRITICAL,
                        'title': '高水位预警',
                        'message': f"设备 {did}（管点 {node_id}）水位 {water_level:.2f}m 距地面仅 {distance_to_ground:.2f}m\n液位: {level:.3f}m",
                        'data': {'device_id': did, 'distance_to_ground': distance_to_ground},
                    })
            elif distance_to_ground < MONITOR_THRESHOLDS['overflow_medium']:
                if alert_state.should_alert(AlertType.LIQUID_LEVEL_HIGH, key):
                    alerts.append({
                        'type': AlertType.LIQUID_LEVEL_HIGH,
                        'level': AlertLevel.WARNING,
                        'title': '液位偏高',
                        'message': f"设备 {did}（管点 {node_id}）水位 {water_level:.2f}m 距地面 {distance_to_ground:.2f}m\n液位: {level:.3f}m",
                        'data': {'device_id': did, 'distance_to_ground': distance_to_ground},
                    })

            nh3n = latest.get('ammonia_n')
            if nh3n is not None and nh3n > MONITOR_THRESHOLDS['nh3n_critical']:
                if alert_state.should_alert(AlertType.NH3N_HIGH, key):
                    alerts.append({
                        'type': AlertType.NH3N_HIGH,
                        'level': AlertLevel.WARNING,
                        'title': '氨氮超标',
                        'message': f"设备 {did} 氨氮={nh3n:.1f}mg/L 超过阈值 {MONITOR_THRESHOLDS['nh3n_critical']}mg/L",
                        'data': {'device_id': did, 'nh3n': nh3n},
                    })

            if len(readings) >= 3:
                recent_levels = [float(r['liquid_level']) for r in readings[:6] if r.get('liquid_level') is not None]
                if len(recent_levels) >= 3:
                    max_change = max(abs(recent_levels[i] - recent_levels[i+1]) for i in range(len(recent_levels)-1))
                    time_span = len(recent_levels)
                    if max_change > MONITOR_THRESHOLDS['level_change_rate_critical'] and time_span <= 6:
                        if alert_state.should_alert(AlertType.LEVEL_SUDDEN_CHANGE, key):
                            alerts.append({
                                'type': AlertType.LEVEL_SUDDEN_CHANGE,
                                'level': AlertLevel.WARNING,
                                'title': '液位突变',
                                'message': f"设备 {did} 液位突变 {max_change:.3f}m（近{time_span}个读数周期）\n当前液位: {level:.3f}m",
                                'data': {'device_id': did, 'change': max_change},
                            })

        trace.add_event("liquid_level_check_done", {'alerts': len(alerts)})
    except Exception as e:
        trace.add_event("liquid_level_check_error", {'error': str(e)})
        logger.error("[MONITOR] Liquid level check error: %s", e)

    return alerts


def _check_anomalies(trace, cache, bindings, pipes, node_map):
    """运行连通器分析，检测新增异常"""
    alerts = []
    try:
        trace.add_event("anomaly_check_start")

        report = cache.get('liquid_report', {})
        old_total = report.get('total', 0)

        new_report = liquid_analysis.generate_liquid_analysis_report(bindings, pipes, node_map)
        new_total = new_report.get('total', 0)
        trace.set_attribute("old_anomaly_count", old_total)
        trace.set_attribute("new_anomaly_count", new_total)
        metrics.set("anomaly_total", new_total)

        if new_total > old_total:
            new_anomalies = new_report.get('anomalies', [])[:new_total - old_total]
            critical_count = sum(1 for a in new_anomalies if a.get('severity') == 'critical')
            high_count = sum(1 for a in new_anomalies if a.get('severity') == 'high')

            key = f"anomaly_{datetime.now().strftime('%Y%m%d_%H')}"
            if alert_state.should_alert(AlertType.WATER_REVERSAL, key):
                msg_lines = [f"检测到 {len(new_anomalies)} 个新增异常（严重{critical_count}/高{high_count}）"]
                for a in new_anomalies[:5]:
                    sev = a.get('severity', '')
                    did = a.get('device_id', '')
                    reason = a.get('reason', '')[:80]
                    msg_lines.append(f"[{sev}] {did}: {reason}")

                alerts.append({
                    'type': AlertType.WATER_REVERSAL,
                    'level': AlertLevel.CRITICAL if critical_count > 0 else AlertLevel.WARNING,
                    'title': f'新增异常 {len(new_anomalies)} 条',
                    'message': '\n'.join(msg_lines),
                    'data': {'new_count': len(new_anomalies), 'critical': critical_count, 'high': high_count},
                })

        metrics.set("anomaly_total", new_total)
        trace.add_event("anomaly_check_done", {'new_total': new_total})
    except Exception as e:
        trace.add_event("anomaly_check_error", {'error': str(e)})
        logger.error("[MONITOR] Anomaly check error: %s", e)

    return alerts


def _backfill_readings(trace):
    """自动回填readings数据"""
    import app as app_module
    if not app_module._backfill_lock.acquire(blocking=False):
        trace.add_event("backfill_skipped", {'reason': 'lock_held'})
        return
    try:
        from data_backfill import DataBackfill
        from db import get_conn

        kcgis = app_module.kcgis_service
        backfill = DataBackfill(kcgis, get_conn=get_conn)

        state = backfill._get_state()
        trace.add_event("backfill_check", {'state': state.get('status') if state else 'new'})

        if state and state.get('status') == 'running':
            result = backfill.backfill_incremental()
        elif not state or not state.get('last_synced_time'):
            result = backfill.backfill_full()
        else:
            result = backfill.backfill_incremental()

        trace.set_attribute("backfill_fetched", result.get('total_fetched', 0))
        trace.set_attribute("backfill_inserted", result.get('total_inserted', 0))
        trace.add_event("backfill_done", {'status': result.get('status', 'unknown')})
        metrics.inc("backfill_cycles")
        metrics.set("backfill_total_inserted", result.get('total_inserted', 0))

    except Exception as e:
        trace.add_event("backfill_error", {'error': str(e)})
        metrics.inc("backfill_errors")
        logger.error("[BACKFILL] Error: %s", e)
    finally:
        app_module._backfill_lock.release()


def _detect_push_alerts(trace, cache):
    """检测是否需要推送告警（3个触发条件：液位突增、液位突降、暴雨预报）"""
    alerts = []

    try:
        trace.add_event("push_trigger_check_start")

        bindings = cache.get('bindings', [])
        active_bindings = [b for b in bindings if b.get('device_id')]

        trigger_devices = []

        for b in active_bindings:
            did = b['device_id']
            readings = data_processor.get_device_readings(did, hours=6)
            if not readings or len(readings) < 2:
                continue

            latest = readings[0]
            prev = readings[1]
            level_now = latest.get('liquid_level')
            level_prev = prev.get('liquid_level')

            if level_now is None or level_prev is None:
                continue

            try:
                change = float(level_now) - float(level_prev)
            except (ValueError, TypeError):
                continue

            if abs(change) >= config.PUSH_LEVEL_CHANGE_THRESHOLD:
                direction = "突增" if change > 0 else "突降"
                trigger_devices.append({
                    'device_id': did,
                    'type': f'液位{direction}',
                    'change': change,
                    'level_now': float(level_now),
                    'level_prev': float(level_prev),
                })

        if trigger_devices:
            now_str = now_bj().strftime('%Y-%m-%d %H:%M')
            msg_lines = [f"【液位突变告警】{now_str}"]
            for td in trigger_devices[:10]:
                direction = "↑" if td['change'] > 0 else "↓"
                msg_lines.append(
                    f"  {td['device_id']}: {td['level_prev']:.3f}m {direction} {td['level_now']:.3f}m "
                    f"(变化 {td['change']:+.3f}m)"
                )
            alerts.append({
                'type': 'level_sudden_change_push',
                'level': 'critical' if any(abs(td['change']) >= 0.8 for td in trigger_devices) else 'warning',
                'title': f'液位突变 ({len(trigger_devices)}台设备)',
                'message': '\n'.join(msg_lines),
                'data': trigger_devices,
            })

        devices_cache = cache.get('devices', [])
        sample_lat, sample_lon = 30.0, 118.0
        for d in devices_cache:
            if d.get('latitude') and d.get('longitude'):
                sample_lat = d['latitude']
                sample_lon = d['longitude']
                break

        heavy_rain, max_precip, forecast = weather_service.check_heavy_rain_forecast(
            sample_lat, sample_lon, threshold=config.PUSH_RAINFALL_FORECAST_THRESHOLD
        )

        if heavy_rain:
            forecast_lines = []
            for f in forecast[:4]:
                try:
                    t_str = f['time'].replace('T', ' ')[:16]
                    forecast_lines.append(f"  {t_str}: {f['precipitation']:.1f}mm/h")
                except:
                    pass

            now_str = now_bj().strftime('%Y-%m-%d %H:%M')
            alerts.append({
                'type': 'rainfall_forecast_push',
                'level': 'critical',
                'title': '暴雨预报预警',
                'message': (
                    f"【天气预报预警】{now_str}\n"
                    f"未来1小时预计有强降雨（最大 {max_precip:.1f}mm/h）\n"
                    f"预报详情:\n" + '\n'.join(forecast_lines) +
                    f"\n建议提前做好防汛准备"
                ),
                'data': {'max_precip': max_precip, 'forecast': forecast[:4]},
            })

        trace.add_event("push_trigger_check_done", {'alerts': len(alerts)})
        metrics.set("push_trigger_alerts", len(alerts))

    except Exception as e:
        trace.add_event("push_trigger_error", {'error': str(e)})
        logger.error("[MONITOR] Push trigger detection error: %s", e)

    return alerts


def _push_alerts(alerts, trace):
    """推送告警到微信（北京时间，触发类告警使用MiMo RAG分析）"""
    if not alerts:
        return

    push_trigger_types = {'level_sudden_change_push', 'rainfall_forecast_push'}
    push_trigger_alerts = [a for a in alerts if a.get('type') in push_trigger_types]
    regular_alerts = [a for a in alerts if a.get('type') not in push_trigger_types]

    try:
        if push_trigger_alerts:
            try:
                from wechat_bot import push_analysis_report
                import app as app_module
                cache = getattr(app_module, 'data_cache', {})
                push_analysis_report(push_trigger_alerts, cache=cache)
                metrics.inc("alerts_pushed")
                trace.add_event("rag_analysis_pushed", {'count': len(push_trigger_alerts)})
                logger.info("[ALERT] RAG analysis pushed for %d trigger alerts", len(push_trigger_alerts))
            except Exception as e:
                metrics.inc("alerts_push_errors")
                trace.add_event("rag_analysis_push_error", {'error': str(e)})
                logger.error("[ALERT] RAG analysis push failed: %s", e)

        from wechat_bot import push_alert_to_contacts

        for alert in regular_alerts:
            alert_type = alert['type']
            level = alert['level']
            title = alert['title']
            message = alert['message']

            alert_state.record_alert(alert_type, f"{title}_{now_bj().strftime('%H%M')}", alert)

            now_str = now_bj().strftime('%Y-%m-%d %H:%M')
            if isinstance(level, AlertLevel):
                prefix = "🔴" if level == AlertLevel.CRITICAL else "🟡" if level == AlertLevel.WARNING else "ℹ️"
            else:
                prefix = "🔴" if level == 'critical' else "🟡" if level == 'warning' else "ℹ️"
            text = f"{prefix} 【{title}】\n{message}\n\n时间: {now_str}"

            try:
                push_alert_to_contacts(text)
                metrics.inc("alerts_pushed")
                trace.add_event("alert_pushed", {'type': alert_type, 'title': title})
                logger.info("[ALERT] Pushed: %s - %s", level, title)
            except Exception as e:
                metrics.inc("alerts_push_errors")
                trace.add_event("alert_push_error", {'type': alert_type, 'error': str(e)})
                logger.error("[ALERT] Push failed: %s - %s", title, e)

    except Exception as e:
        logger.error("[ALERT] Push system error: %s", e)


# ===== 主监控循环 =====

def run_monitor_cycle(cache, kcgis_service=None):
    """执行一次完整监测周期"""
    trace = TraceSpan("monitor_cycle")
    cycle_start = time.time()

    try:
        trace.add_event("cycle_start")
        metrics.inc("monitor_cycles_total")

        bindings = cache.get('bindings', [])
        pipes = cache.get('pipes', [])
        node_map = cache.get('node_map', {})

        trace.set_attribute("bindings_count", len(bindings))

        _fetch_and_sync_data(trace)
        _backfill_readings(trace)

        try:
            data_processor_result = data_processor.load_devices()
            if data_processor_result:
                cache['devices'] = data_processor_result
                cache['bindings'] = data_processor.bind_devices_to_nodes(
                    data_processor_result, cache.get('nodes', [])
                )
                bindings = cache['bindings']
                trace.set_attribute("devices_reloaded", len(data_processor_result))
        except Exception as e:
            trace.add_event("reload_skip", {'reason': str(e)})
            logger.warning("[MONITOR] Skip reload (db locked): %s", e)

        cache['last_update'] = now_bj().isoformat()

        all_alerts = []

        rain_alerts = _check_rainfall(trace, cache)
        all_alerts.extend(rain_alerts)

        liquid_alerts = _check_liquid_levels(trace, cache, bindings, pipes, node_map)
        all_alerts.extend(liquid_alerts)

        anomaly_alerts = _check_anomalies(trace, cache, bindings, pipes, node_map)
        all_alerts.extend(anomaly_alerts)

        push_alerts = _detect_push_alerts(trace, cache)
        all_alerts.extend(push_alerts)

        trace.set_attribute("total_alerts", len(all_alerts))
        metrics.set("alerts_pending", len(all_alerts))

        _push_alerts(all_alerts, trace)

        trace.finish("ok")
        trace.set_attribute("cycle_duration_ms", trace.duration_ms)
        record_trace(trace)

        metrics.observe("cycle_duration_ms", trace.duration_ms)
        logger.info("[MONITOR] Cycle complete: %d alerts in %.1fs", len(all_alerts), trace.duration_ms / 1000)

        return {
            'status': 'ok',
            'alerts': len(all_alerts),
            'duration_ms': trace.duration_ms,
            'timestamp': now_bj().isoformat(),
        }

    except Exception as e:
        trace.finish("error")
        trace.add_event("cycle_error", {'error': str(e), 'traceback': traceback.format_exc()})
        record_trace(trace)
        metrics.inc("monitor_cycle_errors")
        logger.error("[MONITOR] Cycle error: %s\n%s", e, traceback.format_exc())
        return {
            'status': 'error',
            'error': str(e),
            'duration_ms': trace.duration_ms,
            'timestamp': now_bj().isoformat(),
        }


# ===== API 接口 =====

def get_monitor_status():
    """获取监控系统状态"""
    return {
        'metrics': metrics.get_snapshot(),
        'recent_traces': get_traces()[-10:],
        'alert_history': alert_state.get_history(limit=20),
        'thresholds': MONITOR_THRESHOLDS,
        'cooldowns': {k.value: v for k, v in ALERT_COOLDOWN.items()},
        'timestamp': now_bj().isoformat(),
    }


def get_monitor_metrics():
    """获取 Prometheus 风格的 metrics"""
    snap = metrics.get_snapshot()
    lines = []
    lines.append("# HELP monitor_cycles_total Total monitor cycles executed")
    lines.append("# TYPE monitor_cycles_total counter")
    lines.append(f"monitor_cycles_total {snap['counters'].get('monitor_cycles_total', 0)}")
    lines.append("")
    lines.append("# HELP alerts_pushed Total alerts pushed to WeChat")
    lines.append("# TYPE alerts_pushed counter")
    lines.append(f"alerts_pushed {snap['counters'].get('alerts_pushed', 0)}")
    lines.append("")
    lines.append("# HELP alerts_pending Current pending alerts")
    lines.append("# TYPE alerts_pending gauge")
    lines.append(f"alerts_pending {snap['gauges'].get('alerts_pending', 0)}")
    lines.append("")
    lines.append("# HELP active_devices Currently active devices")
    lines.append("# TYPE active_devices gauge")
    lines.append(f"active_devices {snap['gauges'].get('active_devices', 0)}")
    lines.append("")
    lines.append("# HELP anomaly_total Total anomalies detected")
    lines.append("# TYPE anomaly_total gauge")
    lines.append(f"anomaly_total {snap['gauges'].get('anomaly_total', 0)}")
    lines.append("")
    lines.append("# HELP cycle_duration_ms Monitor cycle duration")
    lines.append("# TYPE cycle_duration_ms histogram")
    if 'cycle_duration_ms' in snap.get('histograms', {}):
        h = snap['histograms']['cycle_duration_ms']
        lines.append(f"cycle_duration_ms_count {h['count']}")
        lines.append(f"cycle_duration_ms_sum {h['avg'] * h['count']:.1f}")
        lines.append(f"cycle_duration_ms_avg {h['avg']}")
    lines.append("")
    lines.append("# HELP rainfall_hours_48h Rainfall hours in last 48h")
    lines.append("# TYPE rainfall_hours_48h gauge")
    lines.append(f"rainfall_hours_48h {snap['gauges'].get('rainfall_hours_48h', 0)}")
    lines.append("")

    return '\n'.join(lines)


# ===== 启动入口 =====

_monitor_thread = None
_monitor_running = False


def start_monitor(cache_ref, interval_seconds=300):
    """启动后台监控线程"""
    global _monitor_thread, _monitor_running

    _monitor_running = True

    def _loop():
        logger.info("[MONITOR] Background monitor started (interval=%ds)", interval_seconds)
        while _monitor_running:
            try:
                run_monitor_cycle(cache_ref)
            except Exception as e:
                logger.error("[MONITOR] Loop error: %s", e)
            time.sleep(interval_seconds)

    _monitor_thread = threading.Thread(target=_loop, daemon=True, name="monitor-loop")
    _monitor_thread.start()
    logger.info("[MONITOR] Monitor thread started")
    return _monitor_thread


def stop_monitor():
    global _monitor_running
    _monitor_running = False
    logger.info("[MONITOR] Monitor stopped")
