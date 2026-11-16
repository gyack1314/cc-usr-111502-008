import json
import unittest

from setwarehouse.budget import Budget, BudgetExceeded
from setwarehouse.expression import (
    Expr,
    ExpressionError,
    difference,
    evaluate,
    evaluate_json,
    intersection,
    leaf,
    result_leaf,
    union,
)
from setwarehouse.fragments import Fragments
from setwarehouse.identity import hash_identity
from setwarehouse.summary import (
    HASH_DOMAIN,
    IncompatibleSummary,
    PrecisionMismatch,
    Summary,
    SummaryFormatError,
    Builder,
)
from setwarehouse.warehouse import Warehouse, WarehouseError


def make_source(name, values, seed=41, precision=64):
    summary = Summary.from_identities(values, seed, precision)
    fragments = Fragments(name + '-snapshot', ['f1'])
    fragments.accept('f1', name + '-snapshot', summary.export())
    _, proof = fragments.source_summary()
    return leaf(summary, {
        'snapshot': proof['snapshot'],
        'fragments': proof['fragment_ids'],
        'fragment_digests': proof['fragment_digests'],
        'complete': True,
    })


class SummaryBasics(unittest.TestCase):
    def test_small_sets_are_exact(self):
        summary = Summary.from_identities([1, 2, 2, None, 3], 41, 64)
        self.assertTrue(summary.exact)
        estimate = summary.estimate()
        self.assertEqual(estimate['estimate'], 3)
        self.assertEqual(estimate['interval']['lower'], 3)
        self.assertEqual(estimate['interval']['upper'], 3)
        self.assertEqual(Summary.from_export(summary.export()), summary)

    def test_exact_union_intersection_difference(self):
        left = Summary.from_identities(['a', 'b', 'c'], 41, 64)
        right = Summary.from_identities(['b', 'c', 'd'], 41, 64)
        self.assertEqual(left.combine(right, 'union')[0].estimate()['estimate'], 4)
        self.assertEqual(left.combine(right, 'intersection')[0].estimate()['estimate'], 2)
        self.assertEqual(left.combine(right, 'difference')[0].estimate()['estimate'], 1)
        # difference 不是交换的
        self.assertEqual(right.combine(left, 'difference')[0].estimate()['estimate'], 1)
        self.assertNotEqual(
            left.combine(right, 'difference')[0].export()['values'],
            right.combine(left, 'difference')[0].export()['values'],
        )

    def test_large_summary_is_bounded_and_approximate(self):
        summary = Summary.from_identities(('u%d' % i for i in range(20000)), 7, 128)
        self.assertFalse(summary.exact)
        self.assertLessEqual(len(summary.values), 128)
        estimate = summary.estimate()
        self.assertFalse(estimate['exact'])
        self.assertLessEqual(estimate['interval']['lower'], 20000)
        self.assertGreaterEqual(estimate['interval']['upper'], 20000)
        self.assertFalse(estimate['zero_is_not_certain'])

    def test_roundtrip_preserves_approximate_state(self):
        summary = Summary.from_identities(('u%d' % i for i in range(20000)), 7, 128)
        restored = Summary.from_export(json.loads(json.dumps(summary.export())))
        self.assertEqual(restored, summary)
        self.assertEqual(restored.estimate(), summary.estimate())

    def test_corrupt_state_is_rejected(self):
        summary = Summary.from_identities([1, 2, 3], 41, 64)
        state = summary.export()
        state['values'] = list(reversed(state['values']))
        with self.assertRaises(SummaryFormatError):
            Summary.from_export(state)
        state = summary.export()
        state['threshold_kind'] = 'nonsense'
        with self.assertRaises(SummaryFormatError):
            Summary.from_export(state)
        state = summary.export()
        state['seed'] = 1
        state['values'] = [HASH_DOMAIN]
        with self.assertRaises(SummaryFormatError):
            Summary.from_export(state)

    def test_incompatible_seed_is_refused(self):
        left = Summary.from_identities([1, 2], 1, 64)
        right = Summary.from_identities([2, 3], 2, 64)
        with self.assertRaises(IncompatibleSummary):
            left.combine(right, 'union')


class EstimationSemantics(unittest.TestCase):
    def test_intersection_is_not_arithmetic_on_counts(self):
        left = Summary.from_identities(('a%d' % i for i in range(50000)), 5, 128)
        right = Summary.from_identities(('a%d' % i for i in range(25000, 75000)), 5, 128)
        estimate = left.combine(right, 'intersection')[0].estimate()
        naive_upper = left.estimate()['interval']['upper']
        self.assertNotEqual(estimate['estimate'], 0)
        self.assertFalse(estimate['exact'])
        # 真实交集是 25000 人；下界不应该退化成 0 或直接用集合规模相减。
        self.assertGreater(estimate['interval']['lower'], 0)
        self.assertLess(estimate['interval']['lower'], naive_upper)

    def test_zero_intersection_is_uncertain_not_certain(self):
        left = Summary.from_identities(('a%d' % i for i in range(20000)), 5, 128)
        right = Summary.from_identities(('b%d' % i for i in range(20000)), 5, 128)
        estimate = left.combine(right, 'intersection')[0].estimate()
        self.assertEqual(estimate['estimate'], 0)
        self.assertTrue(estimate['zero_observed'])
        self.assertTrue(estimate['zero_is_not_certain'])
        self.assertGreater(estimate['interval']['upper'], 0)

    def test_empty_exact_intersection_is_certain_zero(self):
        left = Summary.from_identities(['a', 'b'], 5, 64)
        right = Summary.from_identities(['c'], 5, 64)
        estimate = left.combine(right, 'intersection')[0].estimate()
        self.assertTrue(estimate['exact'])
        self.assertEqual(estimate['estimate'], 0)
        self.assertFalse(estimate['zero_is_not_certain'])

    def test_confidence_changes_interval_width(self):
        summary = Summary.from_identities(('u%d' % i for i in range(20000)), 7, 128)
        narrow = summary.estimate(0.8)['interval']
        wide = summary.estimate(0.99)['interval']
        self.assertLess(wide['lower'], narrow['lower'])
        self.assertGreater(wide['upper'], narrow['upper'])
        self.assertEqual(narrow['method'], 'order-statistic-binomial-inversion')

    def test_interval_contains_truth_on_sample_sizes(self):
        for size in (1000, 5000, 20000):
            summary = Summary.from_identities(('v%d' % i for i in range(size)), 11, 128)
            interval = summary.estimate(0.99)['interval']
            self.assertLessEqual(interval['lower'], size)
            self.assertGreaterEqual(interval['upper'], size)

    def test_precision_mismatch_rejected_or_downgraded(self):
        left = Summary.from_identities(('a%d' % i for i in range(5000)), 5, 64)
        right = Summary.from_identities(('a%d' % i for i in range(2500, 7500)), 5, 128)
        with self.assertRaises(PrecisionMismatch):
            left.combine(right, 'union')
        result, meta = left.combine(right, 'union', 'downgrade')
        self.assertEqual(result.precision, 64)
        self.assertTrue(any(item['kind'] == 'precision' for item in meta['degraded']))
        self.assertTrue(any(item.get('dropped_values', 0) > 0 for item in meta['degraded']))

    def test_rebase_can_be_lossless_or_explicit(self):
        summary = Summary.from_identities(('u%d' % i for i in range(5000)), 7, 128)
        with self.assertRaises(PrecisionMismatch):
            summary.rebase_precision(16)
        smaller, record = summary.rebase_precision(16, allow_loss=True)
        self.assertEqual(smaller.precision, 16)
        self.assertEqual(len(smaller.values), 16)
        self.assertFalse(record['lossless'])
        wider, record = smaller.rebase_precision(32)
        self.assertTrue(record['lossless'])
        self.assertEqual(wider.values, smaller.values)


class FragmentEvidence(unittest.TestCase):
    def test_duplicate_fragment_is_idempotent(self):
        fragments = Fragments('snap', ['f1', 'f2'])
        state = Summary.from_identities([1, 2], 41, 64).export()
        self.assertTrue(fragments.accept('f1', 'snap', state))
        self.assertFalse(fragments.accept('f1', 'snap', state))
        self.assertEqual(fragments.receipt()['received'], ['f1'])
        self.assertFalse(fragments.complete)

    def test_conflicting_retry_blocks_completion(self):
        fragments = Fragments('snap', ['f1'])
        fragments.accept('f1', 'snap', {'rows': 0})
        with self.assertRaises(ValueError):
            fragments.accept('f1', 'snap', {'rows': 1})
        self.assertFalse(fragments.complete)
        with self.assertRaises(ValueError):
            fragments.states()

    def test_source_summary_requires_complete_domain(self):
        fragments = Fragments('snap', ['f1', 'f2'])
        fragments.accept('f1', 'snap', Summary.from_identities([1], 41, 64).export())
        with self.assertRaises(ValueError):
            fragments.source_summary()
        fragments.accept('f2', 'snap', {'rows': 0})
        with self.assertRaises(ValueError):
            fragments.source_summary()

    def test_state_serialization_does_not_include_identities(self):
        fragments = Fragments('snap', ['f1'])
        fragments.accept('f1', 'snap', Summary.from_identities([1, 2], 41, 64).export())
        encoded = json.dumps(fragments.states())
        self.assertNotIn('identity', encoded)
        self.assertNotIn('"rows"', encoded)


class Expressions(unittest.TestCase):
    def test_json_roundtrip_and_evaluation(self):
        show = make_source('show', [1, 2, 3])
        stay = make_source('stay', [2, 3, 4])
        expr = intersection(show, stay)
        body = json.loads(json.dumps(expr.to_json()))
        result = evaluate_json(body)
        self.assertEqual(result.estimate['estimate'], 2)
        self.assertEqual(Expr.from_json(body).to_json(), expr.to_json())

    def test_difference_order_matters(self):
        left = make_source('left', [1, 2, 3])
        right = make_source('right', [2])
        forward = evaluate(difference(left, right)).estimate['estimate']
        backward = evaluate(difference(right, left)).estimate['estimate']
        self.assertEqual(forward, 2)
        self.assertEqual(backward, 0)

    def test_difference_arity_is_enforced(self):
        left = make_source('left', [1, 2, 3])
        right = make_source('right', [2])
        expr = Expr('difference', children=(left, right, right))
        with self.assertRaises(ExpressionError):
            evaluate(expr)

    def test_incomplete_source_is_refused(self):
        summary = Summary.from_identities([1], 41, 64)
        with self.assertRaises(ExpressionError):
            leaf(summary, {
                'snapshot': 's',
                'fragments': ['f1'],
                'fragment_digests': {'f1': '0' * 64},
                'complete': False,
            })
        with self.assertRaises(ExpressionError):
            leaf(summary, {
                'snapshot': 's',
                'fragments': ['f1', 'f2'],
                'fragment_digests': {'f1': '0' * 64},
                'complete': True,
            })

    def test_result_can_be_reused_without_identities(self):
        left = make_source('left', [1, 2, 3, 4])
        right = make_source('right', [3, 4, 5])
        first = evaluate(union(left, right))
        second = evaluate(difference(result_leaf(first), right))
        self.assertEqual(second.estimate['estimate'], 2)
        self.assertIn('derived:', second.sources[0]['snapshot'])

    def test_budget_covers_leafs_and_intermediates(self):
        left = make_source('left', range(2000), precision=64)
        right = make_source('right', range(1000, 3000), precision=64)
        budget = Budget(64 * 2)
        with self.assertRaises(BudgetExceeded):
            evaluate(union(left, right), budget=budget)
        self.assertEqual(budget.used, 0)
        # 叶摘要 64 + 64，再加合并临时态 64（之后换成中间结果 64），峰值 192。
        budget = Budget(64 * 4)
        result = evaluate(union(left, right), budget=budget)
        self.assertIsNotNone(result)
        self.assertEqual(budget.used, 0)
        self.assertGreater(budget.peak, 0)
        self.assertEqual(union(left, right).kind, 'union')
        with self.assertRaises(BudgetExceeded):
            evaluate(union(left, right), budget=Budget(64 * 3 - 1))


class WarehouseFlow(unittest.TestCase):
    def build(self, budget=None):
        warehouse = Warehouse(41, precision=64, budget=budget)
        for name, values in {
            'show': [1, 2, 3, 3, None],
            'stay': [2, 3, 4],
            'culture': [3, 4, 5],
        }.items():
            warehouse.register(name, name + '-snapshot', ['part-a', 'part-b'])
            half = len(values) // 2
            for fragment_id, part in zip(['part-a', 'part-b'],
                                         [values[:half], values[half:]]):
                warehouse.accept(name, fragment_id,
                                 Summary.from_identities(part, 41, 64).export())
            warehouse.finalize(name)
        return warehouse

    def test_overlap_queries(self):
        warehouse = self.build()
        show, stay, culture = (warehouse.leaf(name)
                               for name in ('show', 'stay', 'culture'))
        both = warehouse.query(intersection(show, stay))
        self.assertEqual(both.estimate['estimate'], 2)
        self.assertTrue(both.estimate['exact'])
        everything = warehouse.query(union(show, stay, culture))
        self.assertEqual(everything.estimate['estimate'], 5)
        only_show = warehouse.query(difference(show, stay))
        self.assertEqual(only_show.estimate['estimate'], 1)

    def test_duplicate_fragment_does_not_double_count(self):
        warehouse = Warehouse(41, precision=64)
        warehouse.register('show', 'show-snap', ['part-a'])
        state = Summary.from_identities([1, 2, 3], 41, 64).export()
        warehouse.accept('show', 'part-a', state)
        warehouse.accept('show', 'part-a', state)
        warehouse.finalize('show')
        self.assertEqual(warehouse.query(warehouse.leaf('show')).estimate['estimate'], 3)
        self.assertFalse(warehouse.finalize('show').summary is None)

    def test_missing_or_conflicting_fragment_blocks_completion(self):
        warehouse = Warehouse(41, precision=64)
        warehouse.register('show', 'show-snap', ['part-a', 'part-b'])
        warehouse.accept('show', 'part-a', Summary.from_identities([1], 41, 64).export())
        self.assertFalse(warehouse.complete('show'))
        with self.assertRaises(WarehouseError):
            warehouse.finalize('show')
        with self.assertRaises(ValueError):
            warehouse.accept('show', 'part-a',
                             Summary.from_identities([2], 41, 64).export())
        self.assertFalse(warehouse.complete('show'))
        with self.assertRaises(WarehouseError):
            warehouse.finalize('show')
        self.assertEqual(warehouse.receipt('show')['conflicts'], ['part-a'])

    def test_state_roundtrip_across_processes(self):
        warehouse = self.build()
        expression = intersection(warehouse.leaf('show'), warehouse.leaf('stay'))
        before = warehouse.query(expression).estimate
        restored = Warehouse.restore(json.loads(json.dumps(warehouse.export())))
        after = restored.query(intersection(restored.leaf('show'),
                                           restored.leaf('stay'))).estimate
        self.assertEqual(before, after)
        self.assertEqual(restored.receipt('show')['digests'],
                         warehouse.receipt('show')['digests'])

    def test_restore_rejects_tampered_receipts(self):
        warehouse = self.build()
        state = json.loads(json.dumps(warehouse.export()))
        state['sources']['show']['received']['part-a']['state']['values'] = [1]
        with self.assertRaises(WarehouseError):
            Warehouse.restore(state)
        state = json.loads(json.dumps(warehouse.export()))
        state['hash_version'] = 'other'
        with self.assertRaises(WarehouseError):
            Warehouse.restore(state)

    def test_applicability_explains_sources(self):
        warehouse = self.build()
        report = warehouse.applicability()
        self.assertEqual(report['seed'], 41)
        self.assertEqual(report['precision'], 64)
        names = [item['name'] for item in report['sources']]
        self.assertEqual(names, ['culture', 'show', 'stay'])
        for item in report['sources']:
            self.assertTrue(item['complete'])
            self.assertEqual(item['expected_fragments'],
                             item['received_fragments'])
            self.assertEqual(sorted(item['fragment_digests']),
                             item['expected_fragments'])
            self.assertIn('scope', item)
        self.assertTrue(report['not_applicable'])

    def test_precision_policy_at_warehouse_boundary(self):
        warehouse = Warehouse(41, precision=32, precision_policy='reject')
        warehouse.register('show', 'show-snap', ['f'])
        fine = Summary.from_identities(range(1000), 41, 64)
        warehouse.accept('show', 'f', fine.export())
        with self.assertRaises(WarehouseError):
            warehouse.finalize('show')

        warehouse = Warehouse(41, precision=32, precision_policy='downgrade')
        warehouse.register('show', 'show-snap', ['f'])
        warehouse.accept('show', 'f', fine.export())
        warehouse.finalize('show')
        self.assertIsNotNone(warehouse.applicability()['sources'][0]['degraded'])


if __name__ == '__main__':
    unittest.main()
