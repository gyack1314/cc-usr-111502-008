"""当前小集合精确状态。"""
from .identity import hash_identity, VERSION

class Exact:
    def __init__(self, seed, max_items=1024):
        self.seed = seed
        self.max_items = max_items
        self.values = set()

    def add(self, identity):
        value = hash_identity(identity, self.seed)
        if value is None or value in self.values:
            return
        if len(self.values) >= self.max_items:
            raise MemoryError('精确集合达到上限')
        self.values.add(value)

    def export(self):
        return {'format': 'exact-1', 'hash_version': VERSION, 'seed': self.seed, 'values': sorted(self.values)}

    @classmethod
    def restore(cls, state):
        if state['format'] != 'exact-1' or state['hash_version'] != VERSION:
            raise ValueError('摘要版本不支持')
        values = state['values']
        if values != sorted(set(values)) or any(type(value) is not int or not 0 <= value < 2**64 for value in values):
            raise ValueError('精确摘要值损坏')
        result = cls(state['seed'], max(1024, len(values)))
        result.values = set(values)
        return result
