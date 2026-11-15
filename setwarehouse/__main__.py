import json
from pathlib import Path
from .exact import Exact
body = json.loads(Path('data/sample.json').read_text())
result = {}
for name, values in body['sets'].items():
    state = Exact(body['seed'])
    for identity in values:
        state.add(identity)
    result[name] = {'exact_count': len(state.values), 'state': state.export()}
print(json.dumps(result, ensure_ascii=False))
