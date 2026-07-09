# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import warnings
warnings.filterwarnings('ignore')

fp = r'G:\设备数据分析\城西闪传_修复版_更新后.xlsx'

# Read with correct header (row 1 is header, rows 2,3 are sub/metadata)
df_p = pd.read_excel(fp, sheet_name=0, header=0, skiprows=[1,2])
df_l = pd.read_excel(fp, sheet_name=1, header=0, skiprows=[1,2])

# Rename for convenience
# Points: 图上点号, 管网类型, 管点子类型, x坐标, y坐标
# Lines: 起点号, 终点号, 管线类型, 管线子类型

out = []
out.append('='*70)
out.append('  城西闪传_修复版_更新后.xlsx 数据质量分析报告')
out.append('='*70)

# === 1. Basic stats ===
out.append('\n' + '='*70)
out.append('第一部分：基本数据概况')
out.append('='*70)
out.append(f'管点表行数: {len(df_p)}')
out.append(f'管线表行数: {len(df_l)}')
out.append(f'管点表 图上点号 非空数: {df_p["图上点号"].notna().sum()}')
out.append(f'管线表 起点号 非空数: {df_l["起点号"].notna().sum()}')
out.append(f'管线表 终点号 非空数: {df_l["终点号"].notna().sum()}')

# Show sample
out.append('\n管点表 前3行 关键字段:')
for _, r in df_p[['图上点号','管网类型','管点子类型','x坐标','y坐标']].head(3).iterrows():
    out.append(f'  {dict(r)}')

out.append('\n管线表 前3行 关键字段:')
for _, r in df_l[['起点号','终点号','管线类型','管线子类型']].head(3).iterrows():
    out.append(f'  {dict(r)}')

# === 2. 重复点号检查 ===
out.append('\n' + '='*70)
out.append('第二部分：重复点号检查')
out.append('='*70)

# Clean data
df_p_clean = df_p[df_p['图上点号'].notna()].copy()
dup_mask = df_p_clean.duplicated(subset=['图上点号'], keep=False)
df_dup = df_p_clean[dup_mask].sort_values('图上点号')

out.append(f'有效管点数（图上点号非空）: {len(df_p_clean)}')
out.append(f'重复图上点号数量: {df_dup["图上点号"].nunique()} 个不同的点号')
out.append(f'涉及重复的行数: {len(df_dup)} 行')

if len(df_dup) > 0:
    out.append('\n--- 重复点号详细列表 ---')
    for pt_id, grp in df_dup.groupby('图上点号'):
        out.append(f'\n  点号: {pt_id} (出现 {len(grp)} 次)')
        for i, (_, r) in enumerate(grp.iterrows()):
            out.append(f'    [{i+1}] 管网类型={r.get("管网类型","N/A")}, 管点子类型={r.get("管点子类型","N/A")}, '
                       f'x={r.get("x坐标","N/A")}, y={r.get("y坐标","N/A")}, '
                       f'特征={r.get("特征","N/A")}, 附属物={r.get("附属物","N/A")}')
        # Check if attributes are same
        cols_to_check = ['管网类型','管点子类型','x坐标','y坐标','特征','附属物','地面高程']
        same = True
        for col in cols_to_check:
            if col in grp.columns:
                vals = grp[col].unique()
                # Treat NaN as equal
                non_nan = [v for v in vals if pd.notna(v)]
                if len(set(str(v) for v in non_nan)) > 1:
                    same = False
                    break
        if same:
            out.append(f'    -> 属性一致（完全重复）')
        else:
            out.append(f'    -> 属性不一致（存在冲突！）')

# === 3. 连接问题检查 ===
out.append('\n' + '='*70)
out.append('第三部分：连接问题检查')
out.append('='*70)

# Get all valid point IDs
point_ids = set(df_p_clean['图上点号'].astype(str).str.strip())
out.append(f'管点表中唯一图上点号数: {len(point_ids)}')

# Check start/end points
df_l_clean = df_l[df_l['起点号'].notna() & df_l['终点号'].notna()].copy()
out.append(f'管线表中起点号和终点号均非空的行数: {len(df_l_clean)}')

df_l_clean['起点号_str'] = df_l_clean['起点号'].astype(str).str.strip()
df_l_clean['终点号_str'] = df_l_clean['终点号'].astype(str).str.strip()

all_line_points = set(df_l_clean['起点号_str']) | set(df_l_clean['终点号_str'])
out.append(f'管线中引用的唯一点号数: {len(all_line_points)}')

missing_start = set()
missing_end = set()
broken_lines = []

for _, r in df_l_clean.iterrows():
    sp = r['起点号_str']
    ep = r['终点号_str']
    if sp not in point_ids:
        missing_start.add(sp)
        broken_lines.append((r.get('起点号',''), r.get('终点号',''), '起点不存在'))
    if ep not in point_ids:
        missing_end.add(ep)
        broken_lines.append((r.get('起点号',''), r.get('终点号',''), '终点不存在'))

out.append(f'\n--- 断裂管线检查 ---')
out.append(f'起点号在管点表中不存在的个数: {len(missing_start)}')
out.append(f'终点号在管点表中不存在的个数: {len(missing_end)}')
out.append(f'断裂管线总条数: {len(broken_lines)}')

if missing_start:
    out.append(f'\n缺失的起点号列表 (前30个):')
    for s in sorted(missing_start)[:30]:
        # count occurrences
        cnt = len(df_l_clean[df_l_clean['起点号_str'] == s])
        out.append(f'  {s} (被 {cnt} 条管线引用为起点)')

if missing_end:
    out.append(f'\n缺失的终点号列表 (前30个):')
    for s in sorted(missing_end)[:30]:
        cnt = len(df_l_clean[df_l_clean['终点号_str'] == s])
        out.append(f'  {s} (被 {cnt} 条管线引用为终点)')

if broken_lines:
    out.append(f'\n断裂管线详情 (前30条):')
    for sp, ep, reason in broken_lines[:30]:
        out.append(f'  起点={sp}, 终点={ep} -> {reason}')

# Orphan points
referenced_points = all_line_points
orphan_points = point_ids - referenced_points
out.append(f'\n--- 孤立管点检查 ---')
out.append(f'孤立管点数量（管点表中有但管线表未引用）: {len(orphan_points)}')
if orphan_points:
    out.append(f'孤立管点示例 (前30个):')
    for op in sorted(orphan_points)[:30]:
        row = df_p_clean[df_p_clean['图上点号'].astype(str).str.strip() == op]
        if len(row) > 0:
            r = row.iloc[0]
            out.append(f'  {op} (管网类型={r.get("管网类型","N/A")}, 管点子类型={r.get("管点子类型","N/A")})')

# === 4. YSK29 专题 ===
out.append('\n' + '='*70)
out.append('第四部分：YSK29 专题检查')
out.append('='*70)

# Points containing YSK29
ysk29_pts = df_p_clean[df_p_clean['图上点号'].astype(str).str.contains('YSK29', na=False)]
out.append(f'管点表中包含 "YSK29" 的点号数: {len(ysk29_pts)}')
if len(ysk29_pts) > 0:
    out.append('详细列表:')
    for _, r in ysk29_pts.iterrows():
        out.append(f'  {r["图上点号"]} (管网类型={r.get("管网类型","N/A")}, 管点子类型={r.get("管点子类型","N/A")}, '
                   f'x={r.get("x坐标","N/A")}, y={r.get("y坐标","N/A")})')

# Lines containing YSK29
ysk29_lines = df_l_clean[
    df_l_clean['起点号'].astype(str).str.contains('YSK29', na=False) |
    df_l_clean['终点号'].astype(str).str.contains('YSK29', na=False)
]
out.append(f'\n管线表中包含 "YSK29" 的管线数: {len(ysk29_lines)}')
if len(ysk29_lines) > 0:
    out.append('详细列表:')
    for _, r in ysk29_lines.iterrows():
        out.append(f'  起点={r["起点号"]}, 终点={r["终点号"]} (管线类型={r.get("管线类型","N/A")}, '
                   f'管线子类型={r.get("管线子类型","N/A")}, 管径={r.get("管径/断面尺寸","N/A")})')

# === 5. Topology - Connection count distribution ===
out.append('\n' + '='*70)
out.append('第五部分：拓扑合理性检查')
out.append('='*70)

# Count connections per point
from collections import Counter
conn_counter = Counter()
for _, r in df_l_clean.iterrows():
    conn_counter[r['起点号_str']] += 1
    conn_counter[r['终点号_str']] += 1

conn_dist = Counter(conn_counter.values())
out.append('管点连接数分布:')
for k in sorted(conn_dist.keys()):
    out.append(f'  连接 {k} 条管线的点: {conn_dist[k]} 个')

# Points with high connection counts (potential anomalies)
high_conn = {k: v for k, v in conn_counter.items() if v >= 4}
if high_conn:
    out.append(f'\n连接数 >= 4 的异常高连接管点 ({len(high_conn)} 个):')
    for pt, cnt in sorted(high_conn.items(), key=lambda x: -x[1])[:30]:
        row = df_p_clean[df_p_clean['图上点号'].astype(str).str.strip() == pt]
        extra = ''
        if len(row) > 0:
            r = row.iloc[0]
            extra = f' (管网类型={r.get("管网类型","N/A")}, 管点子类型={r.get("管点子类型","N/A")}, 附属物={r.get("附属物","N/A")})'
        out.append(f'  {pt}: {cnt} 条管线{extra}')

# Points that are start-only or end-only (dead ends)
start_only = set(df_l_clean['起点号_str']) - set(df_l_clean['终点号_str'])
end_only = set(df_l_clean['终点号_str']) - set(df_l_clean['起点号_str'])
out.append(f'\n仅作为起点出现的管点数: {len(start_only)}')
out.append(f'仅作为终点出现的管点数: {len(end_only)}')

# Check for isolated subgraphs (simple check: count connected components)
out.append(f'\n--- 连通性分析 ---')
# Build adjacency via union-find
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

for _, r in df_l_clean.iterrows():
    union(r['起点号_str'], r['终点号_str'])

components = {}
for pt in all_line_points:
    root = find(pt)
    if root not in components:
        components[root] = []
    components[root].append(pt)

comp_sizes = sorted([len(v) for v in components.values()], reverse=True)
out.append(f'管线网络连通分量数: {len(comp_sizes)}')
out.append(f'最大连通分量大小: {comp_sizes[0]} 个点' if comp_sizes else 'N/A')
if len(comp_sizes) > 1:
    out.append(f'连通分量大小分布 (前10): {comp_sizes[:10]}')
    small_comps = [c for c in components.values() if len(c) <= 3]
    if small_comps:
        out.append(f'小型连通分量 (<=3个点, 可能是孤立碎片): {len(small_comps)} 个')
        for comp in small_comps[:5]:
            out.append(f'  分量: {comp}')

# === 6. Additional checks ===
out.append('\n' + '='*70)
out.append('第六部分：其他数据质量问题')
out.append('='*70)

# Check for lines where start == end (self-loop)
self_loops = df_l_clean[df_l_clean['起点号_str'] == df_l_clean['终点号_str']]
out.append(f'自环管线（起点=终点）: {len(self_loops)} 条')
if len(self_loops) > 0:
    for _, r in self_loops.head(10).iterrows():
        out.append(f'  起点=终点={r["起点号"]}')

# Check for duplicate lines (same start+end)
df_l_clean['line_key'] = df_l_clean['起点号_str'] + '|' + df_l_clean['终点号_str']
dup_lines = df_l_clean[df_l_clean.duplicated(subset=['line_key'], keep=False)]
out.append(f'重复管线（相同起点+终点）: {dup_lines["line_key"].nunique()} 对, 涉及 {len(dup_lines)} 行')
if len(dup_lines) > 0:
    for key, grp in list(dup_lines.groupby('line_key'))[:10]:
        out.append(f'  {key.replace("|"," -> ")} ({len(grp)} 条)')

# Network type distribution
out.append(f'\n管点表 管网类型 分布:')
for k, v in df_p_clean['管网类型'].value_counts().items():
    out.append(f'  {k}: {v}')

out.append(f'\n管线表 管线类型 分布:')
for k, v in df_l['管线类型'].value_counts().items():
    out.append(f'  {k}: {v}')

# Points with missing coordinates
no_coord = df_p_clean[df_p_clean['x坐标'].isna() | df_p_clean['y坐标'].isna()]
out.append(f'\n缺少坐标的管点: {len(no_coord)} 个')

# Write report
report = '\n'.join(out)
with open(r'G:\设备数据分析\data_quality_report.txt', 'w', encoding='utf-8') as f:
    f.write(report)
print(report)
