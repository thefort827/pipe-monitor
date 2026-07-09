"""
微信图表生成模块 - 用 matplotlib 生成管网数据可视化图表
"""
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patheffects as path_effects
import numpy as np

logger = logging.getLogger(__name__)

# 中文字体配置
plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'WenQuanYi Zen Hei', 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 图表保存目录
CHART_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'charts')
os.makedirs(CHART_DIR, exist_ok=True)


def _save_chart(fig, name='chart'):
    path = os.path.join(CHART_DIR, '%s_%s.png' % (name, datetime.now().strftime('%H%M%S')))
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white', edgecolor='none')
    plt.close(fig)
    return path


# 配色方案
COLORS = {
    'primary': '#2196F3',      # 蓝色主色
    'success': '#4CAF50',      # 绿色正常
    'warning': '#FF9800',      # 橙色预警
    'danger': '#F44336',       # 红色危险
    'bg': '#FAFAFA',           # 背景色
    'grid': '#E0E0E0',        # 网格色
    'text': '#212121',         # 文字色
}


def _setup_chart_style(ax, fig):
    """统一图表样式"""
    ax.set_facecolor(COLORS['bg'])
    fig.patch.set_facecolor('white')
    ax.grid(True, alpha=0.3, color=COLORS['grid'], linestyle='-', linewidth=0.5)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color(COLORS['grid'])
    ax.spines['bottom'].set_color(COLORS['grid'])
    ax.tick_params(colors=COLORS['text'], labelsize=9)


def plot_device_trend(device_id, readings, anomalies=None, title=None):
    if not readings or len(readings) < 2:
        return None

    timestamps = []
    liquid_levels = []
    for r in readings:
        try:
            ts = datetime.strptime(r['recorded_at'], '%Y-%m-%d %H:%M:%S')
        except (ValueError, KeyError):
            continue
        ll = r.get('liquid_level')
        if ll is not None:
            timestamps.append(ts)
            liquid_levels.append(float(ll))

    if len(timestamps) < 2:
        return None

    timestamps = timestamps[::-1]
    liquid_levels = liquid_levels[::-1]

    fig, ax = plt.subplots(figsize=(10, 5))
    _setup_chart_style(ax, fig)

    # 填充区域
    ax.fill_between(timestamps, liquid_levels, alpha=0.15, color=COLORS['primary'])

    # 主线条
    line, = ax.plot(timestamps, liquid_levels, color=COLORS['primary'], linewidth=2.5,
                    marker='o', markersize=4, markerfacecolor='white', markeredgecolor=COLORS['primary'],
                    markeredgewidth=1.5, label='液位', zorder=3)

    # 均值线
    mean_ll = np.mean(liquid_levels)
    ax.axhline(y=mean_ll, color=COLORS['success'], linestyle='--', alpha=0.8, linewidth=1.5,
               label='均值 %.2fm' % mean_ll, zorder=2)

    # 阈值线
    ax.axhline(y=2.0, color=COLORS['warning'], linestyle=':', alpha=0.6, linewidth=1, label='警戒线 2.0m')
    ax.axhline(y=2.5, color=COLORS['danger'], linestyle=':', alpha=0.6, linewidth=1, label='危险线 2.5m')

    # 异常点
    if anomalies:
        anom_times = []
        anom_levels = []
        for a in anomalies:
            if a.get('device_id') == device_id:
                try:
                    at = datetime.strptime(a['recorded_at'], '%Y-%m-%d %H:%M:%S')
                    al = a.get('liquid_level', mean_ll)
                    anom_times.append(at)
                    anom_levels.append(float(al))
                except (ValueError, KeyError):
                    pass
        if anom_times:
            ax.scatter(anom_times, anom_levels, color=COLORS['danger'], s=100, zorder=5,
                       label='异常点', edgecolors='white', linewidths=2)

    # 当前值标注
    if liquid_levels:
        current = liquid_levels[-1]
        ax.annotate(f'{current:.3f}m', xy=(timestamps[-1], current),
                    xytext=(10, 10), textcoords='offset points',
                    fontsize=10, fontweight='bold', color=COLORS['primary'],
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor=COLORS['primary'], alpha=0.9),
                    arrowprops=dict(arrowstyle='->', color=COLORS['primary'], lw=1.5))

    ax.set_xlabel('时间', fontsize=10, color=COLORS['text'])
    ax.set_ylabel('液位 (m)', fontsize=10, color=COLORS['text'])
    ax.set_title(title or '%s 液位趋势' % device_id, fontsize=12, fontweight='bold',
                 color=COLORS['text'], pad=15)
    ax.legend(loc='upper right', fontsize=8, framealpha=0.9, edgecolor=COLORS['grid'])
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
    fig.autofmt_xdate(rotation=30)

    fig.tight_layout()
    return _save_chart(fig, 'trend_%s' % device_id.replace('/', '_'))


def plot_anomaly_overview(liquid_report, anomaly_result=None):
    categories = {}

    if liquid_report:
        for cat in ['consistency_anomalies', 'reversal_anomalies', 'frozen_anomalies',
                     'sudden_anomalies', 'rainfall_anomalies']:
            items = liquid_report.get(cat, [])
            if items:
                label = {
                    'consistency_anomalies': '水位偏离',
                    'reversal_anomalies': '流向异常',
                    'frozen_anomalies': '数据冻结',
                    'sudden_anomalies': '突变异常',
                    'rainfall_anomalies': '降雨异常',
                }.get(cat, cat)
                categories[label] = len(items)

    if anomaly_result:
        summary = anomaly_result.get('summary', {})
        extra = {
            '阈值超标': summary.get('threshold_count', 0),
            '溢满风险': summary.get('overflow_count', 0),
            '管道充满度': summary.get('fullness_count', 0),
            '电压异常': 0,
        }
        voltage = anomaly_result.get('voltage_status', {})
        medium_count = sum(1 for v in voltage.values() if v.get('severity') == 'medium')
        high_count = sum(1 for v in voltage.values() if v.get('severity') == 'high')
        extra['电压异常'] = medium_count + high_count
        for k, v in extra.items():
            if v > 0:
                categories[k] = v

    if not categories:
        return None

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    _setup_chart_style(ax1, fig)
    _setup_chart_style(ax2, fig)

    labels = list(categories.keys())
    values = list(categories.values())
    chart_colors = ['#F44336', '#FF9800', '#FFC107', '#4CAF50', '#2196F3', '#9C27B0'][:len(labels)]

    wedges, texts, autotexts = ax1.pie(
        values, labels=labels, autopct='%1.0f%%', colors=chart_colors, startangle=90,
        wedgeprops=dict(linewidth=1.5, edgecolor='white')
    )
    for t in texts:
        t.set_fontsize(9)
        t.set_color(COLORS['text'])
    for t in autotexts:
        t.set_fontsize(9)
        t.set_fontweight('bold')
    ax1.set_title('异常类型分布', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    bars = ax2.barh(labels, values, color=chart_colors, edgecolor='white', height=0.6)
    ax2.set_xlabel('数量', fontsize=10, color=COLORS['text'])
    ax2.set_title('各类异常数量', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)
    for bar, val in zip(bars, values):
        ax2.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                 str(val), va='center', fontsize=10, fontweight='bold', color=COLORS['text'])

    total = liquid_report.get('total', sum(values)) if liquid_report else sum(values)
    fig.suptitle('管网异常概览 (共 %d 条)' % total, fontsize=14, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.93])

    return _save_chart(fig, 'anomaly_overview')


def plot_component_comparison(component_stats, component_index=None):
    if not component_stats:
        return None

    if component_index is not None:
        comp = component_stats.get(str(component_index))
        if not comp:
            return None
        devices = comp.get('devices', {})
        if len(devices) < 2:
            return None

        fig, ax = plt.subplots(figsize=(10, 5))
        _setup_chart_style(ax, fig)
        names = list(devices.keys())
        levels = [devices[n]['avg_water_level'] for n in names]
        mean_level = comp.get('mean_water_level', np.mean(levels))

        chart_colors = [COLORS['danger'] if abs(l - mean_level) > 0.5 else COLORS['success'] for l in levels]
        bars = ax.bar(range(len(names)), levels, color=chart_colors, edgecolor='white', width=0.6)
        ax.axhline(y=mean_level, color=COLORS['primary'], linestyle='--', linewidth=2, label='组均值 %.2fm' % mean_level)
        ax.set_xticks(range(len(names)))
        ax.set_xticklabels([n.split('_')[-1] for n in names], rotation=45, ha='right', fontsize=8)
        ax.set_ylabel('水位标高 (m)', fontsize=10, color=COLORS['text'])
        ax.set_title('连通分量 #%d 水位对比' % component_index, fontsize=12, fontweight='bold', color=COLORS['text'])
        ax.legend(fontsize=9, framealpha=0.9)
        fig.tight_layout()
        return _save_chart(fig, 'comp_%d' % component_index)

    fig, axes = plt.subplots(1, min(3, len(component_stats)), figsize=(6 * min(3, len(component_stats)), 5))
    if len(component_stats) == 1:
        axes = [axes]

    sorted_comps = sorted(component_stats.items(), key=lambda x: x[1].get('device_count', 0), reverse=True)
    for ax, (ci, comp) in zip(axes, sorted_comps[:3]):
        _setup_chart_style(ax, fig)
        devices = comp.get('devices', {})
        names = list(devices.keys())[:10]
        if not names:
            continue
        levels = [devices[n]['avg_water_level'] for n in names]
        mean_level = comp.get('mean_water_level', np.mean(levels))
        chart_colors = [COLORS['danger'] if abs(l - mean_level) > 0.5 else COLORS['primary'] for l in levels]
        ax.barh(range(len(names)), levels, color=chart_colors, edgecolor='white', height=0.6)
        ax.set_yticks(range(len(names)))
        ax.set_yticklabels([n.split('_')[-1] for n in names], fontsize=7)
        ax.axvline(x=mean_level, color=COLORS['success'], linestyle='--', alpha=0.8, linewidth=1.5)
        ax.set_title('分量 #%s (%d台)' % (ci, comp.get('device_count', 0)), fontsize=10, fontweight='bold', color=COLORS['text'])
        ax.set_xlabel('水位 (m)', fontsize=9, color=COLORS['text'])

    fig.suptitle('连通分量水位对比', fontsize=13, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _save_chart(fig, 'components')


def plot_rainfall_response(rainfall_periods, device_readings_map):
    if not rainfall_periods:
        return None

    fig, ax1 = plt.subplots(figsize=(10, 5))
    _setup_chart_style(ax1, fig)
    ax2 = ax1.twinx()

    for rp in rainfall_periods[-3:]:
        try:
            start = datetime.strptime(rp['start'], '%Y-%m-%d %H:%M')
            end = datetime.strptime(rp['end'], '%Y-%m-%d %H:%M')
            ax1.axvspan(start, end, alpha=0.15, color=COLORS['primary'])
            ax1.text(start, ax1.get_ylim()[1] * 0.9, '%.1fmm' % rp.get('total_mm', 0),
                     fontsize=8, color=COLORS['primary'], fontweight='bold')
        except (ValueError, KeyError):
            pass

    chart_colors = [COLORS['primary'], COLORS['success'], COLORS['warning'], COLORS['danger'], '#9C27B0']
    for (did, readings), color in zip(list(device_readings_map.items())[:5], chart_colors):
        ts_list = []
        ll_list = []
        for r in readings:
            try:
                t = datetime.strptime(r['recorded_at'], '%Y-%m-%d %H:%M:%S')
                ll = r.get('liquid_level')
                if ll is not None:
                    ts_list.append(t)
                    ll_list.append(float(ll))
            except (ValueError, KeyError):
                pass
        if ts_list:
            ax2.plot(ts_list, ll_list, '-o', markersize=3, color=color, linewidth=1.5, label=did.split('_')[-1])

    ax1.set_xlabel('时间', fontsize=10, color=COLORS['text'])
    ax1.set_ylabel('降雨量 (mm)', color=COLORS['primary'], fontsize=10)
    ax2.set_ylabel('液位 (m)', fontsize=10, color=COLORS['text'])
    ax2.legend(loc='upper left', fontsize=8, framealpha=0.9)
    ax1.set_title('降雨响应分析', fontsize=12, fontweight='bold', color=COLORS['text'], pad=10)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    return _save_chart(fig, 'rainfall')


def plot_alert_heatmap(anomalies, bindings):
    if not anomalies:
        return None

    area_counts = defaultdict(int)
    for b in bindings:
        area_counts[b.get('area_name', '未知')] = 0

    device_area = {b['device_id']: b.get('area_name', '未知') for b in bindings}
    for a in anomalies:
        area = device_area.get(a.get('device_id', ''), '未知')
        area_counts[area] += 1

    areas = sorted(area_counts.keys(), key=lambda x: area_counts[x], reverse=True)
    counts = [area_counts[a] for a in areas]

    fig, ax = plt.subplots(figsize=(10, 5))
    _setup_chart_style(ax, fig)
    chart_colors = [COLORS['danger'] if c > 5 else COLORS['warning'] if c > 0 else COLORS['success'] for c in counts]
    bars = ax.barh(areas[:15], counts[:15], color=chart_colors[:15], edgecolor='white', height=0.6)
    ax.set_xlabel('异常数量', fontsize=10, color=COLORS['text'])
    ax.set_title('各区域异常分布', fontsize=12, fontweight='bold', color=COLORS['text'], pad=10)
    for bar, val in zip(bars, counts[:15]):
        if val > 0:
            ax.text(bar.get_width() + 0.2, bar.get_y() + bar.get_height() / 2,
                    str(val), va='center', fontsize=9, fontweight='bold', color=COLORS['text'])
    fig.tight_layout()
    return _save_chart(fig, 'heatmap')


def plot_device_detailed_trend(device_id, readings, anomalies=None):
    if not readings or len(readings) < 2:
        return None

    timestamps = []
    liquid_levels = []
    cods = []
    nh3ns = []
    voltages = []
    for r in readings:
        try:
            ts = datetime.strptime(r['recorded_at'], '%Y-%m-%d %H:%M:%S')
        except (ValueError, KeyError):
            continue
        timestamps.append(ts)
        liquid_levels.append(float(r.get('liquid_level')) if r.get('liquid_level') is not None else None)
        cods.append(float(r.get('cod')) if r.get('cod') is not None else None)
        nh3ns.append(float(r.get('ammonia_n')) if r.get('ammonia_n') is not None else None)
        voltages.append(float(r.get('voltage')) if r.get('voltage') is not None else None)

    if len(timestamps) < 2:
        return None

    timestamps = timestamps[::-1]
    liquid_levels = liquid_levels[::-1]
    cods = cods[::-1]
    nh3ns = nh3ns[::-1]
    voltages = voltages[::-1]

    has_cod = any(v is not None for v in cods)
    has_nh3 = any(v is not None for v in nh3ns)
    has_vol = any(v is not None for v in voltages)

    n_plots = 1 + sum([has_cod, has_nh3, has_vol])
    fig, axes = plt.subplots(n_plots, 1, figsize=(10, 3 * n_plots), sharex=True)
    if n_plots == 1:
        axes = [axes]

    idx = 0
    valid_ll = [v for v in liquid_levels if v is not None]
    ax = axes[idx]
    _setup_chart_style(ax, fig)
    ll_plot = [v if v is not None else np.nan for v in liquid_levels]
    ax.fill_between(timestamps, ll_plot, alpha=0.1, color=COLORS['primary'])
    ax.plot(timestamps, ll_plot, color=COLORS['primary'], linewidth=2, marker='o', markersize=3,
            markerfacecolor='white', markeredgecolor=COLORS['primary'], label='液位(m)')
    if valid_ll:
        mean_ll = np.mean(valid_ll)
        ax.axhline(y=mean_ll, color=COLORS['success'], linestyle='--', alpha=0.8, label='均值%.2fm' % mean_ll)
    ax.axhline(y=2.0, color=COLORS['warning'], linestyle=':', alpha=0.5, linewidth=1)
    ax.axhline(y=2.5, color=COLORS['danger'], linestyle=':', alpha=0.5, linewidth=1)
    if anomalies:
        anom_times = []
        anom_vals = []
        for a in anomalies:
            if a.get('device_id') == device_id:
                try:
                    at = datetime.strptime(a['recorded_at'], '%Y-%m-%d %H:%M:%S')
                    al = a.get('liquid_level')
                    if al is not None:
                        anom_times.append(at)
                        anom_vals.append(float(al))
                except (ValueError, KeyError):
                    pass
        if anom_times:
            ax.scatter(anom_times, anom_vals, color=COLORS['danger'], s=60, zorder=5, label='异常点',
                       edgecolors='white', linewidths=1.5)
    ax.set_ylabel('液位 (m)', fontsize=9, color=COLORS['text'])
    ax.legend(loc='upper right', fontsize=8, framealpha=0.9)
    idx += 1

    metric_colors = {'cod': '#FF5722', 'ammonia_n': '#4CAF50', 'voltage': '#9C27B0'}
    metric_labels = {'cod': 'COD(mg/L)', 'ammonia_n': '氨氮(mg/L)', 'voltage': '电压(V)'}
    metric_data = {'cod': (has_cod, cods), 'ammonia_n': (has_nh3, nh3ns), 'voltage': (has_vol, voltages)}

    for metric, (has_data, data) in metric_data.items():
        if not has_data:
            continue
        ax = axes[idx]
        _setup_chart_style(ax, fig)
        plot_data = [v if v is not None else np.nan for v in data]
        ax.plot(timestamps, plot_data, color=metric_colors[metric], linewidth=1.5, marker='o', markersize=2, label=metric_labels[metric])
        ax.set_ylabel(metric_labels[metric], fontsize=9, color=COLORS['text'])
        ax.legend(loc='upper right', fontsize=8, framealpha=0.9)
        idx += 1

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    axes[-1].xaxis.set_major_locator(mdates.HourLocator(interval=6))
    fig.autofmt_xdate(rotation=30)
    fig.suptitle('设备 %s 监测数据趋势' % device_id, fontsize=13, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    return _save_chart(fig, 'detail_%s' % device_id.replace('/', '_'))


def plot_system_overview(stats):
    if not stats:
        return None

    device_count = stats.get('device_count', 0)
    active_count = stats.get('active_count', 0)
    anomaly_count = stats.get('anomaly_count', 0)
    online_count = stats.get('online_count', 0)
    area_stats = stats.get('area_stats', {})

    fig, axes = plt.subplots(1, 3, figsize=(14, 5))

    ax = axes[0]
    _setup_chart_style(ax, fig)
    labels = ['在线', '离线']
    sizes = [online_count, device_count - online_count]
    chart_colors = [COLORS['success'], COLORS['danger']]
    if sizes[1] <= 0:
        labels = ['在线']
        sizes = [online_count]
    wedges, texts, autotexts = ax.pie(sizes, labels=labels, autopct='%1.0f%%',
                                       colors=chart_colors[:len(sizes)], startangle=90,
                                       wedgeprops=dict(linewidth=1.5, edgecolor='white'))
    for t in texts + autotexts:
        t.set_fontsize(9)
        t.set_color(COLORS['text'])
    ax.set_title('设备在线率\n(%d/%d)' % (online_count, device_count), fontsize=11, fontweight='bold', color=COLORS['text'])

    ax = axes[1]
    _setup_chart_style(ax, fig)
    status_labels = ['活跃', '异常']
    status_vals = [active_count - anomaly_count, anomaly_count]
    if status_vals[0] < 0:
        status_vals[0] = 0
    bars = ax.bar(status_labels, status_vals, color=[COLORS['primary'], COLORS['danger']], edgecolor='white', width=0.5)
    for bar, val in zip(bars, status_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.5,
                str(val), ha='center', fontsize=11, fontweight='bold', color=COLORS['text'])
    ax.set_ylabel('设备数', fontsize=10, color=COLORS['text'])
    ax.set_title('设备状态', fontsize=11, fontweight='bold', color=COLORS['text'])

    ax = axes[2]
    _setup_chart_style(ax, fig)
    if area_stats:
        sorted_areas = sorted(area_stats.items(), key=lambda x: x[1].get('anomaly_count', 0), reverse=True)
        names = [a[0][:6] for a in sorted_areas[:8]]
        a_counts = [a[1].get('anomaly_count', 0) for a in sorted_areas[:8]]
        chart_colors = [COLORS['danger'] if c > 3 else COLORS['warning'] if c > 0 else COLORS['success'] for c in a_counts]
        ax.barh(names, a_counts, color=chart_colors, edgecolor='white', height=0.6)
        ax.set_xlabel('异常数', fontsize=10, color=COLORS['text'])
        ax.set_title('区域异常分布', fontsize=11, fontweight='bold', color=COLORS['text'])
    else:
        ax.text(0.5, 0.5, '无区域数据', ha='center', va='center', fontsize=12, color=COLORS['text'])
        ax.set_title('区域异常分布', fontsize=11, fontweight='bold', color=COLORS['text'])

    fig.suptitle('管网监测系统概览', fontsize=14, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _save_chart(fig, 'overview')


def plot_alert_priority(anomalies):
    if not anomalies:
        return None

    severity_map = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
    for a in anomalies:
        sev = a.get('severity', 'low')
        if sev in severity_map:
            severity_map[sev] += 1

    labels = ['严重', '高', '中', '低']
    values = [severity_map['critical'], severity_map['high'], severity_map['medium'], severity_map['low']]
    chart_colors = [COLORS['danger'], COLORS['warning'], '#FFC107', COLORS['success']]

    filtered = [(l, v, c) for l, v, c in zip(labels, values, chart_colors) if v > 0]
    if not filtered:
        return None

    labels_f, values_f, colors_f = zip(*filtered)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    _setup_chart_style(ax1, fig)
    _setup_chart_style(ax2, fig)

    wedges, texts, autotexts = ax1.pie(values_f, labels=labels_f, autopct='%1.0f%%',
                                        colors=colors_f, startangle=90,
                                        wedgeprops=dict(linewidth=1.5, edgecolor='white'))
    for t in texts + autotexts:
        t.set_fontsize(10)
        t.set_color(COLORS['text'])
    ax1.set_title('告警严重度分布', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    bars = ax2.bar(labels_f, values_f, color=colors_f, edgecolor='white', width=0.5)
    for bar, val in zip(bars, values_f):
        ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                 str(val), ha='center', fontsize=11, fontweight='bold', color=COLORS['text'])
    ax2.set_ylabel('告警数量', fontsize=10, color=COLORS['text'])
    ax2.set_title('各级别告警数', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    total = sum(values_f)
    fig.suptitle('告警优先级分析 (共 %d 条)' % total, fontsize=14, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _save_chart(fig, 'alert_priority')


def plot_report_summary(liquid_report):
    """管网液位工程分析报告综合图表"""
    if not liquid_report:
        return None

    cat_map = {
        'consistency_anomalies': '水位偏离',
        'reversal_anomalies': '流向异常',
        'frozen_anomalies': '数据冻结',
        'sudden_anomalies': '突变异常',
        'rainfall_anomalies': '降雨异常',
    }
    cat_colors = {
        'consistency_anomalies': '#F44336',
        'reversal_anomalies': '#FF9800',
        'frozen_anomalies': '#2196F3',
        'sudden_anomalies': '#FFC107',
        'rainfall_anomalies': '#4CAF50',
    }

    # 收集各类异常数量
    cat_labels = []
    cat_values = []
    cat_colors_list = []
    all_anomalies = []
    for cat_key, label in cat_map.items():
        items = liquid_report.get(cat_key, [])
        if items:
            cat_labels.append(label)
            cat_values.append(len(items))
            cat_colors_list.append(cat_colors[cat_key])
            all_anomalies.extend(items)

    if not cat_labels:
        return None

    total = liquid_report.get('total', sum(cat_values))

    # 严重程度统计
    sev_map = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
    for a in all_anomalies:
        sev = a.get('severity', 'low')
        if sev in sev_map:
            sev_map[sev] += 1
    sev_labels = ['严重', '高', '中', '低']
    sev_values = [sev_map['critical'], sev_map['high'], sev_map['medium'], sev_map['low']]
    sev_colors = [COLORS['danger'], COLORS['warning'], '#FFC107', COLORS['success']]
    sev_filtered = [(l, v, c) for l, v, c in zip(sev_labels, sev_values, sev_colors) if v > 0]

    # 区域 TOP10
    area_counts = defaultdict(int)
    for a in all_anomalies:
        area = a.get('area_name', a.get('node_id', '未知'))[:6]
        area_counts[area] += 1
    area_sorted = sorted(area_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # 左上：异常类型饼图
    ax = axes[0][0]
    _setup_chart_style(ax, fig)
    wedges, texts, autotexts = ax.pie(
        cat_values, labels=cat_labels, autopct='%1.0f%%',
        colors=cat_colors_list, startangle=90,
        wedgeprops=dict(linewidth=1.5, edgecolor='white'))
    for t in texts + autotexts:
        t.set_fontsize(9)
        t.set_color(COLORS['text'])
    ax.set_title('异常类型分布', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    # 右上：各类异常数量
    ax = axes[0][1]
    _setup_chart_style(ax, fig)
    bars = ax.barh(cat_labels, cat_values, color=cat_colors_list, edgecolor='white', height=0.6)
    for bar, val in zip(bars, cat_values):
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                str(val), va='center', fontsize=10, fontweight='bold', color=COLORS['text'])
    ax.set_xlabel('数量', fontsize=10, color=COLORS['text'])
    ax.set_title('各类异常数量', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    # 左下：严重程度分布
    ax = axes[1][0]
    _setup_chart_style(ax, fig)
    if sev_filtered:
        s_labels, s_values, s_colors = zip(*sev_filtered)
        bars = ax.bar(s_labels, s_values, color=s_colors, edgecolor='white', width=0.5)
        for bar, val in zip(bars, s_values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    str(val), ha='center', fontsize=10, fontweight='bold', color=COLORS['text'])
        ax.set_ylabel('数量', fontsize=10, color=COLORS['text'])
    ax.set_title('严重程度分布', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    # 右下：区域 TOP10
    ax = axes[1][1]
    _setup_chart_style(ax, fig)
    if area_sorted:
        a_names = [a[0] for a in area_sorted]
        a_counts = [a[1] for a in area_sorted]
        a_colors = [COLORS['danger'] if c > 5 else COLORS['warning'] if c > 0 else COLORS['success'] for c in a_counts]
        bars = ax.barh(a_names, a_counts, color=a_colors, edgecolor='white', height=0.6)
        for bar, val in zip(bars, a_counts):
            if val > 0:
                ax.text(bar.get_width() + 0.2, bar.get_y() + bar.get_height() / 2,
                        str(val), va='center', fontsize=9, fontweight='bold', color=COLORS['text'])
        ax.set_xlabel('异常数', fontsize=10, color=COLORS['text'])
    ax.set_title('区域异常 TOP10', fontsize=11, fontweight='bold', color=COLORS['text'], pad=10)

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    fig.suptitle(f'管网液位工程分析报告 | {now_str} (共 {total} 条)',
                 fontsize=14, fontweight='bold', color=COLORS['text'])
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    return _save_chart(fig, 'report_summary')
