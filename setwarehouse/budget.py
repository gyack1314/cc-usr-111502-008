"""一次查询的存储预算。

预算以“同时保留的 uint64 值槽位”为单位，覆盖查询同时持有的叶摘要、组合临时态和
入仓接收缓冲，而不是只限制单个摘要。预算对象可被多个摘要和片段接收器共享。
"""

from __future__ import annotations

import contextlib


class BudgetExceeded(MemoryError):
    """超出查询级存储预算。"""


class Budget:
    """共享的存储预算。``max_values`` 是同时保留值槽位的上限。"""

    def __init__(self, max_values):
        if type(max_values) is not int or max_values < 0:
            raise ValueError('预算上限必须是非负整数')
        self.max_values = max_values
        self.used = 0
        self.peak = 0
        self.charges = {}

    @property
    def remaining(self):
        return self.max_values - self.used

    def reserve(self, amount, label=''):
        if type(amount) is not int or amount < 0:
            raise ValueError('预算占用必须是非负整数')
        if self.used + amount > self.max_values:
            raise BudgetExceeded(
                '存储预算不足：已占用 %d，本次需要 %d，上限 %d（%s）'
                % (self.used, amount, self.max_values, label or '未标注')
            )
        self.used += amount
        self.peak = max(self.peak, self.used)
        self.charges[label] = self.charges.get(label, 0) + amount
        return amount

    def release(self, amount, label=''):
        if type(amount) is not int or amount < 0:
            raise ValueError('预算释放必须是非负整数')
        if amount > self.used:
            raise ValueError('释放量超过已占用预算')
        self.used -= amount
        if label in self.charges:
            remaining = self.charges[label] - amount
            if remaining > 0:
                self.charges[label] = remaining
            else:
                del self.charges[label]

    @contextlib.contextmanager
    def hold(self, amount, label=''):
        self.reserve(amount, label)
        try:
            yield
        finally:
            self.release(amount, label)

    def snapshot(self):
        return {
            'max_values': self.max_values,
            'used': self.used,
            'peak': self.peak,
            'remaining': self.remaining,
            'charges': dict(sorted(self.charges.items())),
        }

    def __repr__(self):
        return 'Budget(max=%d, used=%d, peak=%d)' % (self.max_values, self.used, self.peak)
