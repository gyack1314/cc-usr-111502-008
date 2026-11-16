"""集合表达式：叶来源、union、intersection、difference 的有序树。

表达式可以是 JSON 对象（后者可直接随摘要状态序列化），也可以是 Python 的
``Expr`` 节点，避免构造期间出现重复的叶子摘要。每个叶子在求值时提供身份范围的
验证信息。结果只依赖摘要，不读取原始身份。
"""

from __future__ import annotations

import hashlib
import json
import re

from .identity import VERSION
from .summary import (
    PRECISION_POLICIES,
    Summary,
    SummaryError,
    IncompatibleSummary,
)

_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class ExpressionError(ValueError):
    """表达式结构或来源信息错误。"""


def _validate_source(source):
    if not isinstance(source, dict):
        raise ExpressionError('叶来源必须是对象')
    required = ('snapshot', 'fragments', 'fragment_digests', 'complete')
    if any(key not in source for key in required):
        raise ExpressionError('叶来源缺少快照、片段清单、片段指纹或完成标记')
    if source['complete'] is not True:
        raise ExpressionError('叶来源未完成，不能参与组合')
    snapshot = source['snapshot']
    fragments = source['fragments']
    digests = source['fragment_digests']
    if type(snapshot) is not str or not snapshot:
        raise ExpressionError('来源快照必须是非空文本')
    if not isinstance(fragments, list) or not fragments:
        raise ExpressionError('来源片段清单必须是非空列表')
    if any(type(fragment) is not str or not fragment for fragment in fragments):
        raise ExpressionError('来源片段标识必须是非空文本')
    if len(set(fragments)) != len(fragments):
        raise ExpressionError('来源片段清单不能重复')
    if source.get('missing'):
        raise ExpressionError('来源仍有缺失片段')
    if not isinstance(digests, dict) or set(digests) != set(fragments):
        raise ExpressionError('来源片段指纹必须逐一覆盖片段清单')
    for fragment_id, digest in digests.items():
        if type(digest) is not str or not _DIGEST.match(digest):
            raise ExpressionError('片段 %s 的指纹不是 sha256' % fragment_id)
    return {
        'snapshot': snapshot,
        'fragments': sorted(fragments),
        'fragment_digests': {key: digests[key] for key in sorted(digests)},
        'complete': True,
    }


def source_from_proof(proof):
    """把 :meth:`Fragments.source_proof` 的证明转换为叶来源描述。"""
    if not isinstance(proof, dict):
        raise ExpressionError('来源证明必须是对象')
    if proof.get('complete') is not True:
        raise ExpressionError('来源证明未完成')
    if proof.get('hash_version') != VERSION:
        raise IncompatibleSummary('来源证明的哈希约定不支持')
    return _validate_source({
        'snapshot': proof.get('snapshot'),
        'fragments': proof.get('fragment_ids'),
        'fragment_digests': proof.get('fragment_digests'),
        'complete': True,
    })


class Expr:
    """表达式节点。``kind`` 为 ``leaf``/``union``/``intersection``/``difference``。"""

    __slots__ = ('kind', 'summary', 'source', 'children')

    def __init__(self, kind, summary=None, source=None, children=None):
        self.kind = kind
        self.summary = summary
        self.source = source
        self.children = tuple(children or ())
        if kind == 'leaf':
            if not isinstance(summary, Summary) or source is None:
                raise ExpressionError('叶节点需要摘要与来源信息')
            self.source = _validate_source(source)
        elif kind in ('union', 'intersection', 'difference'):
            if not self.children:
                raise ExpressionError('组合节点至少需要一个子节点')
            if any(not isinstance(child, Expr) for child in self.children):
                raise ExpressionError('组合节点的子节点必须是表达式')
        else:
            raise ExpressionError('不支持的表达式类型')

    def to_json(self):
        if self.kind == 'leaf':
            return {
                'op': 'leaf',
                'state': self.summary.export(),
                'source': _json_copy(self.source),
            }
        if self.kind == 'difference':
            return {
                'op': 'difference',
                'left': self.children[0].to_json(),
                'right': self.children[1].to_json(),
            }
        return {'op': self.kind, 'children': [child.to_json() for child in self.children]}

    @classmethod
    def from_json(cls, body):
        if not isinstance(body, dict):
            raise ExpressionError('表达式必须是对象')
        operation = body.get('op')
        if operation == 'leaf':
            if set(body) != {'op', 'state', 'source'}:
                raise ExpressionError('叶表达式字段不正确')
            try:
                summary = Summary.from_export(body['state'])
            except SummaryError as error:
                raise ExpressionError('叶摘要不可用') from error
            return cls('leaf', summary=summary, source=body['source'])
        if operation == 'difference':
            if set(body) != {'op', 'left', 'right'}:
                raise ExpressionError('difference 必须且只能有 left 与 right')
            return cls('difference', children=(
                cls.from_json(body['left']), cls.from_json(body['right'])))
        if operation in ('union', 'intersection'):
            children = body.get('children')
            if not isinstance(children, list):
                raise ExpressionError('组合节点缺少子节点')
            return cls(operation, children=[cls.from_json(child) for child in children])
        raise ExpressionError('不支持的表达式类型')

    def leafs(self):
        if self.kind == 'leaf':
            return [self]
        result = []
        for child in self.children:
            result.extend(child.leafs())
        return result

    def __repr__(self):
        if self.kind == 'leaf':
            return 'Expr(leaf, %r)' % (self.summary.__repr__(),)
        return 'Expr(%s, %r)' % (self.kind, list(self.children))


def leaf(summary, source):
    return Expr('leaf', summary=summary, source=source)


def union(*children):
    return Expr('union', children=children)


def intersection(*children):
    return Expr('intersection', children=children)


def difference(left, right):
    return Expr('difference', children=(left, right))


def _json_copy(value):
    if isinstance(value, dict):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    return value


class Result:
    """一次表达式求值的结果与依据。

    ``summary`` 是结果的摘要状态，可交给 :func:`leaf` 作为后续查询的叶来源，
    不需要回读原始人群。
    """

    __slots__ = ('summary', 'estimate', 'meta', 'sources')

    def __init__(self, summary, estimate, meta, sources):
        self.summary = summary
        self.estimate = estimate
        self.meta = meta
        self.sources = sources

    def export(self):
        return {
            'estimate': _json_copy(self.estimate),
            'evaluation': _json_copy(self.meta),
            'sources': _json_copy(self.sources),
        }

    def __repr__(self):
        return 'Result(estimate=%r)' % (self.estimate,)


def evaluate(expr, precision_policy='reject', confidence=0.95, budget=None):
    """求值表达式；只允许使用摘要状态与来源证明。

    ``budget`` 为共享的查询级存储预算。预算单位是一个 uint64 值槽位：
    求值同时占用所有叶摘要、每个中间结果，以及合并时的临时态（两个操作数
    加结果）。这与接收缓冲共享同一预算。``budget=None`` 时不限制。
    """
    if not isinstance(expr, Expr):
        raise ExpressionError('求值对象必须是表达式')
    if precision_policy not in PRECISION_POLICIES:
        raise ValueError('精度策略必须是 reject 或 downgrade')
    leafs = _check_sources(expr)
    held = []
    try:
        if budget is not None:
            for index, item in enumerate(leafs):
                amount = len(item.summary.values)
                label = 'leaf:%s:%d' % (item.source['snapshot'], index)
                budget.reserve(amount, label)
                held.append((amount, label))
        summary, operations = _evaluate_node(expr, precision_policy, [], budget, held)
    finally:
        if budget is not None:
            for amount, label in reversed(held):
                budget.release(amount, label)
    estimate = summary.estimate(confidence)
    sources = _source_report(expr)
    meta = {
        'precision_policy': precision_policy,
        'operations': operations,
        'result': {
            'exact': summary.exact,
            'threshold_kind': summary.threshold_kind,
            'seed': summary.seed,
            'precision': summary.precision,
            'threshold': summary.threshold,
            'stored_values': len(summary.values),
        },
        'source_domains': _domains(expr),
        'budget': budget.snapshot() if budget is not None else None,
    }
    return Result(summary, estimate, meta, sources)


def result_leaf(result):
    """把一次求值的结果当作后续表达式的叶来源。

    中间结果没有自己的入仓片段回执，来源描述因此标记为派生：快照为
    ``derived:<来源快照...>``，片段标识写成 ``<来源快照>/<片段标识>``，指纹是
    ``sha256(来源快照 + 片段标识 + 原片段指纹)``，仍可逐项回查到原始回执。
    """
    if not isinstance(result, Result):
        raise ExpressionError('只能包装求值结果')
    sources = result.sources
    if not sources:
        raise ExpressionError('结果没有可核对的来源')
    snapshots = sorted({item['snapshot'] for item in sources})
    fragments = []
    digests = {}
    for item in sources:
        if item.get('complete') is not True:
            raise ExpressionError('结果的来源证明不完整')
        for fragment_id, digest in sorted(item['fragment_digests'].items()):
            qualified = '%s/%s' % (item['snapshot'], fragment_id)
            if qualified in digests:
                raise ExpressionError('派生来源的片段标识重复')
            fragments.append(qualified)
            digests[qualified] = _derived_digest(item['snapshot'], fragment_id, digest)
    source = _validate_source({
        'snapshot': 'derived:' + '+'.join(snapshots),
        'fragments': fragments,
        'fragment_digests': digests,
        'complete': True,
    })
    return leaf(result.summary, source)


def _derived_digest(snapshot, fragment_id, digest):
    body = json.dumps([snapshot, fragment_id, digest],
                      ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def evaluate_json(expr_json, precision_policy='reject', confidence=0.95, budget=None):
    """从 JSON 表达式求值。"""
    return evaluate(Expr.from_json(expr_json), precision_policy, confidence, budget)


def _combine_children(summaries, operation, operations, policy, budget):
    """按一次组合的顺序合并子摘要，临时态计入预算并尽快释放。"""
    result = summaries[0]
    for other in summaries[1:]:
        amount = 0
        if budget is not None:
            amount = max(len(result.values), len(other.values))
            budget.reserve(amount, 'combine-temp')
        try:
            result, meta = result.combine(other, operation, policy)
        finally:
            if budget is not None:
                budget.release(amount, 'combine-temp')
        meta['source_count'] = 2
        operations.append(meta)
    return result


def _evaluate_node(expr, policy, operations, budget, held):
    """递归求值；中间结果计入 ``held``，随求值结束统一释放。"""
    if expr.kind == 'leaf':
        return expr.summary, operations
    if expr.kind == 'difference':
        if len(expr.children) != 2:
            raise ExpressionError('difference 只能有两个子节点')
        left, operations = _evaluate_node(expr.children[0], policy, operations, budget, held)
        right, operations = _evaluate_node(expr.children[1], policy, operations, budget, held)
        result = _combine_children([left, right], 'difference', operations, policy, budget)
        _hold_result(budget, held, result)
        return result, operations

    children = []
    for child in expr.children:
        child_summary, operations = _evaluate_node(child, policy, operations, budget, held)
        children.append(child_summary)
    # 先组合阈值更紧（保留样本更少但更确定）的摘要，能尽早收紧结果阈值。
    children.sort(key=lambda item: (item.threshold, item.precision))
    result = _combine_children(children, expr.kind, operations, policy, budget)
    _hold_result(budget, held, result)
    return result, operations


def _hold_result(budget, held, summary):
    """中间结果在求值期间占用预算，结束前由调用方释放。"""
    if budget is None:
        return
    label = 'intermediate'
    budget.reserve(len(summary.values), label)
    held.append((len(summary.values), label))


def _check_sources(expr):
    leafs = expr.leafs()
    if not leafs:
        raise ExpressionError('表达式没有叶来源')
    if len({item.summary.seed for item in leafs}) != 1:
        raise IncompatibleSummary('叶来源的哈希种子不同，不能组合')
    return leafs


def _domains(expr):
    seen = {}
    for item in expr.leafs():
        key = item.source['snapshot']
        seen.setdefault(key, {
            'hash_version': item.summary.export()['hash_version'],
            'seed': item.summary.seed,
        })
    return [{'snapshot': key, **value} for key, value in sorted(seen.items())]


def _source_report(expr):
    return [
        {
            'snapshot': item.source['snapshot'],
            'fragments': list(item.source['fragments']),
            'fragment_digests': dict(item.source['fragment_digests']),
            'complete': True,
            'hash_version': item.summary.export()['hash_version'],
            'seed': item.summary.seed,
            'precision': item.summary.precision,
            'threshold': item.summary.threshold,
            'threshold_kind': item.summary.threshold_kind,
            'exact': item.summary.exact,
        }
        for item in expr.leafs()
    ]
