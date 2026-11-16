"""运行仓内聚合：从 data/sample.json 建摘要，再只用摘要做重叠查询。

演示两条路径：
1. 小集合：分片摘要保留全部哈希，交集/差集/并集给出精确规模。
2. 大集合：合成人群，摘要只保留精度允许的最小哈希，给出估计与区间。
"""

import json
from pathlib import Path

from .budget import Budget
from .expression import difference, intersection, union
from .summary import Summary
from .warehouse import Warehouse


def build_warehouse():
    body = json.loads(Path('data/sample.json').read_text())
    seed = body['seed']
    budget = Budget(max_values=1 << 16)
    warehouse = Warehouse(seed, precision=32, budget=budget)
    receipts = {}
    for name, values in body['sets'].items():
        fragments = ['part-1', 'part-2']
        warehouse.register(name, '%s-snapshot' % name, fragments)
        middle = max(1, len(values) // 2)
        for fragment_id, part in zip(fragments, (values[:middle], values[middle:])):
            state = Summary.from_identities(part, seed, 32).export()
            warehouse.accept(name, fragment_id, state)
        receipts[name] = warehouse.receipt(name)
        warehouse.finalize(name)
    return warehouse, receipts


def summarize(result):
    estimate = result.estimate
    return {
        'estimate': estimate['estimate'],
        'exact': estimate['exact'],
        'interval': [estimate['interval']['lower'], estimate['interval']['upper']],
        'confidence': estimate['interval']['confidence'],
        'method': estimate['interval']['method'],
        'threshold_kind': estimate['sampling']['threshold_kind'],
    }


def main():
    warehouse, receipts = build_warehouse()
    show = warehouse.leaf('show')
    stay = warehouse.leaf('stay')
    culture = warehouse.leaf('culture')

    queries = {
        'show∩stay': intersection(show, stay),
        'show-only': difference(show, stay),
        'show∪stay∪culture': union(show, stay, culture),
        'show∩stay∩culture': intersection(show, stay, culture),
    }
    report = {
        'snapshot_receipts': receipts,
        'queries': {
            name: {
                'result': summarize(warehouse.query(expr)),
                'evaluation': warehouse.query(expr).export()['evaluation'],
            }
            for name, expr in queries.items()
        },
        'applicability': warehouse.applicability(),
    }

    # 合成大集合，展示有界摘要与可复现的区间；这些身份不来自入仓数据。
    # 两个来源共用同一身份池，因此真实交集为 60000 人、并集 180000 人。
    seed = warehouse.seed
    big = Warehouse(seed, precision=128)
    for name, start, stop in (('large-show', 0, 120000), ('large-stay', 60000, 180000)):
        big.register(name, '%s-snapshot' % name, ['only'])
        identities = ('synthetic-person-%d' % index for index in range(start, stop))
        big.accept(name, 'only', Summary.from_identities(identities, seed, 128).export())
        big.finalize(name)
    big_show = big.leaf('large-show')
    big_stay = big.leaf('large-stay')
    report['synthetic_large'] = {
        'note': '合成演示数据，不是入仓来源；真实交集为 60000 人、并集 180000 人',
        'large-show (真实 120000)': summarize(big.query(big_show)),
        'large-show∩large-stay (真实 60000)': summarize(
            big.query(intersection(big_show, big_stay))),
        'large-show∪large-stay (真实 180000)': summarize(
            big.query(union(big_show, big_stay))),
        'large-show−large-stay (真实 60000)': summarize(
            big.query(difference(big_show, big_stay))),
    }

    serialized = json.dumps(warehouse.export(), ensure_ascii=False, sort_keys=True)
    report['serialized_state_bytes'] = len(serialized.encode())
    report['serialized_state_contains_identities'] = 'synthetic-' in serialized
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
