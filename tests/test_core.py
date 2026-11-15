import unittest
from setwarehouse.identity import hash_identity
from setwarehouse.exact import Exact
from setwarehouse.fragments import Fragments

class CoreTests(unittest.TestCase):
    def test_identity_domain_and_null_exclusion(self):
        self.assertNotEqual(hash_identity(True, 1), hash_identity(1, 1))
        self.assertNotEqual(hash_identity('1', 1), hash_identity(1, 1))
        self.assertIsNone(hash_identity(None, 1))

    def test_exact_roundtrip_and_bound(self):
        state = Exact(7, max_items=2)
        for value in [1, 1, None, 2]:
            state.add(value)
        self.assertEqual(Exact.restore(state.export()).export(), state.export())
        with self.assertRaises(MemoryError):
            state.add(3)

    def test_fragment_retry_conflict_and_completion(self):
        fragments = Fragments('s1', ['a', 'b'])
        fragments.accept('a', 's1', {'rows': 0})
        fragments.accept('a', 's1', {'rows': 0})
        with self.assertRaises(ValueError):
            fragments.states()
        with self.assertRaises(ValueError):
            fragments.accept('a', 's1', {'rows': 1})
        with self.assertRaises(ValueError):
            fragments.accept('b', 's2', {})
        fragments.accept('b', 's1', {'rows': 3})
        self.assertEqual(fragments.states(), [{'rows': 0}, {'rows': 3}])
