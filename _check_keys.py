import json, os
for f in ['keys_template.json', 'keys.json']:
    p = os.path.join(os.path.dirname(__file__), f)
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding='utf-8'))
            print(f'=== {f} ===')
            for k, v in d.items():
                if isinstance(v, dict):
                    red = {kk: ('[REDACTED]' if any(x in kk.lower() for x in ('key', 'api', 'secret')) else vv)
                           for kk, vv in v.items()}
                    print(f'  {k}: {red}')
                else:
                    print(f'  {k}: {v}')
        except Exception as e:
            print(f'{f}: ERROR {e}')
# env keys
print('=== ENV ===')
for k in os.environ:
    if any(x in k.upper() for x in ('DEEPSEEK', 'QWEN', 'DASHSCOPE', 'MIMO', 'OPENAI', 'OLLAMA', 'API_KEY')):
        print(f'  {k} = [set]')
