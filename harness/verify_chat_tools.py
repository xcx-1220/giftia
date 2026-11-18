"""Verify tool results and Isla's reply contract through the existing web gateway."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from chat_tools_profile import MARKER, PACKAGE, install
from role_service import TOOL_REPLY_GUIDANCE
from stage1 import GATEWAY_PORT, Lab, MODEL_PORT, PROFILE, SOURCE
from stage5_probe import prepare
from verify import require
from verify_stage2 import tool
from verify_stage3 import event_log
from verify_stage4 import GUEST, OWNER, WebCheck

CALCULATE = 'isla_calculate'
CONVERT = 'isla_convert_units'
NORMAL = '我在。'


def check_tools(check, name, text, identity, *, action=None, answer=NORMAL, result_text=None, error=False, visible=False):
    call_id = f'chat-tool-{name}'
    before = len(check.requests())
    actions = ({'tools': [tool(action[0], call_id, **action[1])]},) if action else ()
    check.plan(text, *actions, {'text': answer})
    check.send(f'chat-tools-{name}', text, identity=identity)
    item = check.wait(f'chat-tools-{name}', 'ready', identity)
    require(item['reply'] == answer, f'{name}: raw tool output replaced the model answer')
    requests = check.requests()[before:]
    require(len(requests) == (2 if action else 1), f'{name}: unexpected model call count')
    for request in requests:
        names = {spec['name'] for spec in request['tools']} if request.get('tools') else set()
        require(names.intersection({CALCULATE, CONVERT}) == ({CALCULATE, CONVERT} if visible else set()),
                f'{name}: exposed wrong capabilities')
        require(TOOL_REPLY_GUIDANCE in json.dumps(request['system'], ensure_ascii=False),
                f'{name}: missing Isla voice guidance in actual model request')
    if action:
        events = event_log(check, check.session(f'chat-tools-{name}'))
        results = [event['data']['message'] for event in events if event.get('type') == 'tool/result']
        require(len(results) == 1 and results[0]['toolCallId'] == call_id and results[0]['isError'] == error,
                f'{name}: tool result did not match execution')
        if result_text:
            require(result_text in json.dumps(results[0]['content'], ensure_ascii=False), f'{name}: incorrect result')
        final_blocks = [block for entry in requests[-1]['messages'] for block in entry['content']]
        require(any(block['type'] == 'tool_result' and block['tool_use_id'] == call_id for block in final_blocks),
                f'{name}: model did not receive the result')
        require([state['status'] for state in item['tools']] == ['running', 'failed' if error else 'ready'],
                f'{name}: incorrect browser tool status')
    print(f'PASS {name}', flush=True)


def main(parent, gateway_port, model_port):
    root = parent.resolve() / ('chat-tools-' + uuid4().hex[:10])
    print(f'Evidence directory: {root}', flush=True)
    lab = Lab(root, gateway_port, model_port)
    prepare(lab)
    archive = install(lab)
    print(f'{archive.name} SHA-256 {sha256(archive.read_bytes()).hexdigest()}', flush=True)
    check = WebCheck(lab)
    with check.serve('chat-tools'):
        check_tools(check, 'calculate', '帮我算 276 的 18.5% 吧。', OWNER, visible=True,
                    action=(CALCULATE, {'operation': 'percent_of', 'left': 18.5, 'right': 276}),
                    result_text='51.06', answer='我算过啦，是 51.06。')
        check_tools(check, 'convert', '32 华氏度是多少摄氏度？', OWNER, visible=True,
                    action=(CONVERT, {'value': 32, 'from': 'F', 'to': 'C'}),
                    result_text='0 C', answer='正好是 0℃，就是水开始结冰的温度。')
        check_tools(check, 'division-failed', '有点好奇，9 除以 0 呢？', OWNER, visible=True, error=True,
                    action=(CALCULATE, {'operation': 'divide', 'left': 9, 'right': 0}),
                    answer='这个算不出数来。除数是零，这个除法没有定义。')
        check_tools(check, 'units-failed', '试试把两米换算成两公斤。', OWNER, visible=True, error=True,
                    action=(CONVERT, {'value': 2, 'from': 'm', 'to': 'kg'}),
                    answer='这两个量不同呀。两米是长度，公斤是质量，没法直接换。')
        check_tools(check, 'guest', '访客测试，想问 2 加 2。', GUEST, error=True,
                    action=(CALCULATE, {'operation': 'add', 'left': 2, 'right': 2}))
    config = lab.profile / 'cordis.patch.yml'
    enabled = config.read_text(encoding='utf-8')
    config.write_text(enabled.replace(MARKER, MARKER + '  disabled: true\n'), encoding='utf-8')
    with check.serve('disabled'):
        check_tools(check, 'disabled', '停用工具后还在吗？', OWNER)
    lab.command('plugin', '--profile', PROFILE, 'remove', PACKAGE)
    config.write_text(enabled.split(MARKER, 1)[0], encoding='utf-8')
    with check.serve('uninstalled'):
        check_tools(check, 'uninstalled', '卸载工具后再打个招呼。', OWNER)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=SOURCE / '.stage1' / 'r2-validation')
    parser.add_argument('--gateway-port', type=int, default=GATEWAY_PORT)
    parser.add_argument('--model-port', type=int, default=MODEL_PORT)
    args = parser.parse_args()
    main(args.root, args.gateway_port, args.model_port)
