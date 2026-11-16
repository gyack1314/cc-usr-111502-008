"""入仓片段身份与完成证据。

片段清单由固定 ``snapshot`` 和预先登记的 ``fragment_id`` 定义；每个片段必须提交
完成回执及其摘要状态。重复发送同一片段是幂等的，但内容不同即视为冲突。清单不完整
或存在冲突时，不能宣称来源完成，也不能返回可用于组合的状态。
"""

import hashlib
import json

from .summary import Summary, SummaryError, IncompatibleSummary


def fingerprint(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _canonical_state(state):
    """规范化片段状态，保证同一内容的 JSON 表述得到同一指纹。

    接收阶段不要求状态已经是摘要；真正的摘要校验发生在组合时
    （:meth:`Fragments.source_proof` / :meth:`Fragments.source_summary`）。
    """
    try:
        return json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')))
    except (TypeError, ValueError) as error:
        raise ValueError('片段状态必须是可序列化的 JSON') from error


def _state_slots(state):
    """接收缓冲占用的值槽位；非摘要状态按 0 计。"""
    if isinstance(state, dict) and isinstance(state.get('values'), list):
        return len(state['values'])
    return 0


class Fragments:
    def __init__(self, snapshot, expected, budget=None):
        if type(snapshot) is not str or not snapshot:
            raise ValueError('来源快照必须是非空文本')
        if not expected:
            raise ValueError('来源片段清单不能为空')
        if any(type(key) is not str or not key for key in expected):
            raise ValueError('片段标识必须是非空文本')
        if len(set(expected)) != len(list(expected)):
            raise ValueError('片段清单不能重复')
        self.snapshot = snapshot
        self.expected = frozenset(expected)
        self.received = {}
        self.conflicts = {}
        self._budget = budget

    def accept(self, fragment_id, snapshot, state):
        """接收一个片段回执。

        重复回执只有在规范化内容完全一致时才被接受；内容不同的重复回执会记录为
        冲突，并拒绝该片段的新内容。返回 ``True`` 表示这是首次接收，``False``
        表示幂等重试。接收缓冲计入共享预算。
        """
        if snapshot != self.snapshot or fragment_id not in self.expected:
            raise ValueError('片段不属于当前冻结来源')
        try:
            canonical = _canonical_state(state)
        except ValueError as error:
            raise ValueError('片段状态不是可接受的摘要') from error
        token = fingerprint(canonical)
        if fragment_id in self.conflicts:
            raise ValueError('片段已存在冲突，拒绝继续接收')
        existing = self.received.get(fragment_id)
        if existing is not None:
            if existing['digest'] != token:
                self.conflicts[fragment_id] = {
                    'expected_digest': existing['digest'],
                    'received_digest': token,
                }
                raise ValueError('重试片段内容冲突')
            return False
        amount = _state_slots(canonical)
        if self._budget is not None:
            self._budget.reserve(amount, 'receive:%s' % fragment_id)
        self.received[fragment_id] = {
            'digest': token,
            'state': canonical,
            'charge': amount,
        }
        return True

    def close(self):
        """释放接收缓冲占用的预算；之后不再接受片段。"""
        if self._budget is None:
            return
        for fragment_id, record in self.received.items():
            if record['charge']:
                self._budget.release(record['charge'], 'receive:%s' % fragment_id)
                record['charge'] = 0
        self._budget = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    @property
    def missing(self):
        return sorted(self.expected - set(self.received))

    @property
    def complete(self):
        return not self.conflicts and not self.missing

    def receipt(self):
        """返回可核对的完成事实，不返回摘要状态。"""
        return {
            'snapshot': self.snapshot,
            'expected': sorted(self.expected),
            'received': sorted(self.received),
            'missing': self.missing,
            'conflicts': sorted(self.conflicts),
            'digests': {
                fragment_id: self.received[fragment_id]['digest']
                for fragment_id in sorted(self.received)
            },
            'complete': self.complete,
        }

    def states(self):
        if not self.complete:
            raise ValueError('来源片段尚未完整或存在冲突')
        return [self.received[key]['state'] for key in sorted(self.received)]

    def source_proof(self, precision=None):
        """校验所有片段属于同一来源域，并返回组合所需的核对信息。

        ``precision`` 非空时，还要求每个片段摘要的精度与之一致。
        """
        if not self.complete:
            raise ValueError('来源片段尚未完整或存在冲突')
        summaries = []
        for fragment_id in sorted(self.received):
            try:
                summary = Summary.from_export(self.received[fragment_id]['state'])
            except SummaryError as error:
                raise ValueError('片段 %s 的摘要不可用' % fragment_id) from error
            summaries.append((fragment_id, summary))
        tags = {
            (record['state'].get('hash_version'), record['state'].get('seed'))
            for record in self.received.values()
        }
        if len(tags) != 1:
            raise IncompatibleSummary('片段摘要的来源域不一致')
        hash_version, seed = next(iter(tags))
        for fragment_id, summary in summaries:
            if precision is not None and summary.precision != precision:
                raise SummaryError('片段 %s 的精度不一致' % fragment_id)
            if hash_version is None or seed != summary.seed:
                raise IncompatibleSummary('片段摘要的来源域不一致')
        return {
            'snapshot': self.snapshot,
            'fragment_ids': [fragment_id for fragment_id, _ in summaries],
            'fragment_digests': {
                fragment_id: self.received[fragment_id]['digest']
                for fragment_id, _ in summaries
            },
            'hash_version': hash_version,
            'seed': seed,
            'precision': (
                precision if precision is not None
                else summaries[0][1].precision
            ),
            'complete': True,
        }

    def source_summary(self, precision_policy='reject'):
        """组合所有片段摘要，返回 ``(Summary, proof)``。

        多次执行是确定性的：片段顺序固定，重试幂等。合并临时态计入共享预算。
        """
        proof = self.source_proof()
        summaries = [Summary.from_export(self.received[key]['state'])
                     for key in sorted(self.received)]
        result = summaries[0]
        for other in summaries[1:]:
            amount = 0
            if self._budget is not None:
                amount = max(len(result.values), len(other.values))
                self._budget.reserve(amount, 'fragment-merge')
            try:
                result, _meta = result.combine(other, 'union', precision_policy)
            finally:
                if self._budget is not None:
                    self._budget.release(amount, 'fragment-merge')
        return result, proof
