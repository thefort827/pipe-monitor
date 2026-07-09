import config
import weather_service
import liquid_analysis


def detect_threshold_anomalies(readings):
    anomalies = []
    for r in readings:
        reasons = []
        if r.get('liquid_level') and r['liquid_level'] > config.THRESHOLDS['liquid_level']:
            reasons.append(f"液位={r['liquid_level']:.3f}m > {config.THRESHOLDS['liquid_level']}m")
        if r.get('cod') and r['cod'] > config.THRESHOLDS['cod']:
            reasons.append(f"COD={r['cod']:.1f}mg/L > {config.THRESHOLDS['cod']}mg/L")
        if r.get('ammonia_n') and r['ammonia_n'] > config.THRESHOLDS['ammonia_n']:
            reasons.append(f"氨氮={r['ammonia_n']:.1f}mg/L > {config.THRESHOLDS['ammonia_n']}mg/L")

        if reasons:
            anomalies.append({
                'device_id': r['device_id'],
                'recorded_at': r['recorded_at'],
                'type': 'threshold',
                'reasons': reasons,
                'severity': 'high' if len(reasons) > 1 else 'medium'
            })
    return anomalies


def detect_overflow_risk(bindings, readings):
    anomalies = []
    device_readings = {}
    for r in readings:
        did = r['device_id']
        if did not in device_readings:
            device_readings[did] = []
        device_readings[did].append(r)

    for b in bindings:
        if not b.get('bound_node'):
            continue

        did = b['device_id']
        node = b['bound_node']
        ground_elev = node.get('ground_elev')
        well_bottom_elev = node.get('well_bottom_elev')

        if ground_elev is None or well_bottom_elev is None:
            continue

        dev_readings = device_readings.get(did, [])[:5]
        for r in dev_readings:
            level = r.get('liquid_level')
            if level is None:
                continue

            water_level = well_bottom_elev + level
            overflow_risk = ground_elev - water_level

            if overflow_risk < config.OVERFLOW_THRESHOLDS['critical']:
                anomalies.append({
                    'device_id': did,
                    'node_id': node['point_id'],
                    'recorded_at': r['recorded_at'],
                    'type': 'overflow',
                    'severity': 'critical',
                    'ground_elev': round(ground_elev, 2),
                    'well_bottom_elev': round(well_bottom_elev, 2),
                    'liquid_level': round(level, 3),
                    'water_level': round(water_level, 2),
                    'overflow_risk': round(overflow_risk, 2),
                    'reason': f"水位({water_level:.2f}m)已超过地面高程({ground_elev:.2f}m)，溢出风险{overflow_risk:.2f}m"
                })
            elif overflow_risk < config.OVERFLOW_THRESHOLDS['high']:
                anomalies.append({
                    'device_id': did,
                    'node_id': node['point_id'],
                    'recorded_at': r['recorded_at'],
                    'type': 'overflow',
                    'severity': 'high',
                    'ground_elev': round(ground_elev, 2),
                    'well_bottom_elev': round(well_bottom_elev, 2),
                    'liquid_level': round(level, 3),
                    'water_level': round(water_level, 2),
                    'overflow_risk': round(overflow_risk, 2),
                    'reason': f"水位({water_level:.2f}m)距地面仅{overflow_risk:.2f}m，高溢出风险"
                })
            elif overflow_risk < config.OVERFLOW_THRESHOLDS['medium']:
                anomalies.append({
                    'device_id': did,
                    'node_id': node['point_id'],
                    'recorded_at': r['recorded_at'],
                    'type': 'overflow',
                    'severity': 'medium',
                    'ground_elev': round(ground_elev, 2),
                    'well_bottom_elev': round(well_bottom_elev, 2),
                    'liquid_level': round(level, 3),
                    'water_level': round(water_level, 2),
                    'overflow_risk': round(overflow_risk, 2),
                    'reason': f"水位({water_level:.2f}m)距地面{overflow_risk:.2f}m，中等溢出风险"
                })

    return anomalies


def detect_hydraulic_slope_anomaly(bindings, pipes, node_map, readings):
    anomalies = []
    device_to_node = {}
    for b in bindings:
        if b.get('bound_node'):
            device_to_node[b['device_id']] = b['bound_node']['point_id']

    device_readings = {}
    for r in readings:
        did = r['device_id']
        if did not in device_readings:
            device_readings[did] = []
        device_readings[did].append(r)

    for did in device_readings:
        device_readings[did].sort(key=lambda x: x['recorded_at'], reverse=True)

    for pipe in pipes:
        start_id, end_id = pipe['start_id'], pipe['end_id']
        devices_at_start = [did for did, nid in device_to_node.items() if nid == start_id]
        devices_at_end = [did for did, nid in device_to_node.items() if nid == end_id]

        up_node = node_map.get(start_id)
        down_node = node_map.get(end_id)

        if not up_node or not down_node:
            continue

        up_elev = up_node.get('well_bottom_elev')
        down_elev = down_node.get('well_bottom_elev')

        if up_elev is None or down_elev is None:
            continue

        for d_up in devices_at_start:
            for d_down in devices_at_end:
                up_data = device_readings.get(d_up, [])[:5]
                down_data = device_readings.get(d_down, [])[:5]

                if not up_data or not down_data:
                    continue

                for u_row in up_data:
                    best_match = None
                    best_diff = float('inf')
                    for d_row in down_data:
                        if d_row['recorded_at'] and u_row['recorded_at']:
                            diff = abs(ord_time(d_row['recorded_at']) - ord_time(u_row['recorded_at']))
                            if diff < best_diff:
                                best_diff = diff
                                best_match = d_row

                    if not best_match:
                        continue

                    up_level = u_row.get('liquid_level')
                    down_level = best_match.get('liquid_level')

                    if up_level is None or down_level is None:
                        continue

                    up_water = up_elev + up_level
                    down_water = down_elev + down_level
                    water_diff = up_water - down_water

                    if water_diff < 0:
                        anomalies.append({
                            'type': 'hydraulic_slope',
                            'severity': 'high',
                            'pipe_info': f"{start_id} -> {end_id}",
                            'upstream_device': d_up,
                            'downstream_device': d_down,
                            'up_elev': round(up_elev, 2),
                            'down_elev': round(down_elev, 2),
                            'up_level': round(up_level, 3),
                            'down_level': round(down_level, 3),
                            'up_water': round(up_water, 2),
                            'down_water': round(down_water, 2),
                            'water_diff': round(water_diff, 2),
                            'recorded_at': u_row['recorded_at'],
                            'reason': f"上游水位({up_water:.2f}m)低于下游({down_water:.2f}m)，水力坡降异常，可能下游堵塞"
                        })

    return anomalies


def detect_pipe_fullness(bindings, readings, pipes):
    anomalies = []
    device_to_node = {}
    for b in bindings:
        if b.get('bound_node'):
            device_to_node[b['device_id']] = b['bound_node']['point_id']

    device_readings = {}
    for r in readings:
        did = r['device_id']
        if did not in device_readings:
            device_readings[did] = []
        device_readings[did].append(r)

    node_to_diameter = {}
    for pipe in pipes:
        start_id = pipe['start_id']
        end_id = pipe['end_id']
        diameter = pipe.get('diameter', '')
        if diameter:
            try:
                if diameter.startswith('d') or diameter.startswith('D'):
                    d_val = float(diameter[1:])
                elif diameter.startswith('DN'):
                    d_val = float(diameter[2:])
                else:
                    d_val = float(diameter)
                node_to_diameter[start_id] = d_val
                node_to_diameter[end_id] = d_val
            except (ValueError, TypeError):
                pass

    for did, nid in device_to_node.items():
        diameter = node_to_diameter.get(nid)
        if not diameter:
            continue

        dev_readings = device_readings.get(did, [])[:5]
        for r in dev_readings:
            level = r.get('liquid_level')
            if level is None:
                continue

            fullness = level / (diameter / 1000)

            if fullness > 1.0:
                anomalies.append({
                    'device_id': did,
                    'node_id': nid,
                    'recorded_at': r['recorded_at'],
                    'type': 'pipe_fullness',
                    'severity': 'high',
                    'diameter': diameter,
                    'liquid_level': round(level, 3),
                    'fullness': round(fullness, 2),
                    'reason': f"管道充满度{fullness:.0%}，已超载（管径{diameter}mm）"
                })
            elif fullness > config.PIPE_FULLNESS_THRESHOLDS['high']:
                anomalies.append({
                    'device_id': did,
                    'node_id': nid,
                    'recorded_at': r['recorded_at'],
                    'type': 'pipe_fullness',
                    'severity': 'medium',
                    'diameter': diameter,
                    'liquid_level': round(level, 3),
                    'fullness': round(fullness, 2),
                    'reason': f"管道充满度{fullness:.0%}，接近满流（管径{diameter}mm）"
                })

    return anomalies


def detect_upstream_downstream_anomalies(bindings, pipes, node_map, readings, precip_cache=None):
    device_to_node = {}
    device_info = {}
    for b in bindings:
        if b.get('bound_node'):
            device_to_node[b['device_id']] = b['bound_node']['point_id']
            device_info[b['device_id']] = {
                'lat': b['latitude'],
                'lon': b['longitude']
            }

    device_readings = {}
    for r in readings:
        did = r['device_id']
        if did not in device_readings:
            device_readings[did] = []
        device_readings[did].append(r)

    for did in device_readings:
        device_readings[did].sort(key=lambda x: x['recorded_at'], reverse=True)

    if precip_cache is None:
        precip_cache = {}

    anomalies = []
    for pipe in pipes:
        start_id, end_id = pipe['start_id'], pipe['end_id']
        devices_at_start = [did for did, nid in device_to_node.items() if nid == start_id]
        devices_at_end = [did for did, nid in device_to_node.items() if nid == end_id]

        up_node = node_map.get(start_id)
        down_node = node_map.get(end_id)

        for d_up in devices_at_start:
            for d_down in devices_at_end:
                up_data = device_readings.get(d_up, [])[:10]
                down_data = device_readings.get(d_down, [])[:10]

                if not up_data or not down_data:
                    continue

                for u_row in up_data:
                    best_match = None
                    best_diff = float('inf')
                    for d_row in down_data:
                        if d_row['recorded_at'] and u_row['recorded_at']:
                            diff = abs(ord_time(d_row['recorded_at']) - ord_time(u_row['recorded_at']))
                            if diff < best_diff:
                                best_diff = diff
                                best_match = d_row

                    if not best_match:
                        continue

                    up_level = u_row.get('liquid_level')
                    down_level = best_match.get('liquid_level')

                    if up_level is None or down_level is None:
                        continue

                    up_elev = up_node.get('well_bottom_elev') if up_node else None
                    down_elev = down_node.get('well_bottom_elev') if down_node else None

                    if up_elev is not None and down_elev is not None:
                        up_water = up_elev + up_level
                        down_water = down_elev + down_level

                        if up_water < down_water - 0.1:
                            precip_info = get_precip_for_time(
                                device_info.get(d_up, {}), u_row['recorded_at'], precip_cache
                            )
                            anomalies.append({
                                'type': 'upstream_downstream',
                                'severity': 'high',
                                'pipe_info': f"{start_id} -> {end_id}",
                                'upstream_device': d_up,
                                'downstream_device': d_down,
                                'metric': 'water_level',
                                'up_level': round(up_level, 3),
                                'down_level': round(down_level, 3),
                                'up_elev': round(up_elev, 2),
                                'down_elev': round(down_elev, 2),
                                'up_water': round(up_water, 2),
                                'down_water': round(down_water, 2),
                                'recorded_at': u_row['recorded_at'],
                                'precipitation': precip_info,
                                'reason': f"上游水位({up_water:.2f}m) < 下游水位({down_water:.2f}m)，可能下游堵塞或上游漏水"
                            })
                    else:
                        if up_level < down_level * 0.8:
                            anomalies.append({
                                'type': 'upstream_downstream',
                                'severity': 'high',
                                'pipe_info': f"{start_id} -> {end_id}",
                                'upstream_device': d_up,
                                'downstream_device': d_down,
                                'metric': 'liquid_level',
                                'up_level': round(up_level, 3),
                                'down_level': round(down_level, 3),
                                'up_elev': None,
                                'down_elev': None,
                                'up_water': None,
                                'down_water': None,
                                'recorded_at': u_row['recorded_at'],
                                'precipitation': None,
                                'reason': f"上游液位({up_level:.3f}m) < 下游({down_level:.3f}m)，可能管线堵塞或设备异常"
                            })

                    if (u_row.get('cod') and best_match.get('cod') and
                            u_row['cod'] > 0 and best_match['cod'] > 0):
                        ratio = abs(u_row['cod'] - best_match['cod']) / max(u_row['cod'], best_match['cod'])
                        if ratio > 0.5:
                            anomalies.append({
                                'type': 'upstream_downstream',
                                'severity': 'medium',
                                'pipe_info': f"{start_id} -> {end_id}",
                                'upstream_device': d_up,
                                'downstream_device': d_down,
                                'metric': 'cod',
                                'up_value': round(u_row['cod'], 1),
                                'down_value': round(best_match['cod'], 1),
                                'recorded_at': u_row['recorded_at'],
                                'precipitation': None,
                                'reason': f"上下游COD差异过大({ratio * 100:.0f}%)，可能有新增排放或设备漂移"
                            })
    return anomalies


def get_precip_for_time(device_info, time_str, precip_cache):
    if not device_info or not time_str:
        return None

    lat = device_info.get('lat')
    lon = device_info.get('lon')
    if lat is None or lon is None:
        return None

    cache_key = f"{round(lat, 2)}_{round(lon, 2)}"
    if cache_key not in precip_cache:
        precip_cache[cache_key] = weather_service.get_precipitation(lat, lon, past_days=7)

    precip_data = precip_cache[cache_key]
    return weather_service.find_precipitation_for_time(precip_data, time_str)


def ord_time(t):
    if isinstance(t, str):
        return sum(int(x) * m for x, m in zip(t.replace('-', ' ').replace(':', ' ').split()[:6],
                                                [100000000, 1000000, 10000, 100, 1, 0]))
    return 0


def detect_mismatch_anomalies(bindings):
    anomalies = []
    expected_mapping = {
        '小区雨污水出口': ['WS', 'YS'],
        '截污管接入口': ['WS'],
        '道路交汇口': ['YS', 'WS'],
    }

    for b in bindings:
        if not b.get('bound_node'):
            if b.get('distance') and b['distance'] > config.BIND_DISTANCE_MAX:
                anomalies.append({
                    'device_id': b['device_id'],
                    'type': 'no_binding',
                    'severity': 'low',
                    'distance': b.get('distance'),
                    'reason': f"设备距离最近管点{b['distance']}m，超过{config.BIND_DISTANCE_MAX}m阈值，未绑定"
                })
            else:
                anomalies.append({
                    'device_id': b['device_id'],
                    'type': 'no_binding',
                    'severity': 'low',
                    'distance': b.get('distance'),
                    'reason': f"设备无坐标或附近无管点"
                })
            continue

        dev_type = b.get('device_type', '')
        node_sub = b['bound_node'].get('sub_type', '')
        distance = b.get('distance', 0)

        if dev_type in expected_mapping:
            expected = expected_mapping[dev_type]
            if node_sub and node_sub not in expected:
                anomalies.append({
                    'device_id': b['device_id'],
                    'type': 'type_mismatch',
                    'severity': 'high',
                    'distance': distance,
                    'reason': f"设备类型'{dev_type}'应监测{'/'.join(expected)}，但绑定管点子类型为'{node_sub}'"
                })
    return anomalies


def detect_offline_devices(bindings, online_stats):
    anomalies = []
    for b in bindings:
        did = b['device_id']
        stats = online_stats.get(did)
        if stats:
            if stats['online_rate'] < 50:
                anomalies.append({
                    'device_id': did,
                    'type': 'offline',
                    'severity': 'high',
                    'online_rate': stats['online_rate'],
                    'last_seen': stats['last_seen'],
                    'reason': f"设备在线率仅{stats['online_rate']}%，通信异常"
                })
            elif stats['online_rate'] < 80:
                anomalies.append({
                    'device_id': did,
                    'type': 'offline',
                    'severity': 'medium',
                    'online_rate': stats['online_rate'],
                    'last_seen': stats['last_seen'],
                    'reason': f"设备在线率{stats['online_rate']}%，存在通信不稳定"
                })
    return anomalies


def analyze_temperature_correlation(readings):
    device_temps = {}
    device_levels = {}
    for r in readings:
        did = r['device_id']
        temp = r.get('temperature')
        level = r.get('liquid_level')
        if temp is not None and level is not None:
            if did not in device_temps:
                device_temps[did] = []
                device_levels[did] = []
            device_temps[did].append(temp)
            device_levels[did].append(level)

    correlations = {}
    for did in device_temps:
        temps = device_temps[did]
        levels = device_levels[did]
        if len(temps) > 10:
            avg_temp = sum(temps) / len(temps)
            avg_level = sum(levels) / len(levels)
            temp_var = sum((t - avg_temp) ** 2 for t in temps) / len(temps)
            level_var = sum((l - avg_level) ** 2 for l in levels) / len(levels)
            if temp_var > 0 and level_var > 0:
                cov = sum((t - avg_temp) * (l - avg_level) for t, l in zip(temps, levels)) / len(temps)
                corr = cov / (temp_var ** 0.5 * level_var ** 0.5)
                correlations[did] = {
                    'correlation': round(corr, 3),
                    'avg_temp': round(avg_temp, 1),
                    'sample_count': len(temps)
                }

    return correlations


def analyze_voltage_status(readings):
    device_voltages = {}
    for r in readings:
        did = r['device_id']
        voltage = r.get('voltage')
        if voltage is not None:
            if did not in device_voltages:
                device_voltages[did] = []
            device_voltages[did].append(voltage)

    voltage_status = {}
    for did, voltages in device_voltages.items():
        if voltages:
            avg_voltage = sum(voltages) / len(voltages)
            min_voltage = min(voltages)
            if avg_voltage < 3.5:
                status = 'low'
                severity = 'high'
            elif avg_voltage < 3.7:
                status = 'medium'
                severity = 'medium'
            else:
                status = 'normal'
                severity = 'low'
            voltage_status[did] = {
                'avg_voltage': round(avg_voltage, 2),
                'min_voltage': round(min_voltage, 2),
                'status': status,
                'severity': severity
            }

    return voltage_status


def run_all_checks(bindings, pipes, node_map, readings):
    precip_cache = {}
    threshold_anomalies = detect_threshold_anomalies(readings)
    updown_anomalies = detect_upstream_downstream_anomalies(bindings, pipes, node_map, readings, precip_cache)
    mismatch_anomalies = detect_mismatch_anomalies(bindings)
    overflow_anomalies = detect_overflow_risk(bindings, readings)
    hydraulic_anomalies = detect_hydraulic_slope_anomaly(bindings, pipes, node_map, readings)
    fullness_anomalies = detect_pipe_fullness(bindings, readings, pipes)

    temp_correlations = analyze_temperature_correlation(readings)
    voltage_status = analyze_voltage_status(readings)

    # ---- 液位数据分析（连通器原理）- 仅分析活跃设备 ----
    active_bindings, inactive_bindings = liquid_analysis.get_active_devices(bindings, hours=48)
    adjacency, components = liquid_analysis.build_pipe_graph(pipes, node_map)
    component_devices, unconnected_devices = liquid_analysis.assign_devices_to_components(
        active_bindings, node_map, components
    )
    consistency_anomalies, component_stats = liquid_analysis.analyze_component_water_levels(
        component_devices, node_map
    )
    reversal_anomalies = liquid_analysis.detect_water_reversal(active_bindings, pipes, node_map)
    frozen_anomalies = liquid_analysis.detect_frozen_data(active_bindings)
    sudden_anomalies = liquid_analysis.detect_level_sudden_change(active_bindings)

    liquid_anomalies = consistency_anomalies + reversal_anomalies + frozen_anomalies + sudden_anomalies
    # 过滤掉非活跃设备的异常
    liquid_anomalies = [a for a in liquid_anomalies
                        if a.get('device_id') in {b['device_id'] for b in active_bindings}]

    return {
        'threshold': threshold_anomalies,
        'upstream_downstream': updown_anomalies,
        'mismatch': mismatch_anomalies,
        'overflow': overflow_anomalies,
        'hydraulic_slope': hydraulic_anomalies,
        'pipe_fullness': fullness_anomalies,
        'temp_correlations': temp_correlations,
        'voltage_status': voltage_status,
        'liquid_analysis': {
            'active_device_count': len(active_bindings),
            'inactive_device_count': len(inactive_bindings),
            'component_count': len(components),
            'component_device_count': sum(len(v) for v in component_devices.values()),
            'consistency_anomalies': consistency_anomalies,
            'reversal_anomalies': reversal_anomalies,
            'frozen_anomalies': frozen_anomalies,
            'sudden_anomalies': sudden_anomalies,
            'liquid_anomaly_count': len(liquid_anomalies)
        },
        'summary': {
            'threshold_count': len(threshold_anomalies),
            'updown_count': len(updown_anomalies),
            'mismatch_count': len(mismatch_anomalies),
            'overflow_count': len(overflow_anomalies),
            'hydraulic_count': len(hydraulic_anomalies),
            'fullness_count': len(fullness_anomalies),
            'liquid_count': len(liquid_anomalies),
            'total': (len(threshold_anomalies) + len(updown_anomalies) +
                      len(mismatch_anomalies) + len(overflow_anomalies) +
                      len(hydraulic_anomalies) + len(fullness_anomalies) +
                      len(liquid_anomalies))
        }
    }
