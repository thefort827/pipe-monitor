import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import math, re
import warnings; warnings.filterwarnings('ignore')

fp = r'G:\设备数据分析\城西闪传_修复版_更新后.xlsx'
df_p = pd.read_excel(fp, sheet_name=0, header=0, skiprows=[1,2])
df_l = pd.read_excel(fp, sheet_name=1, header=0, skiprows=[1,2])

print('YSK29 连接正确性分析:')
print('='*60)
ysk29_pts = df_p[df_p['图上点号'].astype(str) == 'YSK29']
p1 = ysk29_pts.iloc[0]
p2 = ysk29_pts.iloc[1]

print(f'YSK29 #1: x={p1["x坐标"]}, y={p1["y坐标"]}')
print(f'YSK29 #2: x={p2["x坐标"]}, y={p2["y坐标"]}')

ysk29_lines = df_l[(df_l['起点号'].astype(str) == 'YSK29') | (df_l['终点号'].astype(str) == 'YSK29')]
for _, r in ysk29_lines.iterrows():
    other = r['终点号'] if str(r['起点号']) == 'YSK29' else r['起点号']
    other_pt = df_p[df_p['图上点号'].astype(str) == str(other)]
    if len(other_pt) > 0:
        o = other_pt.iloc[0]
        d1 = math.sqrt((float(p1['x坐标'])-float(o['x坐标']))**2 + (float(p1['y坐标'])-float(o['y坐标']))**2)
        d2 = math.sqrt((float(p2['x坐标'])-float(o['x坐标']))**2 + (float(p2['y坐标'])-float(o['y坐标']))**2)
        closest = 1 if d1 < d2 else 2
        print(f'  管线 YSK29 -> {other}: 最近的YSK29是 #{closest}, 距离={min(d1,d2):.2f}m, 较远={max(d1,d2):.2f}m')

print('\n全部重复YSK点号模式分析')
print('='*60)
ysk_counts = df_p[df_p['图上点号'].astype(str).str.match(r'^YSK\d+$')]['图上点号'].value_counts()
dup_ysk = ysk_counts[ysk_counts > 1]
print(f'重复的YSK点号数: {len(dup_ysk)}')
print(f'YSK点号总唯一数: {len(ysk_counts)}')
print(f'所有YSK点号均重复: {len(dup_ysk) == len(ysk_counts)}')

ysk_with_suffix = df_p[df_p['图上点号'].astype(str).str.match(r'^YSK\d+-')]
print(f'\n带后缀的YSK点: {len(ysk_with_suffix)} 个')
if len(ysk_with_suffix) > 0:
    for _, r in ysk_with_suffix.iterrows():
        print(f'  {r["图上点号"]}: x={r["x坐标"]}, y={r["y坐标"]}')

ysk_nums = []
for pt in df_p['图上点号'].dropna().astype(str):
    m = re.match(r'^YSK(\d+)$', pt)
    if m:
        ysk_nums.append(int(m.group(1)))
print(f'\nYSK编号范围: YSK{min(ysk_nums)} 到 YSK{max(ysk_nums)}')
print(f'预期点数: {max(ysk_nums) - min(ysk_nums) + 1}')
print(f'实际唯一点号数: {len(set(ysk_nums))}')
missing = set(range(min(ysk_nums), max(ysk_nums)+1)) - set(ysk_nums)
if missing:
    print(f'缺失的YSK编号: {sorted(missing)}')
else:
    print(f'编号连续，无缺失')
