"""入仓片段身份与完成证据。"""
import hashlib
import json

def fingerprint(body):
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

class Fragments:
    def __init__(self, snapshot, expected):
        self.snapshot = snapshot
        self.expected = frozenset(expected)
        self.received = {}

    def accept(self, fragment_id, snapshot, state):
        if snapshot != self.snapshot or fragment_id not in self.expected:
            raise ValueError('片段不属于当前冻结来源')
        token = fingerprint(state)
        if fragment_id in self.received and self.received[fragment_id]['digest'] != token:
            raise ValueError('重试片段内容冲突')
        self.received[fragment_id] = {'digest': token, 'state': json.loads(json.dumps(state))}

    @property
    def complete(self):
        return set(self.received) == self.expected

    def states(self):
        if not self.complete:
            raise ValueError('来源片段尚未完整')
        return [self.received[key]['state'] for key in sorted(self.received)]
