# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import warnings
warnings.filterwarnings('ignore')
from collections import Counter, defaultdict
import math

fp = r'G:\设备数据分析\城西闪传_修复版_更新后.xlsx'
df_p = pd.read_excel(fp, sheet_name=0, header=0, skiprows=[1,2])
df_l = pd.read_excel(fp, sheet_name=1, header=0, skiprows=[1,2])

# 1. YSK29 detailed analysis
print('='*60)
print('YSK29 深入分析')
print('='*60)

ysk29_pts = df_p[df_p['图上点号'].astype(str) == 'YSK29']
print(f'\n管点表中 YSK29 共 {len(ysk29_pts)} 条记录:')
for i, (_, r) in enumerate(ysk29_pts.iterrows()):
    vals = [f'x={r["x坐标"]}', f'y={r["y坐标"]}', f'地面高程={r.get("地面高程","N/A")}', f'附属物={r.get("附属物","N/A")}', f'特征={r.get("特征","N/A")}']
    print(f'  [{i+1}] ' + ', '.join(vals))

ysk29_lines = df_l[(df_l['起点号'].astype(str) == 'YSK29') | (df_l['终点号'].astype(str) == 'YSK29')]
print(f'\n管线表中连接 YSK29 的管线 {len(ysk29_lines)} 条:')
for _, r in ysk29_lines.iterrows():
    print(f'  起点={r["起点号"]}, 终点={r["终点号"]}, 管径={r.get("管径/断面尺寸","N/A")}, 材料={r.get("管线材料","N/A")}')

print(f'\nYSK29 相关管线连接的另一端点:')
for _, r in ysk29_lines.iterrows():
    other = r['终点号'] if str(r['起点号']) == 'YSK29' else r['起点号']
    pts = df_p[df_p['图上点号'].astype(str) == str(other)]
    if len(pts) > 0:
        p = pts.iloc[0]
        print(f'  {other}: x={p["x坐标"]}, y={p["y坐标"]}, 附属物={p.get("附属物","N/A")}')
    else:
        print(f'  {other}: 在管点表中不存在!')

# 2. Connectivity
print('\n' + '='*60)
print('连通分量详细分析')
print('='*60)
df_l_clean = df_l[df_l['起点号'].notna() & df_l['终点号'].notna()].copy()
df_l_clean['sn'] = df_l_clean['起点号'].astype(str).str.strip()
df_l_clean['en'] = df_l_clean['终点号'].astype(str).str.strip()

parent = {}
def find(x):
    while parent.get(x, x) != x:
        parent[x] = parent.get(parent[x], parent[x])
        x = parent[x]
    return x
def union(a, b):
    a, b = find(a), find(b)
    if a != b:
        parent[a] = b

all_pts = set()
for _, r in df_l_clean.iterrows():
    union(r['sn'], r['en'])
    all_pts.add(r['sn'])
    all_pts.add(r['en'])

components = defaultdict(list)
for pt in all_pts:
    components[find(pt)].append(pt)

comp_list = sorted(components.values(), key=len, reverse=True)
print(f'连通分量总数: {len(comp_list)}')
for i, comp in enumerate(comp_list[:10]):
    sample = sorted(comp)[:5]
    print(f'  分量{i+1}: {len(comp)} 个点, 示例: {sample}...')

print('\n小型连通分量详情:')
for i, comp in enumerate(comp_list):
    if len(comp) <= 3:
        print(f'\n  分量 {i+1}: {comp}')
        for pt in comp:
            lines = df_l_clean[(df_l_clean['sn'] == pt) | (df_l_clean['en'] == pt)]
            for _, r in lines.iterrows():
                print(f'    管线: {r["sn"]} -> {r["en"]}, 管径={r.get("管径/断面尺寸","N/A")}')

# 3. Duplicate line
print('\n' + '='*60)
print('重复管线详情: WCB-W191-1 -> WCB-W191')
print('='*60)
dup = df_l_clean[(df_l_clean['sn'] == 'WCB-W191-1') & (df_l_clean['en'] == 'WCB-W191')]
if len(dup) == 0:
    dup = df_l_clean[(df_l_clean['sn'] == 'WCB-W191') & (df_l_clean['en'] == 'WCB-W191-1')]
for _, r in dup.iterrows():
    print(f'  起点={r["起点号"]}, 终点={r["终点号"]}, 管径={r.get("管径/断面尺寸","N/A")}, 材料={r.get("管线材料","N/A")}, 起点埋深={r.get("起点埋深","N/A")}, 终点埋深={r.get("终点埋深","N/A")}')

# 4. XFS-W10
print('\n' + '='*60)
print('XFS-W10 重复详情')
print('='*60)
xfs10 = df_p[df_p['图上点号'].astype(str) == 'XFS-W10']
for i, (_, r) in enumerate(xfs10.iterrows()):
    print(f'  [{i+1}] 附属物={r.get("附属物","N/A")}, 特征={r.get("特征","N/A")}, 井深={r.get("井深","N/A")}, x={r["x坐标"]}, y={r["y坐标"]}')

# 5. YSK29 distance
print('\n' + '='*60)
print('YSK29 两点距离分析')
print('='*60)
ysk29_list = list(ysk29_pts.iterrows())
if len(ysk29_list) >= 2:
    r1 = ysk29_list[0][1]
    r2 = ysk29_list[1][1]
    dx = float(r1['x坐标']) - float(r2['x坐标'])
    dy = float(r1['y坐标']) - float(r2['y坐标'])
    dist = math.sqrt(dx*dx + dy*dy)
    print(f'  YSK29 #1: x={r1["x坐标"]}, y={r1["y坐标"]}')
    print(f'  YSK29 #2: x={r2["x坐标"]}, y={r2["y坐标"]}')
    print(f'  两点间距离: {dist:.2f} 米')

# 6. All YSK dup distances
print('\n' + '='*60)
print('所有重复YSK点间距分析')
print('='*60)
ysk_counts = df_p[df_p['图上点号'].astype(str).str.startswith('YSK')]['图上点号'].value_counts()
ysk_dup_ids = [k for k in ysk_counts.items() if k[1] > 1]
for pt_id, cnt in sorted(ysk_dup_ids):
    pts = df_p[df_p['图上点号'].astype(str) == str(pt_id)]
    if len(pts) >= 2:
        r1 = pts.iloc[0]
        r2 = pts.iloc[1]
        dx = float(r1['x坐标']) - float(r2['x坐标'])
        dy = float(r1['y坐标']) - float(r2['y坐标'])
        dist = math.sqrt(dx*dx + dy*dy)
        print(f'  {pt_id}: 距离={dist:.2f}m')
