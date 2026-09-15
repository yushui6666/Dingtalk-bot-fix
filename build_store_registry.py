#!/usr/bin/env python3
"""自动生成「工单店名 → 备件店」注册表草稿，供人工审阅。

决定映射的两条规则（保守）：
- 只有明确对应关系才自动映射；
- 歧义（一个店名可对应多个备件店，如「上海商场08店」）与无对应表者留 None，交人工定夺。

输入：data/groups.json 的 store_name + tickets 表 DISTINCT store_name（只读）。
输出：data/stores/_store_registry.json
"""
import json, os, re, sqlite3, collections

OUT = 'data/stores/_store_registry.json'

# 备件店 canonical key（去数字前缀）→ 文件名
spare = {}
for f in os.listdir('data/stores'):
    if not f.endswith('.json') or f.startswith('_'): continue
    d = json.load(open(os.path.join('data/stores', f), encoding='utf-8'))
    s = d['store']
    s2 = re.sub(r'^\d+(\.\d+)*[.．]?\s*', '', s)
    spare[s2] = f

# 需要映射的工单侧店名（群 + 历史票）
names = collections.OrderedDict()
g = json.load(open('data/groups.json', encoding='utf-8'))
for grp in g['groups']:
    if grp.get('store_name'): names[grp['store_name']] = None
con = sqlite3.connect('file:data/tickets.db?mode=ro', uri=True)
for r in con.execute('SELECT DISTINCT store_name FROM tickets'):
    if r[0]: names[r[0]] = None
con.close()

# 显式手工映射表（覆盖自动规则）——此处为草稿判断，交用户审阅
EXPLICIT = {
    '测试群（示例）': None,
    '测试群': None,
    '杭州商场24龙湖店': '杭州商场24龙湖店',
    '杭州商场24印象城店': '杭州商场24印象城店',
    '杭州商场23龙湖店': '杭州商场23',
    '杭州大悦城品牌A': None,
    '杭州商场22龙湖店': None,
    '北京商场14大悦城品牌B店': '商场14大悦城',
    '北京商场17龙湖店': '北京商场17天街店',
    '常州商场19吾悦店': '常州店',
    '北京商场15店': '北京商场15店',
    '北京商场16龙湖店': '北京商场16',
    '上海品牌A商场01店': None,
    '上海商场02万达品牌A店': '上海商场02店',
    '上海商场11大悦城店': '上海商场11大悦城',
    '上海商场09品牌A品牌B': '上海商场09',
    '上海品牌B商场05店': '上海商场05',
    '上海商场10来福士店': '上海商场10来福士',
    '上海品牌C商场03店': '上海商场03品牌C',
    '上海品牌C商场13店': '上海商场13',
    '上海商场08店': ['上海商场08品牌C', '上海商场08品牌B'],   # 一家店含两个品牌，备件池=两表并集
    '上海商场02店': '上海商场02店',
    '上海商场07印象城店': '上海商场07印象城店',
    '上海商场03店': ['上海商场03品牌C', '上海商场03品牌B'],   # 同商场08：一店两品牌，并集
    '杭州商场20店': '杭州商场20店',
    '北京商场18龙湖店': '北京长盈店',
    '杭州商场21CC品牌B品牌A店': None,
    '上海商场04店': '上海商场04店',
    '上海商场06品牌D店': '上海商场06tpy',
    '苏州商场25龙湖店': '苏州商场25龙湖店',
    '上海商场12大悦城店': '上海商场12大悦城',
}

registry = {'_comment': '工单店名 -> 备件店（data/stores/<file> 去前缀名）。None=无对应表/待人工定夺。', 'stores': {}}
review = {'_comment': '审阅提示', 'needs_decision': [], 'unmapped': []}
for n in names:
    target = EXPLICIT.get(n)
    if target is None:
        if n not in ('测试群（示例）', '测试群'):
            review['unmapped'].append(n)
        registry['stores'][n] = None
    else:
        targets = target if isinstance(target, list) else [target]
        files = []
        bad = []
        for t in targets:
            if t in spare:
                files.append(spare[t])
            else:
                bad.append(t)
        if bad:
            review['needs_decision'].append(
                f'{n} -> {"/".join(bad)}（备件店不存在!）')
        if files:
            # 单文件存字符串；多文件（并集，如商场08店）存列表
            registry['stores'][n] = files[0] if len(files) == 1 else files
        else:
            registry['stores'][n] = None
            if n not in ('测试群（示例）', '测试群'):
                review['unmapped'].append(n)

# 用户决策备注（2026-09-09）：歧义店（商场08/商场03一店两品牌）按两表并集处理；
# 无备件表门店保持 None（先不记录），待有表后再启用。
review['pending_decision'] = []
review['no_store_table'] = [
    '杭州大悦城品牌A', '杭州商场22龙湖店', '上海品牌A商场01店',
    '杭州商场21CC品牌B品牌A店',  # 暂无备件表 → 先不记录
]

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, 'w', encoding='utf-8') as fh:
    json.dump({'registry': registry, '_review': review}, fh, ensure_ascii=False, indent=2)

missing = [k for k, v in registry['stores'].items() if v is None and k not in ('测试群（示例）', '测试群')]
print('已生成', OUT)
print('映射成功:', sum(1 for v in registry['stores'].values() if v))
print('未映射(无表/歧义):', missing)
