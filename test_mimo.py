import os
from openai import OpenAI

with open('G:/设备数据分析/.env') as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

api_base = os.environ.get('MIMO_API_BASE', '')
api_key = os.environ.get('MIMO_API_KEY', '')
model = os.environ.get('MIMO_MODEL', 'mimo-v2.5')

print(f"API Base: {api_base}")
print(f"API Key: {api_key[:20]}..." if api_key else "API Key: None")
print(f"Model: {model}")

client = OpenAI(api_key=api_key, base_url=api_base)

try:
    r = client.chat.completions.create(
        model=model,
        messages=[{'role': 'user', 'content': '你好'}],
        max_tokens=50,
    )
    print(f"Response: {r.choices[0].message.content}")
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")
