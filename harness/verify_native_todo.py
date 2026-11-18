"""Exercise the official DSH checklist with the Giftia projection and access policy."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from chat_tools_profile import install as install_chat_tools
from role_service import TOOL_REPLY_GUIDANCE
from stage1 import Lab, PROFILE, SOURCE
from stage5_probe import prepare
from verify import require
from verify_stage2 import tool
from verify_stage3 import event_log
from verify_stage4 import GUEST, OWNER, PAGE_ONE, WebCheck, fixture

TOOL = 'todo_write'
TODOS = [{'content': '列出已知条件', 'status': 'completed'},
         {'content': '归纳可行方案', 'status': 'in_progress'}]


def check_turn(check, identity, visible):
    request_id = 'native-todo-' + identity
    prompt = f'请分步骤整理合成资料（{identity}）。'
    answer = '我已经把已知条件列出来了，接下来整理可行的办法。' if visible else '我可以先听你说说这份资料。'
    count = len(check.requests())
    check.plan(prompt, {'tools': [tool(TOOL, request_id, todos=TODOS)]}, {'text': answer})
    check.send(request_id, prompt, identity=identity)
    result = check.wait(request_id, 'ready', identity)
    require(result['reply'] == answer, 'The native tool replaced Isla’s reply')
    requests = check.requests()[count:]
    require(len(requests) == 2, 'Unexpected model turn count')
    for request in requests:
        names = {spec['name'] for spec in request.get('tools', [])}
        require((TOOL in names) == visible,
                'Native checklist was projected to the wrong account')
        chat_tools = {'isla_calculate', 'isla_convert_units'}
        require(names & chat_tools == (chat_tools if visible else set()),
                'Existing chat tools did not retain their permission policy')
        require(TOOL_REPLY_GUIDANCE in json.dumps(request['system'], ensure_ascii=False), 'Missing Isla role instructions')
    events = event_log(check, check.session(request_id))
    todos = [event['data']['todos'] for event in events if event.get('type') == 'todo/write']
    require(todos == ([TODOS] if visible else []), 'Unexpected DSH todo event')
    require(result.get('todos') == (TODOS if visible else None), 'Incorrect web projection')
    require(result['tools'][-1]['status'] == ('ready' if visible else 'failed'), 'Forced invocation was not blocked')
    with closing(sqlite3.connect(check.lab.root / 'web.db')) as db:
        stored, = db.execute('SELECT events FROM jobs WHERE id=?', (request_id,)).fetchone()
    require(all('name' in entry and 'status' in entry for entry in json.loads(stored)),
            'Previous web shell cannot display the stored tool events after rollback')
    print(f'PASS {identity}: native tool, view isolation and role reply', flush=True)


def check_document(check, path=None):
    if path is None:
        path = check.lab.root / 'synthetic-reference.pdf'
        fixture(path)
    attachment = check.upload(path.read_bytes())
    check.wait(attachment, 'ready')
    request_id = 'native-todo-document'
    prompt = '合成资料的实验室标签是什么？'
    count = len(check.requests())
    check.plan(prompt, {'text': f'实验室是 {PAGE_ONE} [p.1]。'})
    check.send(request_id, prompt, attachment)
    result = check.wait(request_id, 'ready')
    requests = check.requests()[count:]
    require(len(requests) == 1 and not any(spec['name'] == TOOL for spec in requests[0].get('tools', [])),
            'Native checklist was exposed in a PDF model request')
    require(result['citations'][0]['page'] == 1 and 'todos' not in result, 'Incorrect PDF reply projection')
    print('PASS synthetic PDF: native checklist is absent and page citation works', flush=True)


def main(parent, gateway, model, pdf):
    root = parent.resolve() / ('native-todo-' + uuid4().hex[:10])
    print(f'Evidence directory: {root}', flush=True)
    lab = Lab(root, gateway, model)
    prepare(lab)
    install_chat_tools(lab)
    package = SOURCE / 'native-todo-bundle'
    manifest = json.loads((package / 'package.json').read_text(encoding='utf-8'))
    subprocess.run([shutil.which('npm'), 'pack', '--ignore-scripts', '--pack-destination', str(lab.pack_dir)],
                   cwd=package, env=lab.env, check=True)
    lab.command('plugin', '--profile', PROFILE, 'add', str(lab.pack_dir / f'{manifest["name"]}-{manifest["version"]}.tgz'))
    check = WebCheck(lab)
    with check.serve('native-todo'):
        check_turn(check, OWNER, True)
        check_turn(check, GUEST, False)
        check_document(check, pdf)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=SOURCE / '.stage1' / 'ui-validation')
    parser.add_argument('--gateway-port', type=int, default=18189)
    parser.add_argument('--model-port', type=int, default=18190)
    parser.add_argument('--pdf', type=Path)
    options = parser.parse_args()
    main(options.root, options.gateway_port, options.model_port, options.pdf)
