"""可复用的仓内聚合状态：来源登记、片段接收、摘要建叶与表达式查询。

仓库只持有摘要状态和来源证明，不读取原始身份。来源由冻结快照与预先登记的片段
清单定义；片段重复发送且内容相同时幂等，内容不同则记为冲突，两者都不会让来源
提前完成或重复计数。完成后的来源可以导出为可序列化状态，在另一个进程恢复后继续
组合，含义不变。
"""

from __future__ import annotations

from .budget import Budget
from .expression import Expr, evaluate, leaf, _validate_source
from .fragments import Fragments
from .identity import VERSION
from .summary import (
    DEFAULT_PRECISION,
    PRECISION_POLICIES,
    SummaryError,
)

WAREHOUSE_FORMAT = 'warehouse-1'
WAREHOUSE_VERSION = 1


class WarehouseError(ValueError):
    """仓库状态或来源引用错误。"""


class Warehouse:
    """仓内来源集合与摘要状态的容器。

    ``precision`` 决定每个近似摘要保留的哈希数；``precision_policy`` 决定精度
    不同的摘要组合时拒绝（``reject``）还是兼容降阶（``downgrade``）。
    ``budget`` 是查询级共享存储预算，接收缓冲和求值临时态都计入。
    """

    def __init__(self, seed, precision=DEFAULT_PRECISION, precision_policy='reject',
                 budget=None):
        if type(seed) is not int or not 0 <= seed < 2 ** 64:
            raise ValueError('种子超出uint64范围')
        if type(precision) is not int or precision < 1:
            raise ValueError('精度必须是正整数')
        if precision_policy not in PRECISION_POLICIES:
            raise ValueError('精度策略必须是 reject 或 downgrade')
        if budget is not None and not isinstance(budget, Budget):
            raise ValueError('预算必须是 Budget')
        self.seed = seed
        self.precision = precision
        self.precision_policy = precision_policy
        self.budget = budget
        self._sources = {}
        self._leafs = {}
        self._degraded = {}

    # -- 来源登记与片段接收 -------------------------------------------------

    def register(self, name, snapshot, fragments):
        if type(name) is not str or not name:
            raise ValueError('来源名称必须是非空文本')
        if name in self._sources:
            raise WarehouseError('来源 %s 已登记' % name)
        self._sources[name] = Fragments(snapshot, fragments, budget=self.budget)
        return self._sources[name]

    def accept(self, name, fragment_id, state):
        """接收片段；重复发送相同内容是幂等的，内容不同记为冲突。"""
        frags = self._require_source(name)
        if name in self._leafs:
            raise WarehouseError('来源 %s 已定稿，不能再接收片段' % name)
        return frags.accept(fragment_id, frags.snapshot, state)

    def receipt(self, name):
        return self._require_source(name).receipt()

    def missing(self, name):
        return self._require_source(name).missing

    def complete(self, name):
        return self._require_source(name).complete

    def finalize(self, name):
        """校验来源完整、来源域一致与精度兼容，构建可查询的叶摘要。"""
        if name in self._leafs:
            return self._leafs[name]
        frags = self._require_source(name)
        if not frags.complete:
            raise WarehouseError(
                '来源 %s 未完成：缺失 %r，冲突 %r'
                % (name, frags.missing, sorted(frags.conflicts))
            )
        summary, proof = frags.source_summary(self.precision_policy)
        if summary.seed != self.seed:
            raise WarehouseError('来源 %s 的哈希种子与仓库不同' % name)
        if summary.precision != self.precision:
            try:
                summary, record = summary.rebase_precision(
                    self.precision,
                    allow_loss=self.precision_policy == 'downgrade')
            except SummaryError as error:
                raise WarehouseError(
                    '来源 %s 的精度 %d 与仓库精度 %d 不兼容：%s'
                    % (name, summary.precision, self.precision, error)
                ) from error
            self._degraded[name] = record
        source = _validate_source({
            'snapshot': proof['snapshot'],
            'fragments': proof['fragment_ids'],
            'fragment_digests': proof['fragment_digests'],
            'complete': True,
        })
        expr = leaf(summary, source)
        self._leafs[name] = expr
        return expr

    def leaf(self, name):
        if name not in self._leafs:
            raise WarehouseError('来源 %s 尚未定稿' % name)
        return self._leafs[name]

    def sources(self):
        return sorted(self._sources)

    def finalized(self):
        return sorted(self._leafs)

    # -- 查询 ---------------------------------------------------------------

    def query(self, expression, confidence=0.95):
        """在表达式上求值；表达式只含摘要与来源证明。"""
        if isinstance(expression, dict):
            expression = Expr.from_json(expression)
        if not isinstance(expression, Expr):
            raise WarehouseError('查询必须是表达式或 JSON 表达式')
        return evaluate(expression, self.precision_policy, confidence, self.budget)

    def applicability(self):
        """解释当前仓库状态适用于哪些来源，以及它的限制。"""
        sources = []
        for name in sorted(self._sources):
            frags = self._sources[name]
            record = {
                'name': name,
                'snapshot': frags.snapshot,
                'expected_fragments': sorted(frags.expected),
                'received_fragments': sorted(frags.received),
                'fragment_digests': {
                    key: frags.received[key]['digest']
                    for key in sorted(frags.received)
                },
                'missing_fragments': frags.missing,
                'conflicts': sorted(frags.conflicts),
                'complete': frags.complete,
                'finalized': name in self._leafs,
            }
            if name in self._leafs:
                summary = self._leafs[name].summary
                record.update({
                    'hash_version': VERSION,
                    'seed': summary.seed,
                    'precision': summary.precision,
                    'threshold': summary.threshold,
                    'threshold_kind': summary.threshold_kind,
                    'exact': summary.exact,
                    'degraded': self._degraded.get(name),
                    'scope': 'snapshot %s 的已登记片段 %s'
                             % (frags.snapshot, ','.join(sorted(frags.expected))),
                })
            sources.append(record)
        return {
            'format': WAREHOUSE_FORMAT,
            'format_version': WAREHOUSE_VERSION,
            'hash_version': VERSION,
            'hash_domain': 2 ** 64,
            'seed': self.seed,
            'precision': self.precision,
            'precision_policy': self.precision_policy,
            'sources': sources,
            'assumptions': [
                '摘要只覆盖已登记且完成回执的片段；未登记人群不在任何估计内',
                '人群重叠按哈希域估计；不同来源的同一身份在并集中只计一次',
                '近似区间以阈值以下抽样计数为基础，置信水平按查询显式给出',
                '哈希碰撞未单独建模',
            ],
            'not_applicable': [
                '不能解释为未登记来源或快照之外的人群规模',
                '未观测到重叠不等于确定为零，除非结果处于精确状态',
                '不同哈希版本或种子的摘要不能互相组合',
            ],
        }

    # -- 序列化 -------------------------------------------------------------

    def export(self):
        """导出可跨进程恢复的状态；只含摘要、片段回执与来源证明。"""
        sources = {}
        for name in sorted(self._sources):
            frags = self._sources[name]
            sources[name] = {
                'snapshot': frags.snapshot,
                'expected': sorted(frags.expected),
                'received': {
                    key: {
                        'digest': frags.received[key]['digest'],
                        'state': frags.received[key]['state'],
                    }
                    for key in sorted(frags.received)
                },
                'conflicts': {
                    key: dict(value) for key, value in sorted(frags.conflicts.items())
                },
                'finalized': name in self._leafs,
            }
        return {
            'format': WAREHOUSE_FORMAT,
            'format_version': WAREHOUSE_VERSION,
            'hash_version': VERSION,
            'seed': self.seed,
            'precision': self.precision,
            'precision_policy': self.precision_policy,
            'sources': sources,
        }

    @classmethod
    def restore(cls, state, budget=None):
        if not isinstance(state, dict):
            raise WarehouseError('仓库状态必须是对象')
        if state.get('format') != WAREHOUSE_FORMAT:
            raise WarehouseError('仓库格式不支持')
        if state.get('format_version') != WAREHOUSE_VERSION:
            raise WarehouseError('仓库格式版本不支持')
        if state.get('hash_version') != VERSION:
            raise WarehouseError('仓库哈希约定不支持')
        try:
            warehouse = cls(state['seed'], state['precision'],
                            state.get('precision_policy', 'reject'), budget)
        except (KeyError, ValueError) as error:
            raise WarehouseError('仓库状态损坏') from error
        sources = state.get('sources')
        if not isinstance(sources, dict):
            raise WarehouseError('仓库来源状态损坏')
        for name, record in sources.items():
            if not isinstance(record, dict) or not isinstance(record.get('expected'), list):
                raise WarehouseError('来源 %s 的状态损坏' % name)
            try:
                frags = warehouse.register(name, record.get('snapshot'), record['expected'])
            except ValueError as error:
                raise WarehouseError('来源 %s 的状态损坏' % name) from error
            received = record.get('received', {})
            if not isinstance(received, dict):
                raise WarehouseError('来源 %s 的片段状态损坏' % name)
            for fragment_id, item in received.items():
                if not isinstance(item, dict) or 'state' not in item:
                    raise WarehouseError('来源 %s 的片段状态损坏' % name)
                try:
                    frags.accept(fragment_id, frags.snapshot, item['state'])
                except ValueError as error:
                    raise WarehouseError('来源 %s 的片段回执校验失败' % name) from error
                if item.get('digest') != frags.received[fragment_id]['digest']:
                    raise WarehouseError('来源 %s 的片段指纹不一致' % name)
            conflicts = record.get('conflicts', {})
            if conflicts:
                for fragment_id in conflicts:
                    if fragment_id in frags.received:
                        raise WarehouseError('来源 %s 的冲突片段仍有回执' % name)
                    frags.conflicts[fragment_id] = dict(conflicts[fragment_id])
            if record.get('finalized'):
                warehouse.finalize(name)
        return warehouse

    def close(self):
        """释放仓库持有的接收缓冲预算。"""
        for frags in self._sources.values():
            frags.close()
        if self.budget is not None:
            self.budget = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def _require_source(self, name):
        if name not in self._sources:
            raise WarehouseError('未登记来源 %s' % name)
        return self._sources[name]
