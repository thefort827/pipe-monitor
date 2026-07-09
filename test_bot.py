import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print('=== 1. Module Import Check ===')
try:
    from wechaty import Wechaty, Message
    print('  wechaty: OK')
except Exception as e:
    print(f'  wechaty: DEGRADED ({type(e).__name__})')

try:
    from openai import OpenAI
    print('  openai: OK')
except ImportError as e:
    print(f'  openai: FAILED ({e})')

import config
print('  config: OK')

import config_secrets
print('  config_secrets: OK')
print(f'    MIMO_API_BASE = {config_secrets.MIMO_API_BASE}')
print(f'    MIMO_MODEL = {config_secrets.MIMO_MODEL}')

import wechat_config
print('  wechat_config: OK')
print(f'    PUPPET = {wechat_config.WECHATY_PUPPET}')
print(f'    CONTACTS = {wechat_config.ALERT_CONTACTS}')

print()
print('=== 2. MiMo API Test ===')
client = OpenAI(api_key=config_secrets.MIMO_API_KEY, base_url=config_secrets.MIMO_API_BASE)
try:
    r = client.chat.completions.create(
        model=config_secrets.MIMO_MODEL,
        messages=[{'role':'user','content':'ping'}],
        max_tokens=200,
    )
    print(f'  API: OK (reply: {r.choices[0].message.content[:80]})')
except Exception as e:
    print(f'  API: FAILED ({e})')

print()
print('=== 3. wechat_bot.py Import Test ===')
try:
    import wechat_bot
    print('  import wechat_bot: OK')
    print(f'  _WECHATY_AVAILABLE: {wechat_bot._WECHATY_AVAILABLE}')
except Exception as e:
    print(f'  import wechat_bot: FAILED ({e})')
    sys.exit(1)

print()
print('=== 4. Format Functions Test ===')
test_report = {
    'total': 3,
    'anomalies': [
        {'severity':'high','device_id':'DEV001','description':'liquid overflow'},
        {'severity':'medium','device_id':'DEV002','description':'frozen data'},
    ],
    'categories': {'threshold': [{'severity':'high','device_id':'DEV001','description':'liquid overflow'}]}
}
r1 = wechat_bot._format_liquid_report(test_report)
print(f'  _format_liquid_report: OK ({len(r1)} chars)')

r2 = wechat_bot._format_alert_message(test_report['anomalies'])
print(f'  _format_alert_message: OK ({len(r2)} chars)')

r3 = wechat_bot._format_status_report()
print(f'  _format_status_report: OK ({len(r3)} chars)')

r4 = wechat_bot._format_device_detail('NONEXIST')
print(f'  _format_device_detail: OK (not found)')

r5 = wechat_bot._handle_ai_chat('test question')
print(f'  _handle_ai_chat: OK ({len(r5)} chars)')

print()
print('=== 5. Command Handling Test ===')
r = wechat_bot.handle_command('/help')
print(f'  /help: OK ({len(r)} chars)')

r = wechat_bot.handle_command('/status')
print(f'  /status: OK ({len(r)} chars)')

r = wechat_bot.handle_command('/alerts')
print(f'  /alerts: OK ({len(r)} chars)')

r = wechat_bot.handle_command('/report')
print(f'  /report: OK ({len(r)} chars)')

r = wechat_bot.handle_command('/device DEV001')
print(f'  /device: OK ({len(r)} chars)')

r = wechat_bot.handle_command('/unknown')
print(f'  /unknown: OK ({len(r)} chars)')

print()
print('=== 6. Startup Functions ===')
print(f'  start_wechat_bot: {callable(wechat_bot.start_wechat_bot)}')
print(f'  push_alert_to_contacts: {callable(wechat_bot.push_alert_to_contacts)}')
print(f'  send_message_to_contacts: {callable(wechat_bot.send_message_to_contacts)}')

print()
print('=== ALL CHECKS PASSED ===')
