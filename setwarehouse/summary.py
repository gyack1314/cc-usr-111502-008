"""有界集合摘要：小集合精确保存，大集合保留哈希阈值以下的完整样本。

摘要不保存原始身份，只保存带类型的 uint64 哈希。摘要状态有三种阈值语义：

``exhaustive``
    阈值就是整个哈希域，``values`` 是完整集合，规模精确。
``topk``
    ``values`` 是目标集合最小的 ``c`` 个哈希，阈值是第 ``c+1`` 小的哈希且
    自身是一个目标元素。阈值以下没有遗漏，抽样比例是 ``threshold / 2**64``。
``common``
    阈值是多个来源共同的取样边界（通常取较小阈值）。阈值以下的所有目标元素
    都完整保留，但阈值本身不一定属于目标集合，也不知道目标是第几阶次序统计量。

集合并、交、差都在共同阈值以下逐值计算，不会把几个基数估计直接相加减。
"""

from __future__ import annotations

import math

from .budget import Budget
from .identity import VERSION, hash_identity

HASH_DOMAIN = 1 << 64
FORMAT = 'summary-1'
FORMAT_VERSION = 1
DEFAULT_PRECISION = 256

PRECISION_POLICIES = ('reject', 'downgrade')
THRESHOLD_KINDS = ('exhaustive', 'topk', 'common')


# -- 二项分布工具 -----------------------------------------------------------

def _log_binomial_cdf(k, n, p):
    """返回 ``P(X <= k)``，其中 ``X ~ Binomial(n, p)``，在对数域求和。"""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    log_combination = 0.0
    terms = []
    for index in range(k + 1):
        if index > 0:
            log_combination += math.log(n - index + 1) - math.log(index)
        terms.append(log_combination + index * log_p + (n - index) * log_q)
    peak = max(terms)
    return math.exp(peak) * math.fsum(math.exp(term - peak) for term in terms)


def _invert_lower(k, p, tail):
    """最小 ``n`` 使 ``P(X >= k | n) >= tail``；该概率关于 ``n`` 单调递增。"""
    if k <= 0:
        return 0
    if k * math.log(p) >= math.log(tail):
        return k
    low, high = k, k + 1
    while 1.0 - _log_binomial_cdf(k - 1, high, p) < tail:
        low = high
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if 1.0 - _log_binomial_cdf(k - 1, middle, p) >= tail:
            high = middle
        else:
            low = middle
    return high


def _invert_upper(k, p, tail):
    """最大 ``n`` 使 ``P(X <= k | n) >= tail``；该概率关于 ``n`` 单调递减。"""
    low = k
    high = max(k + 1, int(math.ceil(k / p)))
    while _log_binomial_cdf(k, high, p) >= tail:
        low = high
        high *= 2
    while low + 1 < high:
        middle = (low + high) // 2
        if _log_binomial_cdf(k, middle, p) >= tail:
            low = middle
        else:
            high = middle
    return low


def _invert_pair(count, p, tail):
    """结果集合阈值 `t` 是第 ``count + 1`` 阶次序统计量时，总体规模 n 的精确区间。

    阈值以下恰有 ``count`` 个元素，阈值本身也是目标元素，因此
    ``X = #(<= t) = count + 1`` 是一个可观测的二项计数，且
    ``P(T <= t | n) = P(Binomial(n, p) >= count + 1)`` 关于 ``n`` 递增，
    ``P(X <= count + 1 | n)`` 关于 ``n`` 递减。分别对两侧尾部 ``tail`` 反演，
    得到置信水平 ``1 - 2 * tail`` 的区间。
    """
    lower = max(count + 1, _invert_lower(count + 1, p, tail))
    upper = max(lower, _invert_upper(count + 1, p, tail))
    return lower, upper


# -- 校验 ------------------------------------------------------------------

class SummaryError(ValueError):
    """摘要相关错误。"""


class SummaryFormatError(SummaryError):
    """摘要状态损坏或不是受支持的格式。"""


class IncompatibleSummary(SummaryError):
    """摘要的哈希约定或种子不同，不能组合。"""


class PrecisionMismatch(SummaryError):
    """两个近似摘要精度不同，且策略要求拒绝组合。"""


def _is_uint64(value):
    return type(value) is int and 0 <= value < HASH_DOMAIN


def _is_precision(value):
    return type(value) is int and value >= 1


def _validate_values(values, threshold):
    if type(values) not in (list, tuple):
        raise SummaryFormatError('摘要值必须是列表')
    previous = -1
    for value in values:
        if not _is_uint64(value):
            raise SummaryFormatError('摘要值必须是 uint64 整数')
        if value <= previous:
            raise SummaryFormatError('摘要值必须严格递增')
        if value >= threshold:
            raise SummaryFormatError('摘要值必须低于阈值')
        previous = value


# -- 流式构造 --------------------------------------------------------------

class Builder:
    """流式构造有界摘要，只保留最小的 ``precision`` 个哈希。"""

    def __init__(self, seed, precision=DEFAULT_PRECISION, budget=None):
        if not _is_uint64(seed):
            raise ValueError('种子超出uint64范围')
        if not _is_precision(precision):
            raise ValueError('精度必须是正整数')
        self.seed = seed
        self.precision = precision
        self._values = set()
        self._overflow = False
        self._budget = budget
        self._charge = 0
        self._reserve()

    def _reserve(self):
        if self._budget is None:
            return
        amount = (self.precision + 1) - self._charge
        self._budget.reserve(amount, 'builder')
        self._charge += amount

    def close(self):
        """释放预算占用；摘要本身已与预算无关。"""
        if self._budget is not None and self._charge:
            self._budget.release(self._charge, 'builder')
            self._charge = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def add(self, identity):
        """加入一个有类型身份；null 和重复身份不增加计数。返回是否为新值。"""
        value = hash_identity(identity, self.seed)
        if value is None:
            return False
        if value in self._values:
            return False
        self._values.add(value)
        if len(self._values) > self.precision + 1:
            self._values.remove(max(self._values))
        if len(self._values) > self.precision:
            self._overflow = True
        return True

    def add_many(self, identities):
        for identity in identities:
            self.add(identity)

    def summary(self):
        values = sorted(self._values)
        if not self._overflow:
            # 所有元素都保留且可枚举，精确状态。
            return Summary(self.seed, self.precision, HASH_DOMAIN, values,
                           'exhaustive')
        # 保留了 precision + 1 个最小哈希；第 precision 个（从 0 数）是第
        # precision + 1 小的元素，作为阈值，其下恰好保留 precision 个值。
        threshold = values[self.precision]
        return Summary(self.seed, self.precision, threshold,
                       values[:self.precision], 'topk')


# -- 摘要 ------------------------------------------------------------------

class Summary:
    """不可变的有界摘要。"""

    __slots__ = ('seed', 'precision', 'threshold', 'values', 'threshold_kind',
                 'exact')

    def __init__(self, seed, precision, threshold, values, threshold_kind):
        if not _is_uint64(seed):
            raise SummaryFormatError('摘要种子超出uint64范围')
        if not _is_precision(precision):
            raise SummaryFormatError('摘要精度必须是正整数')
        if type(threshold) is not int or not 0 < threshold <= HASH_DOMAIN:
            raise SummaryFormatError('摘要阈值超出哈希域')
        if threshold_kind not in THRESHOLD_KINDS:
            raise SummaryFormatError('摘要阈值语义不受支持')
        if (threshold_kind == 'exhaustive') != (threshold == HASH_DOMAIN):
            raise SummaryFormatError('阈值语义与阈值不一致')
        values = tuple(values)
        _validate_values(values, threshold)
        if len(values) > precision:
            raise SummaryFormatError('摘要值数量超过精度上限')
        self.seed = seed
        self.precision = precision
        self.threshold = threshold
        self.values = values
        self.threshold_kind = threshold_kind
        self.exact = threshold_kind == 'exhaustive'

    # 构造 -----------------------------------------------------------------

    @classmethod
    def from_identities(cls, identities, seed, precision=DEFAULT_PRECISION,
                        budget=None):
        builder = Builder(seed, precision, budget=budget)
        try:
            builder.add_many(identities)
            return builder.summary()
        finally:
            builder.close()

    @classmethod
    def from_export(cls, state):
        """从 JSON 状态恢复；只接受摘要，不接受原始身份列表。"""
        if not isinstance(state, dict):
            raise SummaryFormatError('摘要状态必须是对象')
        fmt = state.get('format')
        if fmt == 'exact-1':
            if state.get('hash_version') != VERSION:
                raise IncompatibleSummary('哈希约定不支持')
            seed = state.get('seed')
            if not _is_uint64(seed):
                raise SummaryFormatError('摘要种子损坏')
            values = state.get('values')
            try:
                _validate_values(values, HASH_DOMAIN)
            except SummaryFormatError as error:
                raise SummaryFormatError('精确摘要值损坏') from error
            precision = state.get('precision', max(1, len(values)))
            if not _is_precision(precision) or precision < len(values):
                precision = max(1, len(values))
            return cls(seed, precision, HASH_DOMAIN, values, 'exhaustive')
        if fmt != FORMAT:
            raise SummaryFormatError('摘要格式不支持')
        if state.get('format_version') != FORMAT_VERSION:
            raise SummaryFormatError('摘要格式版本不支持')
        if state.get('hash_version') != VERSION:
            raise IncompatibleSummary('哈希约定不支持')
        if state.get('hash_domain', HASH_DOMAIN) != HASH_DOMAIN:
            raise IncompatibleSummary('哈希域不同')
        seed = state.get('seed')
        precision = state.get('precision')
        threshold = state.get('threshold')
        values = state.get('values')
        kind = state.get('threshold_kind')
        if not _is_uint64(seed):
            raise SummaryFormatError('摘要种子损坏')
        if not _is_precision(precision):
            raise SummaryFormatError('摘要精度损坏')
        if type(threshold) is not int or not 0 < threshold <= HASH_DOMAIN:
            raise SummaryFormatError('摘要阈值损坏')
        if kind not in THRESHOLD_KINDS:
            raise SummaryFormatError('摘要阈值语义损坏')
        try:
            _validate_values(values, threshold)
        except SummaryFormatError as error:
            raise SummaryFormatError('摘要值损坏') from error
        if len(values) > precision:
            raise SummaryFormatError('摘要值数量超过精度上限')
        exact_flag = state.get('exact')
        if exact_flag is not None and type(exact_flag) is not bool:
            raise SummaryFormatError('精确标记损坏')
        if exact_flag is not None and exact_flag != (threshold == HASH_DOMAIN):
            raise SummaryFormatError('精确标记与阈值不一致')
        try:
            return cls(seed, precision, threshold, values, kind)
        except SummaryFormatError as error:
            raise SummaryFormatError('摘要状态自相矛盾') from error

    def export(self):
        return {
            'format': FORMAT,
            'format_version': FORMAT_VERSION,
            'hash_version': VERSION,
            'hash_domain': HASH_DOMAIN,
            'seed': self.seed,
            'precision': self.precision,
            'threshold': self.threshold,
            'threshold_kind': self.threshold_kind,
            'exact': self.exact,
            'value_count': len(self.values),
            'values': list(self.values),
        }

    # 精度调整 -------------------------------------------------------------

    def rebase_precision(self, precision, allow_loss=False):
        """把摘要的存储容量调整到 ``precision``。

        增加容量是无损的；减少容量若会丢弃已知值则需要 ``allow_loss=True``，
        否则抛出 :class:`PrecisionMismatch`。返回 ``(Summary, 降阶记录或 None)``。
        """
        if not _is_precision(precision):
            raise ValueError('精度必须是正整数')
        if precision == self.precision:
            return self, None
        if len(self.values) <= precision:
            # 只是改变容量声明，已知样本与阈值不变。
            return Summary(self.seed, precision, self.threshold, self.values,
                           self.threshold_kind), {
                'kind': 'capacity-change',
                'lossless': True,
                'source_precision': self.precision,
                'result_precision': precision,
            }
        if not allow_loss:
            raise PrecisionMismatch(
                '摘要精度 %d 下调到 %d 会丢弃已知值' % (self.precision, precision)
            )
        new_threshold = self.values[precision]
        # 保留下来的 precision 个值与新阈值本身都是真实目标元素，因此结果
        # 满足 topk 次序统计量语义（无论原状态是 exact 还是 common）。
        new_kind = 'topk'
        record = {
            'kind': 'exact-to-approximate' if self.exact else 'sample-tightened',
            'lossless': False,
            'source_precision': self.precision,
            'result_precision': precision,
            'dropped_values': len(self.values) - precision,
            'source_threshold': self.threshold,
            'result_threshold': new_threshold,
        }
        return Summary(self.seed, precision, new_threshold,
                       self.values[:precision], new_kind), record

    # 组合 -----------------------------------------------------------------

    def combine(self, other, operation, precision_policy='reject'):
        """组合两个摘要，返回 ``(Summary, 组合元数据)``。

        并、交、差都在共同阈值以下逐值计算，交集/差集不会用基数估计相加减。
        精度不同时：``reject`` 直接拒绝；``downgrade`` 把较精细的一方有依据地
        缩到共同精度，并在元数据中记录丢弃的样本。
        """
        if not isinstance(other, Summary):
            raise SummaryError('只能组合摘要')
        if operation not in ('union', 'intersection', 'difference'):
            raise SummaryError('不支持的集合运算')
        if precision_policy not in PRECISION_POLICIES:
            raise ValueError('精度策略必须是 reject 或 downgrade')
        if self.seed != other.seed:
            raise IncompatibleSummary('哈希种子不同，不能组合')

        degraded = []
        if self.exact or other.exact:
            # 至少一侧精确：取较大容量，保留双方的已知值，让结果尽量保持精确。
            precision = max(self.precision, other.precision)
        elif self.precision != other.precision:
            if precision_policy == 'reject':
                raise PrecisionMismatch(
                    '近似摘要精度不同：%d 与 %d；请显式选择 downgrade 策略'
                    % (self.precision, other.precision)
                )
            precision = min(self.precision, other.precision)
            degraded.append({
                'kind': 'precision',
                'policy': precision_policy,
                'source_precisions': [self.precision, other.precision],
                'result_precision': precision,
            })
        else:
            precision = self.precision

        left, record = self.rebase_precision(
            precision, allow_loss=precision_policy == 'downgrade')
        if record is not None:
            degraded.append(record)
        right, record = other.rebase_precision(
            precision, allow_loss=precision_policy == 'downgrade')
        if record is not None:
            degraded.append(record)

        result = _combine_bounded(left, right, precision, operation)

        meta = {
            'operation': operation,
            'source_precisions': [self.precision, other.precision],
            'source_exact': [self.exact, other.exact],
            'source_thresholds': [self.threshold, other.threshold],
            'result_precision': result.precision,
            'result_threshold': result.threshold,
            'result_threshold_kind': result.threshold_kind,
            'result_exact': result.exact,
            'degraded': degraded or None,
        }
        return result, meta

    # 估计 -----------------------------------------------------------------

    def estimate(self, confidence=0.95):
        """返回规模估计、区间和可核对的依据。

        误差条件于哈希域：近似区间对“阈值以下观测计数”做精确二项反演，
        不会用几个去重计数相加减来冒充交集或差集的规模。两侧尾部各取
        ``(1 - confidence) / 4``，为阈值本身也由次序统计量决定留出余量。
        """
        if type(confidence) not in (int, float) or not 0 < confidence < 1:
            raise ValueError('置信水平必须在 0 与 1 之间')
        confidence = float(confidence)
        assumptions = [
            'uint64 哈希在哈希域上均匀且独立；误差解释条件于该哈希域',
            '哈希碰撞未单独建模；原身份不参与查询',
        ]
        if self.exact:
            cardinality = len(self.values)
            assumptions.append('精确状态枚举了全部哈希，规模没有抽样误差')
            return {
                'estimate': cardinality,
                'exact': True,
                'interval': {
                    'lower': cardinality,
                    'upper': cardinality,
                    'confidence': 1.0,
                    'method': 'exact-enumeration',
                },
                'sampling': {
                    'threshold': self.threshold,
                    'threshold_kind': self.threshold_kind,
                    'sample_fraction': 1.0,
                    'sample_count': cardinality,
                    'precision': self.precision,
                },
                'zero_observed': cardinality == 0,
                'zero_is_not_certain': False,
                'assumptions': assumptions,
            }

        rate = self.threshold / HASH_DOMAIN
        count = len(self.values)
        alpha = 1.0 - confidence
        # 阈值本身也由次序统计量决定，因此在目标置信水平之外按更小的尾部
        # 反演（每侧 alpha / 4），覆盖阈值随机性带来的额外波动。
        tail = alpha / 4.0
        if self.threshold_kind == 'topk':
            # 阈值是结果集合的第 count + 1 阶次序统计量：阈值以下恰有 count
            # 个目标元素。对 n 的区间用次序统计量的二项尾部反演。
            lower, upper = _invert_pair(count, rate, tail)
            estimate = math.sqrt(lower * upper)
            method = 'order-statistic-binomial-inversion'
            assumptions.append(
                '阈值是结果集合的次序统计量；区间按次序统计量的二项尾部'
                '反演（每侧 tail = (1-confidence)/4），点估计取区间端点几何平均'
            )
        else:
            # 阈值是来源间的共同取样边界，边界以下的目标元素完整保留。
            # 该边界本身由来源次序统计量决定，覆盖率为近似；在此条件下按
            # 二项抽样对观测计数反演。
            estimate = count / rate
            lower = max(count, _invert_lower(count, rate, tail))
            upper = max(lower, _invert_upper(count, rate, tail))
            method = 'common-threshold-binomial-inversion'
            assumptions.append(
                '阈值是来源间的共同取样边界，本身由次序统计量决定；按其'
                '给定的近似处理，对观测计数做二项精确反演'
            )
        return {
            'estimate': int(round(estimate)),
            'estimate_unrounded': estimate,
            'exact': False,
            'interval': {
                'lower': int(lower),
                'upper': int(upper),
                'confidence': confidence,
                'method': method,
                'tail_probability': tail,
            },
            'sampling': {
                'threshold': self.threshold,
                'threshold_kind': self.threshold_kind,
                'sample_fraction': rate,
                'sample_count': count,
                'precision': self.precision,
            },
            'zero_observed': count == 0,
            'zero_is_not_certain': count == 0,
            'assumptions': assumptions,
        }

    # 其它 -----------------------------------------------------------------

    def __eq__(self, other):
        return (
            isinstance(other, Summary)
            and self.seed == other.seed
            and self.precision == other.precision
            and self.threshold == other.threshold
            and self.threshold_kind == other.threshold_kind
            and self.values == other.values
        )

    def __hash__(self):
        return hash((self.seed, self.precision, self.threshold,
                     self.threshold_kind, self.values))

    def __repr__(self):
        return ('Summary(%s, seed=%d, precision=%d, threshold=%d, values=%d)'
                % (self.threshold_kind, self.seed, self.precision,
                   self.threshold, len(self.values)))


def _combine_bounded(left, right, precision, operation):
    """并、交、差在共同阈值以下逐值计算。

    两侧在 ``min(threshold)`` 以下都完整保留了各自的目标元素，所以该边界以下
    的并、交、差也是完整观测；边界以上只知道被抽样。结果因此以共同边界为阈值。
    """
    threshold = min(left.threshold, right.threshold)
    left_values = {value for value in left.values if value < threshold}
    right_values = {value for value in right.values if value < threshold}
    if operation == 'union':
        values = sorted(left_values | right_values)
    elif operation == 'intersection':
        values = sorted(left_values & right_values)
    else:
        values = sorted(left_values - right_values)
    return _seal(left.seed, precision, threshold, values,
                 complete_everywhere=left.exact and right.exact)


def _seal(seed, precision, threshold, values, complete_everywhere):
    """按已知观测封装结果摘要。

    ``values`` 在 ``threshold`` 以下完整。若已知观测超过容量，就把第
    ``precision + 1`` 个观测当作新阈值：此时结果的前 ``precision`` 个元素
    已知，阈值本身也是目标元素，正好是 ``topk`` 语义。若两个输入都是精确态，
    结果也在整个哈希域上完备。
    """
    values = list(values)
    if complete_everywhere and len(values) <= precision:
        return Summary(seed, precision, HASH_DOMAIN, values, 'exhaustive')
    if len(values) > precision:
        return Summary(seed, precision, values[precision],
                       values[:precision], 'topk')
    return Summary(seed, precision, threshold, values, 'common')
