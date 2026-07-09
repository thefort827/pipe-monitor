// Ensure Chart.js is available before any chart code runs
if (!window.Chart && !document.querySelector('script[src*="chart-bootstrap"]')) {
    var _cs = document.createElement('script');
    _cs.src = '/static/js/chart-bootstrap.js';
    document.head.appendChild(_cs);
}

const CHART_JS_CDN = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js';
const AUTONAVI_RD_URL = 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
const AUTONAVI_SATELLITE_URL = 'https://webst01.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}';

let map;
let deviceLayer, nodeLayer, pipeLayer;
let allDevices = [];
let deviceTypes = new Set();
let currentTileLayer = null;
let currentTileName = '街道图';
let rawDevicesGeoJSON = null;
let rawNodesGeoJSON = null;
let rawPipesGeoJSON = null;

// WGS84到GCJ-02坐标转换
function wgs84ToGcj02(lng, lat) {
    const PI = Math.PI;
    const A = 6378245.0;
    const EE = 0.00669342162296594323;
    
    if (lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271) {
        return { lat, lng };
    }
    
    let dLat = transformLat(lng - 105.0, lat - 35.0);
    let dLng = transformLng(lng - 105.0, lat - 35.0);
    
    const radLat = lat / 180.0 * PI;
    let magic = Math.sin(radLat);
    magic = 1 - EE * magic * magic;
    const sqrtMagic = Math.sqrt(magic);
    
    dLat = (dLat * 180.0) / ((A * (1 - EE)) / (magic * sqrtMagic) * PI);
    dLng = (dLng * 180.0) / (A / sqrtMagic * Math.cos(radLat) * PI);
    
    return {
        lat: lat + dLat,
        lng: lng + dLng
    };
}

function transformLat(x, y) {
    let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin(y / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (160.0 * Math.sin(y / 12.0 * Math.PI) + 320.0 * Math.sin(y * Math.PI / 30.0)) * 2.0 / 3.0;
    return ret;
}

function transformLng(x, y) {
    let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    ret += (20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0 / 3.0;
    ret += (20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin(x / 3.0 * Math.PI)) * 2.0 / 3.0;
    ret += (150.0 * Math.sin(x / 12.0 * Math.PI) + 300.0 * Math.sin(x / 30.0 * Math.PI)) * 2.0 / 3.0;
    return ret;
}

function gcj02ToWgs84(lng, lat) {
    const PI = Math.PI;
    const A = 6378245.0;
    const EE = 0.00669342162296594323;
    if (lng < 72.004 || lng > 137.8347 || lat < 0.8293 || lat > 55.8271) {
        return [lng, lat];
    }
    let dlat = transformLat(lng - 105.0, lat - 35.0);
    let dlng = transformLng(lng - 105.0, lat - 35.0);
    const radlat = lat / 180.0 * PI;
    let magic = Math.sin(radlat);
    magic = 1 - EE * magic * magic;
    const sqm = Math.sqrt(magic);
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqm) * PI);
    dlng = (dlng * 180.0) / (A / sqm * Math.cos(radlat) * PI);
    return [lng - dlng, lat - dlat];
}

function convertGeoJSONCoords(geojson, converter) {
    if (!geojson || !geojson.features) return geojson;
    const converted = JSON.parse(JSON.stringify(geojson));
    converted.features.forEach(f => {
        if (f.geometry.type === 'Point') {
            f.geometry.coordinates = converter(f.geometry.coordinates[0], f.geometry.coordinates[1]);
        } else if (f.geometry.type === 'LineString') {
            f.geometry.coordinates = f.geometry.coordinates.map(c => converter(c[0], c[1]));
        } else if (f.geometry.type === 'MultiLineString') {
            f.geometry.coordinates = f.geometry.coordinates.map(line =>
                line.map(c => converter(c[0], c[1]))
            );
        }
    });
    return converted;
}

function getCoordConverter() {
    if (currentTileName === 'KCGIS影像') {
        return gcj02ToWgs84;
    }
    return (lng, lat) => [lng, lat];
}

const tileLayers = [
    {
        name: '街道图',
        url: AUTONAVI_RD_URL,
        attribution: '&copy; 高德地图',
        maxZoom: 18,
        crs: 'gcj02',
        subdomains: ['1', '2', '3', '4']
    },
    {
        name: '影像图',
        url: '/proxy/kcgis-img-tile/{z}/{y}/{x}',
        attribution: '&copy; KCGIS 智慧管网',
        maxZoom: 22,
        crs: 'wgs84'
    },
    {
        name: '影像混合',
        url: AUTONAVI_SATELLITE_URL,
        attribution: '&copy; 高德地图',
        maxZoom: 18,
        overlay: AUTONAVI_RD_URL.replace('{s}', '01'),
        crs: 'gcj02',
        subdomains: ['1', '2', '3', '4']
    }
];

function initMap() {
    if (!document.getElementById('map')) {
        return;
    }

    map = L.map('map', {
        maxZoom: 19,
        crs: L.CRS.EPSG3857,
        attributionControl: false
    }).setView([29.75, 118.25], 12);

    // Add default tile layer
    switchTileLayer('街道图');

    deviceLayer = L.layerGroup().addTo(map);
    nodeLayer = L.layerGroup().addTo(map);
    pipeLayer = L.layerGroup().addTo(map);

    loadData();
    setupControls();
    setupTileSwitcher();
}

function switchTileLayer(name) {
    const prevTileName = currentTileName;
    currentTileName = name;

    // Remove current tile layers
    if (currentTileLayer) {
        if (Array.isArray(currentTileLayer)) {
            currentTileLayer.forEach(l => map.removeLayer(l));
        } else {
            map.removeLayer(currentTileLayer);
        }
    }

    const config = tileLayers.find(t => t.name === name);
    if (!config) return;

    const tileOptions = {
        attribution: config.attribution,
        maxZoom: config.maxZoom || 19
    };
    if (config.subdomains) {
        tileOptions.subdomains = config.subdomains;
    }

    if (config.overlay) {
        // Base image + overlay
        const base = L.tileLayer(config.url, tileOptions);
        const overlayOpts = {
            attribution: config.attribution,
            transparent: true,
            maxZoom: config.overlayMaxZoom || 19
        };
        overlayOpts.opacity = 0.7;
        const overlay = L.tileLayer(config.overlay, overlayOpts);
        currentTileLayer = [base, overlay];
        base.addTo(map);
        overlay.addTo(map);
    } else {
        currentTileLayer = L.tileLayer(config.url, tileOptions);
        currentTileLayer.addTo(map);
    }

    // Update active button state
    document.querySelectorAll('.tile-switcher button').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tile === name);
    });

    // Re-render layers to ensure clean state on tile switch
    if (prevTileName !== name) {
        renderAllLayers();
    }
}

function setupTileSwitcher() {
    var TileSwitcher = L.Control.extend({
        onAdd: function(map) {
            var div = L.DomUtil.create('div', 'tile-switcher');
            tileLayers.forEach(t => {
                var btn = L.DomUtil.create('button', '', div);
                btn.textContent = t.name;
                btn.dataset.tile = t.name;
                if (t.name === '街道图') btn.classList.add('active');
                L.DomEvent.on(btn, 'click', function() {
                    switchTileLayer(t.name);
                });
            });
            return div;
        }
    });

    var switcher = new TileSwitcher({ position: 'topright' });
    switcher.addTo(map);
}

const CACHE_KEY = 'mapdata_cache';
const CACHE_TTL = 5 * 60 * 1000; // 5分钟缓存

function loadData() {
    console.log('Loading map data...');

    // 检查缓存
    const cached = localStorage.getItem(CACHE_KEY);
    if (cached) {
        try {
            const { data, timestamp } = JSON.parse(cached);
            if (Date.now() - timestamp < CACHE_TTL) {
                console.log('Using cached map data');
                processData(data);
                return;
            }
        } catch (e) {
            localStorage.removeItem(CACHE_KEY);
        }
    }

    // 请求新数据
    fetch('/api/mapdata')
        .then(r => {
            console.log('Response status:', r.status);
            if (!r.ok) {
                return r.json().then(err => { throw new Error(err.error || `HTTP ${r.status}`); });
            }
            return r.json();
        })
        .then(data => {
            // 保存到缓存
            try {
                localStorage.setItem(CACHE_KEY, JSON.stringify({
                    data,
                    timestamp: Date.now()
                }));
            } catch (e) {
                console.warn('Failed to cache map data:', e);
            }
            processData(data);
        })
        .catch(err => console.error('Error loading data:', err));
}

function processData(data) {
    if (data.error) throw new Error(data.error);
    const devices = data.devices || { type: 'FeatureCollection', features: [] };
    const nodes = data.nodes || { type: 'FeatureCollection', features: [] };
    const pipes = data.pipes || { type: 'FeatureCollection', features: [] };
    console.log('Data loaded:', {
        devices: devices.features.length,
        nodes: nodes.features.length,
        pipes: pipes.features.length
    });
    rawDevicesGeoJSON = devices;
    rawNodesGeoJSON = nodes;
    rawPipesGeoJSON = pipes;
    renderAllLayers();
    updateStats(data);
    console.log('All layers rendered');
}

function renderAllLayers() {
    if (!rawDevicesGeoJSON || !rawNodesGeoJSON || !rawPipesGeoJSON) return;
    const config = tileLayers.find(t => t.name === currentTileName);
    const needWgs84 = config && config.crs === 'wgs84';
    if (needWgs84) {
        const converter = (lng, lat) => gcj02ToWgs84(lng, lat);
        renderPipes(convertGeoJSONCoords(rawPipesGeoJSON, converter));
        renderNodes(convertGeoJSONCoords(rawNodesGeoJSON, converter));
        renderDevices(convertGeoJSONCoords(rawDevicesGeoJSON, converter));
    } else {
        renderPipes(rawPipesGeoJSON);
        renderNodes(rawNodesGeoJSON);
        renderDevices(rawDevicesGeoJSON);
    }
}

function renderPipes(geojson) {
    pipeLayer.clearLayers();
    L.geoJSON(geojson, {
        style: feature => ({
            color: feature.properties.sub_type === 'YS' ? '#2196F3' : '#FF9800',
            weight: 3,
            opacity: 0.7
        }),
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            layer.bindPopup(`
                <b>管线</b><br>
                类型: ${p.sub_type === 'YS' ? '雨水' : '污水'}<br>
                管径: ${p.diameter}<br>
                起点: ${p.start_id}<br>
                终点: ${p.end_id}
            `);
        }
    }).addTo(pipeLayer);
}

function renderNodes(geojson) {
    nodeLayer.clearLayers();
    L.geoJSON(geojson, {
        pointToLayer: (feature, latlng) => {
            return L.circleMarker(latlng, {
                radius: 4,
                fillColor: '#4CAF50',
                color: '#fff',
                weight: 1,
                fillOpacity: 0.8
            });
        },
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            let popup = `<b>管点: ${p.point_id}</b><br>类型: ${p.sub_type}<br>特征: ${p.feature}`;
            if (p.well_bottom_elev) {
                popup += `<br>井底高程: ${p.well_bottom_elev}m`;
            }
            if (p.ground_elev) {
                popup += `<br>地面高程: ${p.ground_elev}m`;
            }
            layer.bindPopup(popup);
        }
    }).addTo(nodeLayer);
}

function renderDevices(geojson) {
    allDevices = geojson.features;
    geojson.features.forEach(f => {
        if (f.properties.device_type) {
            deviceTypes.add(f.properties.device_type);
        }
    });

    const select = document.getElementById('device-type-filter');
    deviceTypes.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t;
        opt.textContent = t;
        select.appendChild(opt);
    });

    renderDeviceLayer(geojson);
}

function getDeviceFilter(status) {
    switch (status) {
        case 'critical': return 'hue-rotate(-40deg) saturate(1.5) brightness(1.1)';
        case 'warning':  return 'hue-rotate(0deg) saturate(1.2)';
        case 'inactive': return 'grayscale(1) opacity(0.5)';
        default:         return 'hue-rotate(0deg)';
    }
}

function getDeviceSVG(size, filter) {
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 800 800" style="filter:${filter};display:block">
<polygon points="400,150 573.2,250 573.2,450 400,550 226.8,450 226.8,250" fill="rgba(245,158,11,0.6)" stroke="#f59e0b" stroke-width="20" stroke-linejoin="round"/>
<circle cx="400" cy="350" r="100" fill="#0D1B2A" stroke="#f59e0b" stroke-width="12"/>
<circle cx="400" cy="350" r="40" fill="#f59e0b" opacity="0.9"/>
<circle cx="400" cy="350" r="12" fill="#FFFFFF"/>
</svg>`;
}

function renderDeviceLayer(geojson) {
    deviceLayer.clearLayers();
    L.geoJSON(geojson, {
        pointToLayer: (feature, latlng) => {
            const p = feature.properties;
            const size = p.status === 'critical' ? 56 : 48;
            const filter = getDeviceFilter(p.status);
            const icon = L.divIcon({
                className: 'device-icon-wrapper',
                html: getDeviceSVG(size, filter),
                iconSize: [size, size],
                iconAnchor: [size / 2, size / 2]
            });
            return L.marker(latlng, { icon });
        },
        onEachFeature: (feature, layer) => {
            const p = feature.properties;
            const isInactive = p.status === 'inactive';

            let popup = `<b>${p.name || p.device_id}</b><br>`;
            popup += `ID: ${p.device_id}<br>`;
            popup += `类型: ${p.device_type || '未知'}<br>`;
            popup += `区域: ${p.area_name || '未知'}<br>`;

            if (p.bound_node_id) {
                popup += `绑定管点: ${p.bound_node_id}<br>`;
                popup += `绑定距离: ${p.bind_distance}m<br>`;
            } else {
                popup += `绑定管点: 无（距离过远或无坐标）<br>`;
            }

            const statusText = getStatusText(p.status);
            popup += `<br><b>状态: ${statusText}</b>`;

            if (p.details && p.details.length > 0) {
                popup += `<br>分析详情:`;
                p.details.forEach(d => {
                    popup += `<br>- ${d}`;
                });
            }

            layer.bindPopup(popup);
            if (!isInactive) {
                layer.on('click', () => showDeviceInfo(p.device_id, p.bound_node_id));
            }
        }
    }).addTo(deviceLayer);
}

function getStatusText(status) {
    switch (status) {
        case 'critical': return '<span style="color:#d32f2f">严重</span>';
        case 'warning': return '<span style="color:#ff9800">警告</span>';
        case 'inactive': return '<span style="color:#9e9e9e">离线/无数据</span>';
        default: return '<span style="color:#4caf50">正常</span>';
    }
}

let currentDeviceId = null;

function showDeviceInfo(deviceId, nodeId) {
    currentDeviceId = deviceId;
    const infoDiv = document.getElementById('device-info');
    infoDiv.innerHTML = '<p>加载中...</p>';

    fetch(`/api/device/${deviceId}/readings?hours=168`)
        .then(r => r.json())
        .then(readings => {
            let html = `
                <div class="device-info-header">
                    <svg width="28" height="28" viewBox="0 0 800 800">
                        <polygon points="400,150 573.2,250 573.2,450 400,550 226.8,450 226.8,250" fill="rgba(245,158,11,0.6)" stroke="#f59e0b" stroke-width="30" stroke-linejoin="round"/>
                        <circle cx="400" cy="350" r="100" fill="#0D1B2A" stroke="#f59e0b" stroke-width="16"/>
                        <circle cx="400" cy="350" r="50" fill="#f59e0b"/>
                    </svg>
                    <div>
                        <div class="device-name">${deviceId}</div>
                        ${nodeId ? `<div class="device-node">绑定管点: ${nodeId}</div>` : ''}
                    </div>
                </div>
            `;
            if (readings.length > 0) {
                const latest = readings[0];
                const rows = [
                    ['最新液位', latest.liquid_level != null ? latest.liquid_level + 'm' : '-'],
                    ['最新COD', latest.cod != null ? latest.cod + 'mg/L' : '-'],
                    ['最新氨氮', latest.ammonia_n != null ? latest.ammonia_n + 'mg/L' : '-'],
                    ['电压', latest.voltage != null ? latest.voltage + 'V' : '-'],
                    ['记录时间', latest.recorded_at || '-'],
                    ['7天记录数', readings.length]
                ];
                rows.forEach(([label, value]) => {
                    html += `<div class="device-info-row"><span class="device-info-label">${label}</span><span class="device-info-value">${value}</span></div>`;
                });

                const levelData = readings.slice(0, 48).reverse();
                if (levelData.length > 0) {
                    html += `<div class="level-chart" style="margin-top:10px;">`;
                    html += `<canvas id="levelChart" width="240" height="120"></canvas>`;
                    html += `</div>`;
                }
            } else {
                html += '<p style="color:var(--text-muted);font-style:italic;">暂无数据</p>';
            }
            infoDiv.innerHTML = html;

            const levelData = readings.slice(0, 48).reverse();
            if (levelData.length > 0) {
                drawLevelChart(levelData);
            }

            fetch(`/api/device/${deviceId}/profile`)
                .then(r => {
                    if (!r.ok) throw new Error(`HTTP ${r.status}`);
                    return r.json();
                })
                .then(profile => {
                    if (profile && !profile.error) {
                        window.currentProfile = profile;
                        showProfileModal();
                    }
                })
                .catch(err => {
                    console.error('Profile fetch failed:', err);
                });
        })
        .catch(err => {
            console.error('Failed to load readings:', err);
            infoDiv.innerHTML = '<p>加载失败，请稍后重试</p>';
        });
}

function showProfileModal() {
    if (!window.currentProfile) return;
    const content = document.getElementById('modal-profile-content');
    const profile = window.currentProfile;
    if (profile && profile.node_details && profile.bound_node) {
        renderProfile(profile, content);
    } else {
        content.innerHTML = `<div class="text-center py-4"><h5>管线纵断面图</h5><p class="text-muted">设备: ${profile.device_id || '-'}</p><p class="text-danger">暂无管线剖面数据</p></div>`;
    }
    // Use Bootstrap modal API
    const modalEl = document.getElementById('profile-modal');
    const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    bsModal.show();
}

function closeModal() {
    const modalEl = document.getElementById('profile-modal');
    const bsModal = bootstrap.Modal.getInstance(modalEl);
    if (bsModal) bsModal.hide();
    if (window.profileChart) {
        window.profileChart.destroy();
        window.profileChart = null;
    }
    ['liquidTrendChart', 'codTrendChart', 'ammoniaTrendChart', 'voltageTrendChart'].forEach(id => {
        const canvas = document.getElementById(id);
        if (canvas) {
            const chart = Chart.getChart(canvas);
            if (chart) chart.destroy();
        }
    });
}

document.addEventListener('DOMContentLoaded', function() {
    // Bootstrap modal handles its own close button and backdrop click
});

function renderProfile(profile, targetDiv) {
    const profileDiv = targetDiv || document.getElementById('modal-profile-content');
    if (!profileDiv) return;
    
    if (!profile || !profile.node_details || !profile.bound_node) {
        profileDiv.innerHTML = '<p class="no-data">无法生成剖面图</p>';
        return;
    }

    const nodeOrder = [...(profile.upstream || []).map(n => n.point_id),
                       profile.bound_node.point_id,
                       ...(profile.downstream || []).map(n => n.point_id)];
    
    const nodes = nodeOrder.map(nid => {
        const detail = profile.node_details[nid] || {};
        return {
            name: nid,
            groundElev: detail.ground_elev || 0,
            elev: detail.well_bottom_elev || 0,
            level: detail.level || 0,
            diameter: detail.diameter || '',
            diameter_mm: detail.diameter_mm || 300,
            fullness: detail.fullness || 0,
            isDevice: detail.has_device || false
        };
    });

    let html = `
        <div class="profile-header">
            <h4>管线纵断面图</h4>
            <p>设备: ${profile.device_id} | 总长: ${profile.total_length ? profile.total_length.toFixed(0) : 0}m</p>
        </div>
    `;

    html += `
        <div class="device-readings-summary" id="deviceReadingsSummary">
            <h4>设备监测数据</h4>
            <div class="readings-grid">
                <div class="reading-item">
                    <span class="reading-label">最新液位</span>
                    <span class="reading-value" id="rt-liquid">加载中...</span>
                </div>
                <div class="reading-item">
                    <span class="reading-label">最新COD</span>
                    <span class="reading-value" id="rt-cod">加载中...</span>
                </div>
                <div class="reading-item">
                    <span class="reading-label">最新氨氮</span>
                    <span class="reading-value" id="rt-ammonia">加载中...</span>
                </div>
                <div class="reading-item">
                    <span class="reading-label">电压</span>
                    <span class="reading-value" id="rt-voltage">加载中...</span>
                </div>
                <div class="reading-item">
                    <span class="reading-label">记录时间</span>
                    <span class="reading-value" id="rt-time">加载中...</span>
                </div>
                <div class="reading-item">
                    <span class="reading-label">7天记录数</span>
                    <span class="reading-value" id="rt-count">加载中...</span>
                </div>
            </div>
        </div>
    `;
    
    if (profile.precipitation) {
        const precip24 = profile.precipitation.total_24h || 0;
        const precipClass = precip24 > 25 ? 'heavy' : precip24 > 10 ? 'moderate' : 'light';
        html += `<div class="precip-info ${precipClass}">
            <span>24h降雨: ${precip24}mm</span>
            <span>7天降雨: ${(profile.precipitation.total_7d || 0)}mm</span>
        </div>`;
    }
    
    html += `<div class="profile-chart"><canvas id="profileChart"></canvas></div>`;
    
    if (profile.pipe_segments && profile.pipe_segments.length > 0) {
        html += `<div class="pipe-segments">`;
        profile.pipe_segments.forEach(seg => {
            const typeLabel = seg.sub_type === 'YS' ? '雨水' : '污水';
            const dirClass = seg.direction.includes('逆坡') ? 'reverse' : '';
            html += `<div class="segment-info ${dirClass}">
                <span class="seg-label">${seg.from} → ${seg.to}</span>
                <span class="seg-detail">${seg.diameter || '未知'} | ${seg.length}m | i=${seg.slope}% | ${seg.direction} | ${typeLabel}</span>
            </div>`;
        });
        html += `</div>`;
    }
    
    html += `<div class="node-details">`;
    nodes.forEach(n => {
        const fullnessClass = n.fullness > 100 ? 'critical' : n.fullness > 80 ? 'high' : n.fullness > 50 ? 'medium' : 'normal';
        const deviceTag = n.isDevice ? '<span class="device-tag">设备</span>' : '';
        html += `<div class="node-item">
            <span class="node-name">${n.name} ${deviceTag}</span>
            <span class="node-info">地面${n.groundElev ? n.groundElev.toFixed(1) : '-'}m | 管底${n.elev ? n.elev.toFixed(1) : '-'}m | 液位${n.level ? n.level.toFixed(2) : '-'}m</span>
            <span class="node-fullness ${fullnessClass}">充满度${n.fullness}%</span>
        </div>`;
    });
    html += `</div>`;

    html += `<div class="trend-charts-section">
        <h4>48小时数据趋势</h4>
        <div class="trend-charts-grid">
            <div class="trend-chart-container">
                <canvas id="liquidTrendChart"></canvas>
            </div>
            <div class="trend-chart-container">
                <canvas id="codTrendChart"></canvas>
            </div>
            <div class="trend-chart-container">
                <canvas id="ammoniaTrendChart"></canvas>
            </div>
            <div class="trend-chart-container">
                <canvas id="voltageTrendChart"></canvas>
            </div>
        </div>
    </div>`;

    profileDiv.innerHTML = html;
    drawProfileChart(nodes, profile);

    if (profile.device_id) {
        loadDeviceRealtimeData(profile.device_id);
    }
}

function loadDeviceRealtimeData(deviceId) {
    fetch(`/api/device/${deviceId}/realtime`)
        .then(r => r.json())
        .then(data => {
            if (data && !data.error) {
                const liquidEl = document.getElementById('rt-liquid');
                const codEl = document.getElementById('rt-cod');
                const ammoniaEl = document.getElementById('rt-ammonia');
                const voltageEl = document.getElementById('rt-voltage');
                const timeEl = document.getElementById('rt-time');

                if (liquidEl) liquidEl.textContent = data.liquid_level != null ? data.liquid_level + 'm' : '-';
                if (codEl) codEl.textContent = data.cod != null ? data.cod + 'mg/L' : '-';
                if (ammoniaEl) ammoniaEl.textContent = data.ammonia_n != null ? data.ammonia_n + 'mg/L' : '-';
                if (voltageEl) voltageEl.textContent = data.voltage != null ? data.voltage + 'V' : '-';
                if (timeEl) timeEl.textContent = data.recorded_at || '-';
            }
        })
        .catch(err => {
            console.log('Failed to load realtime data:', err);
            ['rt-liquid', 'rt-cod', 'rt-ammonia', 'rt-voltage', 'rt-time'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.textContent = '-';
            });
        });

    fetch(`/api/device/${deviceId}/history?hours=48`)
        .then(r => r.json())
        .then(data => {
            if (Array.isArray(data)) {
                const countEl = document.getElementById('rt-count');
                if (countEl) countEl.textContent = data.length;
                if (data.length > 0) {
                    drawTrendCharts(data);
                }
            }
        })
        .catch(err => {
            console.log('Failed to load history data:', err);
        });
}

function drawProfileChart(nodes, profile) {
    setTimeout(() => {
        const canvas = document.getElementById('profileChart');
        if (!canvas) {
            console.log('Canvas not found');
            return;
        }
        if (!window.Chart) {
            console.log('Chart.js not loaded, loading dynamically...');
            const script = document.createElement('script');
            script.src = CHART_JS_CDN;
            script.onload = () => drawProfileChart(nodes, profile);
            document.head.appendChild(script);
            return;
        }
    
        const distances = profile.cumulative_dist || nodes.map((_, i) => i);
        const labels = nodes.map(n => n.name);
    
        const groundData = nodes.map(n => n.groundElev);
        const crownData = nodes.map(n => n.elev + (n.diameter_mm / 1000));
        const elevData = nodes.map(n => n.elev);
        const waterData = nodes.map(n => n.elev + n.level);
    
        const deviceIndex = nodes.findIndex(n => n.isDevice);
        const pointColors = nodes.map((n, i) => {
            if (i === deviceIndex) return '#d32f2f';
            if (n.fullness > 100) return '#d32f2f';
            if (n.fullness > 80) return '#ff9800';
            if (n.fullness > 50) return '#ffc107';
            return '#4CAF50';
        });
        const pointRadius = nodes.map((n, i) => i === deviceIndex ? 8 : 5);
        
        if (window.profileChart && typeof window.profileChart.destroy === 'function') {
            window.profileChart.destroy();
        }
        
        window.profileChart = new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: '地面线',
                        data: groundData,
                        borderColor: '#795548',
                        borderWidth: 1.5,
                        borderDash: [8, 4],
                        pointRadius: 2,
                        pointBackgroundColor: '#795548',
                        fill: false,
                        order: 4
                    },
                    {
                        label: '管顶高程',
                        data: crownData,
                        borderColor: '#607D8B',
                        borderWidth: 1.5,
                        pointRadius: 2,
                        pointBackgroundColor: '#607D8B',
                        fill: false,
                        order: 3
                    },
                    {
                        label: '水面线',
                        data: waterData,
                        borderColor: '#2196F3',
                        backgroundColor: 'rgba(33, 150, 243, 0.3)',
                        fill: '-1',
                        borderWidth: 2,
                        pointBackgroundColor: pointColors,
                        pointRadius: pointRadius,
                        pointHoverRadius: 8,
                        order: 1
                    },
                    {
                        label: '管底高程',
                        data: elevData,
                        borderColor: '#8B4513',
                        backgroundColor: 'transparent',
                        borderWidth: 2,
                        pointRadius: 3,
                        pointBackgroundColor: '#8B4513',
                        fill: false,
                        order: 2
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                legend: {
                    position: 'top',
                    labels: { usePointStyle: true, font: { size: 10 } }
                },
                tooltip: {
                    callbacks: {
                        title: function(items) {
                            const idx = items[0].dataIndex;
                            const dist = distances[idx];
                            return `${nodes[idx].name} (${dist.toFixed(1)}m)`;
                        },
                        afterTitle: function(items) {
                            const idx = items[0].dataIndex;
                            const n = nodes[idx];
                            const deviceTag = n.isDevice ? ' [设备]' : '';
                            return `管径: ${n.diameter || '未知'}${deviceTag}`;
                        },
                        label: function(item) {
                            const idx = item.dataIndex;
                            const n = nodes[idx];
                            const datasets = ['地面高程', '管顶高程', '水面高程', '管底高程'];
                            const vals = [n.groundElev, n.elev + n.diameter_mm/1000, n.elev + n.level, n.elev];
                            return `${datasets[item.datasetIndex]}: ${vals[item.datasetIndex].toFixed(2)}m`;
                        },
                        afterBody: function(items) {
                            const idx = items[0].dataIndex;
                            const n = nodes[idx];
                            return [`液位: ${n.level.toFixed(2)}m`, `充满度: ${n.fullness}%`];
                        }
                    }
                }
            },
            scales: {
                    y: {
                        title: { display: true, text: '高程 (m)', font: { size: 11 } },
                        grid: { color: '#eee' }
                    },
                    x: {
                        title: { display: true, text: '节点', font: { size: 11 } },
                        grid: { display: false }
                    }
                },
                interaction: { intersect: false, mode: 'index' }
            }
        });
    }, 100);
}

function drawLevelChart(data) {
    const canvas = document.getElementById('levelChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');

    const levels = data.map(r => r.liquid_level || 0);
    const times = data.map(r => {
        const t = r.recorded_at || '';
        return t.substring(11, 16);
    });

    const maxLevel = Math.max(...levels, 1);
    const minLevel = 0;
    const range = maxLevel - minLevel;

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    ctx.fillStyle = '#f5f5f5';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.strokeStyle = '#ddd';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {
        const y = 20 + (100 - i * 25);
        ctx.beginPath();
        ctx.moveTo(30, y);
        ctx.lineTo(235, y);
        ctx.stroke();

        ctx.fillStyle = '#999';
        ctx.font = '9px Arial';
        ctx.fillText((minLevel + range * i / 4).toFixed(1), 2, y + 3);
    }

    ctx.beginPath();
    ctx.strokeStyle = '#2196F3';
    ctx.lineWidth = 2;
    levels.forEach((level, i) => {
        const x = 30 + (i / (levels.length - 1)) * 205;
        const y = 20 + 100 - ((level - minLevel) / range) * 100;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.fillStyle = '#333';
    ctx.font = '9px Arial';
    if (times.length > 0) ctx.fillText(times[0], 30, 115);
    if (times.length > 1) ctx.fillText(times[times.length - 1], 190, 115);

    ctx.fillStyle = '#2196F3';
    ctx.font = '10px Arial';
    ctx.fillText('液位(m)', 100, 12);
}

function drawTrendCharts(readings) {
    if (!readings || readings.length === 0) return;
    if (!window.Chart) {
        const script = document.createElement('script');
        script.src = CHART_JS_CDN;
        script.onload = () => drawTrendCharts(readings);
        document.head.appendChild(script);
        return;
    }

    const sortedReadings = [...readings].reverse();

    const labels = sortedReadings.map(r => {
        const t = r.recorded_at || '';
        if (t.includes('/')) {
            return t.substring(5, 10);
        }
        return t.substring(5, 10);
    });

    const safeVal = v => (v != null && v >= 0) ? v : null;

    drawSingleTrendChart('liquidTrendChart', labels,
        sortedReadings.map(r => safeVal(r.liquid_level)), '液位(m)', '#2196F3');

    drawSingleTrendChart('codTrendChart', labels,
        sortedReadings.map(r => safeVal(r.cod)), 'COD(mg/L)', '#FF9800');

    drawSingleTrendChart('ammoniaTrendChart', labels,
        sortedReadings.map(r => safeVal(r.ammonia_n)), '氨氮(mg/L)', '#4CAF50');

    drawSingleTrendChart('voltageTrendChart', labels,
        sortedReadings.map(r => safeVal(r.voltage)), '电压(V)', '#9C27B0');
}

function drawSingleTrendChart(canvasId, labels, data, label, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const chartInstance = Chart.getChart(canvas);
    if (chartInstance) chartInstance.destroy();

    new Chart(canvas, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: label,
                data: data,
                borderColor: color,
                backgroundColor: color + '20',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: data.length > 50 ? 0 : 2,
                pointHoverRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    mode: 'index',
                    intersect: false
                }
            },
            scales: {
                x: {
                    grid: { display: false },
                    ticks: {
                        maxTicksLimit: 7,
                        font: { size: 10 }
                    }
                },
                y: {
                    grid: { color: '#eee' },
                    ticks: { font: { size: 10 } }
                }
            },
            interaction: {
                mode: 'nearest',
                axis: 'x',
                intersect: false
            }
        }
    });
}

function updateStats(data) {
    const statsDiv = document.getElementById('stats');
    const devCount = data.devices.features.length;
    const nodeCount = data.nodes.features.length;
    const pipeCount = data.pipes.features.length;
    const boundCount = data.devices.features.filter(f => f.properties.bound_node_id).length;

    const criticalCount = data.devices.features.filter(f => f.properties.status === 'critical').length;
    const warningCount = data.devices.features.filter(f => f.properties.status === 'warning').length;
    const inactiveCount = data.devices.features.filter(f => f.properties.status === 'inactive').length;
    const normalCount = devCount - criticalCount - warningCount - inactiveCount;

    statsDiv.innerHTML = `
        设备: ${devCount}<br>
        管点: ${nodeCount}<br>
        管线: ${pipeCount}<br>
        已绑定设备: ${boundCount}<br>
        <hr>
        <b>状态统计:</b><br>
        <span style="color:#d32f2f">严重: ${criticalCount}</span><br>
        <span style="color:#ff9800">警告: ${warningCount}</span><br>
        <span style="color:#4caf50">正常: ${normalCount}</span><br>
        <span style="color:#9e9e9e">离线: ${inactiveCount}</span>
    `;
}

function setupControls() {
    document.getElementById('toggle-devices').addEventListener('change', function() {
        if (this.checked) deviceLayer.addTo(map);
        else map.removeLayer(deviceLayer);
    });

    document.getElementById('toggle-nodes').addEventListener('change', function() {
        if (this.checked) nodeLayer.addTo(map);
        else map.removeLayer(nodeLayer);
    });

    document.getElementById('toggle-pipes').addEventListener('change', function() {
        if (this.checked) pipeLayer.addTo(map);
        else map.removeLayer(pipeLayer);
    });

    document.getElementById('device-type-filter').addEventListener('change', function() {
        const selected = this.value;
        if (selected === 'all') {
            renderDeviceLayer({ type: 'FeatureCollection', features: allDevices });
        } else {
            const filtered = allDevices.filter(f => f.properties.device_type === selected);
            renderDeviceLayer({ type: 'FeatureCollection', features: filtered });
        }
    });
}

document.addEventListener('DOMContentLoaded', initMap);
