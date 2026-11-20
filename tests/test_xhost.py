from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from thaliris import core
from thaliris_claude import codex_transport, hooks, store, xhost
from .conftest import SESSION, run_cli, run_hook


RELAY = 'thaliris:codex-reviewer'
AGENT_ID = 'a2147'
FOREIGN_SESSION = '11111111-3333-4444-5555-666666666666'
MARKER = ('thaliris-evidence: {"candidate":"C1","requirement_refs":["R1"],'
          '"codex_model":"gpt-6-luna","codex_effort":"low"}')
CLAIMS = 'Reviewed.\n```thaliris-claims\n{"claims":[{"requirement":"R1","polarity":"unknown",' \
         '"text":"Inspect production shape","evidence":"app.py:1","basis":"read"}]}\n```'


@pytest.fixture(autouse=True)
def selector_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'shared-home'))
    monkeypatch.delenv(store.FOREIGN_MARKER, raising=False)


def _archive_history(task, monkeypatch):
    """Create a real prior Core segment so failed adapter commits must preserve retrieval."""
    root, task_id = task['root'], task['task_id']
    state = core._load_state(root)
    expected = {'id': 'prior-1', 'kind': 'test', 'locator': 'tests/test_xhost.py', 'summary': 'prior archive body'}
    state['evidence_refs'] = [
        {'id': f'prior-{index}', 'kind': 'test', 'locator': 'tests/test_xhost.py', 'summary': 'prior archive body'}
        for index in range(600)
    ]
    state['revision'] += 1
    task['revision'] += 1
    high_water, min_savings = core.STATE_ARCHIVE_HIGH_WATER_BYTES, core.STATE_ARCHIVE_MIN_SAVINGS_BYTES
    monkeypatch.setattr(core, 'STATE_ARCHIVE_HIGH_WATER_BYTES', 0)
    monkeypatch.setattr(core, 'STATE_ARCHIVE_MIN_SAVINGS_BYTES', 0)
    with core._lock(root):
        core._write_state(root, state)
        store.authority(root, task_id).checkpoint()
    monkeypatch.setattr(core, 'STATE_ARCHIVE_HIGH_WATER_BYTES', high_water)
    monkeypatch.setattr(core, 'STATE_ARCHIVE_MIN_SAVINGS_BYTES', min_savings)
    archived = core._load_state(root)
    assert core._segment_refs(archived)
    assert core.task_get(root, 'prior-1')['object'] == expected
    return expected


def _transaction_paths(root, task_id):
    pins = core._pins_path(root, task_id)
    return (core._state_path(root, task_id), store.authority(root, task_id).path(),
            store.lifecycle_path(root, task_id), pins, pins.with_name('archive-pins-required'))


def _snapshot_files(root, task_id):
    return {path: path.read_bytes() if path.is_file() else None for path in _transaction_paths(root, task_id)}


def _assert_files_match(snapshot):
    for path, content in snapshot.items():
        if content is None:
            assert not path.exists() and not path.is_symlink(), path
        else:
            assert path.read_bytes() == content, path


def _event(task, tool, **extra):
    return {'cwd': str(task['root']), 'session_id': SESSION, 'transcript_path': task['transcript'],
            'tool_name': tool, 'permission_mode': 'default', **extra}


def _reserve(task, **options):
    native = {'description': 'Independent review', 'subagent_type': RELAY, 'prompt': 'Review app.py.\n' + MARKER,
              'run_in_background': True, **options}
    return run_hook('PreToolUse', _event(task, 'Agent', tool_use_id='toolu_01Review', tool_input=native))['hookSpecificOutput']


def _prepare(task):
    approved = _reserve(task)
    assert 'updatedInput' in approved, approved
    prompt = approved['updatedInput']['prompt']
    root, task_id = task['root'], task['task_id']
    entry = store.read_lifecycle(root, task_id)['dispatches'][0]
    assert 'Review app.py.' not in prompt and 'C1' not in prompt
    assert approved['updatedInput']['run_in_background']
    assert store.handoff(xhost._selected_state(root, task_id), entry['handoff_id'])['run_state'] == 'reserved'
    meta = Path(task['transcript']).with_suffix('') / 'subagents' / f'agent-{AGENT_ID}.meta.json'
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({'toolUseId': 'toolu_01Review', 'agentType': RELAY}), encoding='utf-8')
    command = hooks.relay_command(entry['handoff_id'])
    assert command in prompt
    return entry['handoff_id'], command


def _relay_call(task, command, *, agent=AGENT_ID, tool='Bash', **options):
    payload = _event(task, tool, agent_id=agent, agent_type=RELAY, tool_input={'command': command, **options})
    return run_hook('PreToolUse', payload)['hookSpecificOutput']


def test_dispatch_and_tool_identity(task):
    handoff_id, command = _prepare(task)
    assert _reserve(task)['permissionDecision'] == 'deny'
    for tool, text, agent in [('Write', command, AGENT_ID), ('Read', command, AGENT_ID), ('Agent', command, AGENT_ID),
                              ('mcp__foo__get_file', command, AGENT_ID), ('Bash', 'ls', AGENT_ID),
                              ('Bash', command + ' && ls', AGENT_ID), ('Bash', command + ' --native-agent fake', AGENT_ID),
                              ('Bash', command, 'invented'), ('PowerShell', command, AGENT_ID)]:
        assert _relay_call(task, text, agent=agent, tool=tool)['permissionDecision'] == 'deny'
    assert _relay_call(task, command, run_in_background=True)['permissionDecision'] == 'deny'
    allowed = _relay_call(task, command)
    assert allowed['permissionDecision'] == 'allow'
    suffix = f" --task-id {task['task_id']} --native-session {SESSION} --native-agent {AGENT_ID}"
    assert allowed['updatedInput']['command'] == command + suffix
    assert allowed['updatedInput']['timeout'] == 600000
    assert run_cli('xhost-run', '--handoff', handoff_id, '--task-id', task['task_id'])[0] != 0
    direct = _event(task, 'Bash', tool_input={'command': command})
    assert run_hook('PreToolUse', direct)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_refuses_unmanaged_disallowed_or_child_dispatch(task):
    for overrides in ({'model': 'haiku'}, {'isolation': 'worktree'}, {'prompt': '{}'},
                      {'prompt': MARKER.replace('"low"', 'false')}):
        assert _reserve(task, **overrides)['permissionDecision'] == 'deny'
    for identity in ({'session_id': 'other'}, {'agent_type': 'thaliris:reviewer', 'agent_id': 'child'},
                     {'agent_type': 'thaliris:reviewer'}, {'permission_mode': 'plan'}):
        payload = _event(task, 'Agent', tool_use_id='toolu_refused',
                         tool_input={'subagent_type': RELAY, 'prompt': MARKER}) | identity
        assert run_hook('PreToolUse', payload)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert store.read_lifecycle(task['root'], task['task_id'])['dispatches'] == []


class FakeCodex:
    def __init__(self, task, handoff_id, check_live, action):
        self.task, self.handoff_id, self.check_live, self.action = task, handoff_id, check_live, action
        self.text = None

    def start_thread(self, root, model, effort):
        assert (model, effort) == ('gpt-6-luna', 'low')
        state = xhost._selected_state(root, self.task['task_id'])
        assert store.handoff(state, self.handoff_id)['run_state'] == 'reserved'
        assert not xhost.has_selector(FOREIGN_SESSION)
        return FOREIGN_SESSION

    def run_turn(self, root, prompt):
        assert 'Review app.py.' in prompt and 'read_file' in prompt
        state = xhost._selected_state(root, self.task['task_id'])
        assert state['revision'] == self.task['revision'] + 3
        record = store.handoff(state, self.handoff_id)
        assert record['run_state'] == 'bound'
        assert record['foreign_session_hash'] == core.foreign_session_hash(FOREIGN_SESSION)
        assert record['transport_agent_id_hash'] == core.foreign_session_hash(AGENT_ID)
        assert xhost.has_selector(FOREIGN_SESSION)
        self.action(self)
        self.text = CLAIMS
        return 'completed'

    def report(self, began):
        return {'model': 'gpt-6-luna', 'effort': 'low', 'cost_basis': 'unknown', 'input_tokens': 23,
                'output_tokens': 17, 'total_tokens': 40, 'tool_uses': 1, 'duration_ms': 4}


def _launcher(task, monkeypatch, action=lambda worker: None):
    handoff_id, command = _prepare(task)
    assert _relay_call(task, command)['permissionDecision'] == 'allow'
    started = []

    @contextmanager
    def connect(root, check_live):
        started.append(True)
        try:
            yield FakeCodex(task, handoff_id, check_live, action)
        finally:
            started.append(False)

    monkeypatch.setattr(codex_transport, 'connect', connect)

    def run():
        return run_cli('xhost-run', '--handoff', handoff_id, '--task-id', task['task_id'], agent=AGENT_ID)

    return handoff_id, run, started


def test_one_launcher_result_claim_usage_and_relay_noop(task, monkeypatch):
    def repeat(worker):
        assert run()[0] != 0

    handoff_id, run, started = _launcher(task, monkeypatch, repeat)
    code, result = run()
    assert code == 0 and result['run_state'] == 'completed'
    root = task['root']
    state = xhost._selected_state(root, task['task_id'])
    record = store.handoff(state, handoff_id)
    assert record['result_source'] == 'foreign-runner'
    assert record['usage']['total_tokens'] == 40
    assert core.foreign_body(root, state, handoff_id, 'result') == CLAIMS
    assert run_cli('get', f'foreign-result:{handoff_id}')[1]['text'] == CLAIMS
    assert run_cli('get', f'foreign-result:{handoff_id}', agent=AGENT_ID)[0] != 0
    assert state['evidence_continuity']['observations'][0]['provenance'] == 'foreign-runner'
    revision = state['revision']
    run_hook('SubagentStop', _event(task, 'Agent', agent_id=AGENT_ID, agent_type=RELAY, last_assistant_message=CLAIMS))
    run_hook('PostToolUse', _event(task, 'Agent', tool_use_id='toolu_01Review',
                                  tool_response={'status': 'completed', 'agentId': AGENT_ID, 'totalTokens': 999}))
    assert run()[0] != 0 and started == [True, False]
    assert xhost._selected_state(root, task['task_id'])['revision'] == revision


@pytest.mark.parametrize('action', ['dispose', 'end', 'timeout', 'partial-timeout'])
def test_cancellation_or_failed_partial_timeout(task, monkeypatch, action):
    def interrupt(worker):
        if action == 'dispose':
            assert run_cli('dispose', '--handoff-id', handoff_id, '--reason', 'user cancelled')[1]['run_state'] == 'cancelled'
        elif action == 'end':
            run_hook('SessionEnd', _event(task, 'Agent', reason='other'))
        else:
            if action == 'partial-timeout':
                worker.text = CLAIMS
            raise TimeoutError('Synthetic timeout')

    handoff_id, run, started = _launcher(task, monkeypatch, interrupt)
    code, result = run()
    assert code != 0 and started == [True, False]
    expected = 'failed' if action == 'partial-timeout' else 'cancelled'
    assert result['run_state'] == expected
    record = store.handoff(xhost._selected_state(task['root'], task['task_id']), handoff_id)
    assert record['run_state'] == expected
    assert ('result_source' in record) == (expected == 'failed')


def test_incoming_binding_exact_read_only_and_no_authority_store(task, monkeypatch):
    root, task_id = task['root'], task['task_id']
    handoff_id = 'Hreceive'
    digest = core.foreign_session_hash(FOREIGN_SESSION)
    with store.transaction(root, task_id) as ctx:
        core.reserve_foreign_handoff(ctx['state'], {'id': handoff_id, 'parent_handoff_id': None, 'producer': 'reviewer',
                                                    'candidate': 'C1', 'requirement_refs': ['R1']},
                                     executor_host='claude', prompt='Inspect app.py', root=root)
        core.register_foreign_launch(ctx['state'], handoff_id, digest, core.foreign_session_hash(AGENT_ID))
        ctx['state_changed'] = True
    xhost._publish_selector(FOREIGN_SESSION, root, task_id, handoff_id)
    assert xhost.incoming_binding(FOREIGN_SESSION) is None
    with store.transaction(root, task_id) as ctx:
        _, ctx['state_changed'] = core.bind_foreign_session(ctx['state'], digest)
    payload = _event(task, 'Read', session_id=FOREIGN_SESSION, tool_input={'file_path': 'app.py'})

    def forbidden(*args, **kwargs):
        pytest.fail('receiving hooks opened the dispatch authority')

    with monkeypatch.context() as receiver:
        receiver.setattr(store, 'authority', forbidden)
        assert run_hook('PreToolUse', payload) is None
        context = run_hook('SessionStart', payload)['hookSpecificOutput']['additionalContext']
        assert 'managed foreign Claude reviewer' in context
        for tool in ('Bash', 'Edit', 'NotebookEdit', 'Skill', 'Agent', 'mcp__server__get_file', 'UnknownTool'):
            assert run_hook('PreToolUse', {**payload, 'tool_name': tool})['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert run_cli('status', session=FOREIGN_SESSION)[0] != 0
        assert store.read_session(FOREIGN_SESSION) is None
    with pytest.raises(FileExistsError):
        xhost._publish_selector(FOREIGN_SESSION, root, task_id, 'OtherHandoff')
    with store.transaction(root, task_id) as ctx:
        _, ctx['state_changed'] = core.cancel_foreign_run(ctx['state'], handoff_id, 'settled by dispatcher')
    assert run_hook('PreToolUse', payload)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert xhost.has_selector(FOREIGN_SESSION)


def _bind_incoming(task, handoff_id='Hscope'):
    root, task_id = task['root'], task['task_id']
    digest = core.foreign_session_hash(FOREIGN_SESSION)
    with store.transaction(root, task_id) as ctx:
        core.reserve_foreign_handoff(ctx['state'], {'id': handoff_id, 'parent_handoff_id': None, 'producer': 'reviewer',
                                                    'candidate': 'C1', 'requirement_refs': ['R1']},
                                     executor_host='claude', prompt='Inspect app.py', root=root)
        core.register_foreign_launch(ctx['state'], handoff_id, digest, core.foreign_session_hash(AGENT_ID))
        ctx['state_changed'] = True
    xhost._publish_selector(FOREIGN_SESSION, root, task_id, handoff_id)
    with store.transaction(root, task_id) as ctx:
        _, ctx['state_changed'] = core.bind_foreign_session(ctx['state'], digest)


def test_incoming_reads_are_cwd_and_repo_scoped(task, tmp_path):
    root = task['root']
    _bind_incoming(task)
    outside = tmp_path / 'outside'
    outside.mkdir()
    secret = outside / 'secret.txt'
    secret.write_text('private', encoding='utf-8')

    allowed = [
        ('Read', {'file_path': 'app.py'}),
        ('Read', {'file_path': str(root / 'app.py')}),
        ('Grep', {'pattern': 'print'}),  # omitted path uses the exact checked cwd
        ('Grep', {'pattern': 'print', 'path': '.'}),
        ('Grep', {'pattern': 'print', 'path': 'app.py', 'glob': '**/*.py'}),
        ('Glob', {'pattern': '**/*.py'}),  # omitted path uses the exact checked cwd
        ('Glob', {'pattern': '*.py', 'path': '.'}),
    ]
    for tool, tool_input in allowed:
        assert run_hook('PreToolUse', _event(task, tool, session_id=FOREIGN_SESSION, tool_input=tool_input)) is None

    denied = [
        ('Read', {'file_path': '../outside/secret.txt'}),
        ('Read', {'file_path': str(secret)}),
        ('Grep', {'pattern': 'private', 'path': '../outside'}),
        ('Grep', {'pattern': 'private', 'path': str(outside)}),
        ('Grep', {'pattern': 'private', 'glob': '../**/*.txt'}),
        ('Glob', {'pattern': '../outside/*.txt'}),
        ('Glob', {'pattern': '*.txt', 'path': str(outside)}),
    ]
    for tool, tool_input in denied:
        result = run_hook('PreToolUse', _event(task, tool, session_id=FOREIGN_SESSION, tool_input=tool_input))
        assert result['hookSpecificOutput']['permissionDecision'] == 'deny', (tool, tool_input)

    wrong_cwd = _event(task, 'Read', session_id=FOREIGN_SESSION, tool_input={'file_path': 'app.py'})
    wrong_cwd['cwd'] = str(tmp_path)
    assert run_hook('PreToolUse', wrong_cwd)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_incoming_reads_reject_symlink_and_junction_targets(task, tmp_path):
    root = task['root']
    _bind_incoming(task, 'Hlink')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('private', encoding='utf-8')
    linked = root / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(outside)], capture_output=True, text=True)
        if result.returncode:
            pytest.skip(f'Windows directory junction creation unavailable: {result.stderr or result.stdout}')
    else:
        linked.symlink_to(outside, target_is_directory=True)
    try:
        for tool, tool_input in [
            ('Read', {'file_path': 'linked/secret.txt'}),
            ('Grep', {'pattern': 'private', 'path': 'linked'}),
            ('Glob', {'pattern': '*.txt', 'path': 'linked'}),
        ]:
            result = run_hook('PreToolUse', _event(task, tool, session_id=FOREIGN_SESSION, tool_input=tool_input))
            assert result['hookSpecificOutput']['permissionDecision'] == 'deny', (tool, tool_input)
    finally:
        if os.name == 'nt':
            linked.rmdir()
        else:
            linked.unlink()


def test_marked_missing_or_invalid_selector_never_a_controller(task, monkeypatch):
    monkeypatch.setenv(store.FOREIGN_MARKER, '1')
    payload = _event(task, 'Read', session_id=FOREIGN_SESSION, tool_input={'file_path': 'app.py'})
    assert run_hook('PreToolUse', payload)['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert 'no live exact managed binding' in run_hook('SessionStart', payload)['hookSpecificOutput']['additionalContext']
    xhost._publish_selector(FOREIGN_SESSION, task['root'], task['task_id'], 'Hother')
    xhost.selection(FOREIGN_SESSION).write_text('bad JSON', encoding='utf-8')
    monkeypatch.delenv(store.FOREIGN_MARKER)
    assert run_hook('PreToolUse', payload)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_dynamic_tool_arguments_and_read_boundary(repo, tmp_path):
    (tmp_path / 'outside.txt').write_text('private', encoding='utf-8')

    def call(name, arguments):
        return codex_transport.inspect_source(repo, name, arguments, lambda: None)

    assert 'print' in call('read_file', {'path': 'app.py'})['contentItems'][0]['text']
    result = call('search_files', {'query': 'print'})
    assert result['success'] and 'app.py:1:' in result['contentItems'][0]['text']
    assert call('list_files', {})['contentItems'][0]['text'] == 'app.py'
    for path in ('../outside.txt', str(tmp_path / 'outside.txt'), 'app.py:stream'):
        assert not call('read_file', {'path': path})['success']
    assert not call('shell', {'path': 'app.py'})['success']
    assert not call('read_file', {'path': 'app.py', 'start_line': True})['success']


def test_jsonrpc_matches_turn_and_usage_with_real_envelope(repo):
    session = codex_transport.CodexSession.__new__(codex_transport.CodexSession)
    session.check_live = lambda: None
    session.pending, session.tool_ids, session.usage = [], set(), {}
    session.thread_id, session.text, session.model, session.effort = 'thread', None, 'gpt-6-luna', 'low'
    events = iter([
        {'id': 9, 'method': 'item/tool/call', 'params': {'threadId': 'other', 'turnId': 'turn', 'callId': 'other-call',
                                                        'namespace': None, 'tool': 'read_file', 'arguments': {'path': 'app.py'}}},
        {'id': 10, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn', 'callId': 'call',
                                                         'tool': 'read_file', 'arguments': {'path': 'app.py'}}},
        {'id': 11, 'method': 'item/commandExecution/requestApproval', 'params': {}},
        {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': 'turn',
                                                           'tokenUsage': {'total': {'inputTokens': 17, 'cachedInputTokens': 8,
                                                                                      'outputTokens': 4, 'totalTokens': 21}}}},
        {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
                                                'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': CLAIMS}}},
        {'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'completed', 'items': []}}},
    ])
    sent = []
    session.next_message = lambda: next(events)
    session.send = sent.append
    session.call = lambda *args: {'turn': {'id': 'turn'}}
    assert session.run_turn(repo, 'review') == 'completed'
    assert session.text == CLAIMS
    assert [message.get('result', {}).get('success') for message in sent] == [False, True, None]
    assert sent[-1]['error']['code'] == -32601
    assert session.report(time.monotonic())['total_tokens'] == 21
    assert session.report(time.monotonic())['tool_uses'] == 1


def test_pre_workload_requires_disabled_mcp_readback(repo):
    worker = codex_transport.CodexSession.__new__(codex_transport.CodexSession)
    calls = []
    worker.send = lambda message: calls.append(message['method'])

    def call(number, method, params):
        calls.append(method)
        if method == 'config/read':
            return {'config': {'mcp_servers': {'configured_server': {'command': 'host-program'}}}}
        if method == 'thread/start':
            assert params['config']['agents']['enabled'] is False
            assert params['config']['mcp_servers'] == {'configured_server': {'enabled': False}}
            assert params['sandbox'] == 'read-only' and params['approvalPolicy'] == 'never'
            assert params['ephemeral'] and 'environments' not in params
            assert {tool['name'] for tool in params['dynamicTools']} == {'read_file', 'list_files', 'search_files'}
            return {'thread': {'id': 'thread'}, 'sandbox': {'type': 'readOnly', 'networkAccess': False}, 'cwd': str(repo),
                    'approvalPolicy': 'never', 'reasoningEffort': 'low', 'model': 'gpt-6-luna'}
        if method == 'mcpServerStatus/list':
            assert params['threadId'] == 'thread'
            return {'data': [{'runtimeStatus': 'running', 'name': 'configured_server', 'tools': {}}], 'nextCursor': None}
        return {}

    worker.call = call
    with pytest.raises(RuntimeError, match='every MCP server is disabled'):
        worker.start_thread(repo, 'gpt-6-luna', 'low')
    assert calls == ['initialize', 'initialized', 'config/read', 'thread/start', 'mcpServerStatus/list']


def test_prelaunch_checkpoint_failure_restores_prior_stores_before_terminal_cancel(task, monkeypatch):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    handoff_id, run, _ = _launcher(task, monkeypatch)
    before_prelaunch = []
    original_start = FakeCodex.start_thread

    def capture_before_bind(self, root, model, effort):
        before_prelaunch.append(_snapshot_files(root, task_id))
        return original_start(self, root, model, effort)

    monkeypatch.setattr(FakeCodex, 'start_thread', capture_before_bind)
    original_checkpoint = store.AuthorityStore.checkpoint
    calls = 0

    def fail_first_checkpoint(self):
        nonlocal calls
        calls += 1
        original_checkpoint(self)
        if calls == 1:
            raise OSError('injected prelaunch checkpoint failure')

    monkeypatch.setattr(store.AuthorityStore, 'checkpoint', fail_first_checkpoint)
    original_cancel = xhost.cancel_entry

    def verify_rollback_before_cancel(ctx, entry, reason):
        _assert_files_match(before_prelaunch[0])
        assert core.task_get(root, 'prior-1')['object'] == prior
        return original_cancel(ctx, entry, reason)

    monkeypatch.setattr(xhost, 'cancel_entry', verify_rollback_before_cancel)
    code, result = run()
    assert code == 3 and result['run_state'] == 'cancelled'
    assert calls == 2 and before_prelaunch
    assert not xhost.has_selector(FOREIGN_SESSION)
    assert core.task_get(root, 'prior-1')['object'] == prior
    assert store.handoff(core._load_state(root), handoff_id)['run_state'] == 'cancelled'


def test_terminal_result_checkpoint_failure_restores_prior_stores(task, monkeypatch):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    _, run, _ = _launcher(task, monkeypatch)
    before_result = []
    original_turn = FakeCodex.run_turn

    def capture_before_result(self, root, prompt):
        before_result.append(_snapshot_files(root, task_id))
        return original_turn(self, root, prompt)

    monkeypatch.setattr(FakeCodex, 'run_turn', capture_before_result)
    original_checkpoint = store.AuthorityStore.checkpoint
    calls = 0

    def fail_terminal_checkpoint(self):
        nonlocal calls
        calls += 1
        original_checkpoint(self)
        if calls == 2:
            raise OSError('injected terminal result checkpoint failure')

    monkeypatch.setattr(store.AuthorityStore, 'checkpoint', fail_terminal_checkpoint)
    code, result = run()
    assert code == 2 and 'injected terminal result checkpoint failure' in result['error']
    assert calls == 2 and before_result
    _assert_files_match(before_result[0])
    state = core._load_state(root)
    record = store.handoff(state, store.read_lifecycle(root, task_id)['dispatches'][0]['handoff_id'])
    assert record['run_state'] == 'bound' and 'result_sha256' not in record
    assert core.task_get(root, 'prior-1')['object'] == prior


def test_lifecycle_publish_failure_rolls_back_ledger_anchor_and_prior_segment(task, monkeypatch):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    _prepare(task)
    before = _snapshot_files(root, task_id)
    original_write = store.write_lifecycle

    def write_then_fail(root, task_id, value):
        original_write(root, task_id, value)
        raise OSError('injected lifecycle publication failure')

    monkeypatch.setattr(store, 'write_lifecycle', write_then_fail)
    with pytest.raises(OSError, match='injected lifecycle publication failure'):
        with store.transaction(root, task_id) as ctx:
            ctx['state']['pending_results'].append('uncommitted terminal result')
            ctx['state']['revision'] += 1
            ctx['state_changed'] = True
            store.note(ctx['lifecycle']['dispatches'][0], 'fault-injection', detail='lifecycle publish')
            ctx['lifecycle_changed'] = True
    _assert_files_match(before)
    assert store.authority(root, task_id).check() is not None
    assert core.task_get(root, 'prior-1')['object'] == prior


def test_failed_checkpoint_preserves_corrupt_pin_registry_bytes(task, monkeypatch):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    pins = core._pins_path(root, task_id)
    pins.write_bytes(b'{broken registry')
    before = _snapshot_files(root, task_id)
    state = core._load_state(root)
    with pytest.raises(ValueError, match='archive pin registry is missing or unreadable'):
        with store.transaction(root, task_id) as ctx:
            ctx['state']['pending_results'].append('must not commit')
            ctx['state']['revision'] += 1
            ctx['state_changed'] = True
    _assert_files_match(before)
    assert pins.read_bytes() == b'{broken registry'
    assert core.task_get(root, 'prior-1')['object'] == prior
    assert state['revision'] == core._load_state(root)['revision']


def test_critical_rolls_back_core_write_when_anchor_checkpoint_fails(task, monkeypatch, tmp_path):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    before = _snapshot_files(root, task_id)
    state = core._load_state(root)
    update = tmp_path / 'update.json'
    update.write_text(json.dumps({'pending_results': ['critical write']}), encoding='utf-8')
    original_checkpoint = store.AuthorityStore.checkpoint

    def checkpoint_then_fail(self):
        original_checkpoint(self)
        raise OSError('injected critical checkpoint failure')

    monkeypatch.setattr(store.AuthorityStore, 'checkpoint', checkpoint_then_fail)
    with pytest.raises(OSError, match='injected critical checkpoint failure'):
        with store.critical(root, task_id):
            core.select_task(root, task_id)
            core.task_update(root, 'controller', state['revision'], str(update))
    _assert_files_match(before)
    assert core.task_get(root, 'prior-1')['object'] == prior


def test_critical_without_outer_checkpoint_rolls_back_failed_core_mode_write(task, monkeypatch):
    prior = _archive_history(task, monkeypatch)
    root, task_id = task['root'], task['task_id']
    before = _snapshot_files(root, task_id)
    anchor = store.authority(root, task_id).path()
    expected = hashlib.sha256(anchor.read_bytes()).hexdigest()
    revision = core._load_state(root)['revision']
    original_write = store.AuthorityStore.write

    def write_then_fail(self, record):
        original_write(self, record)
        raise OSError('injected mode anchor publication failure')

    monkeypatch.setattr(store.AuthorityStore, 'write', write_then_fail)
    with pytest.raises(OSError, match='injected mode anchor publication failure'):
        with store.critical(root, task_id, checkpoint=False) as ctx:
            ctx['store'].switch_mode(expected, revision, 'single-agent', 'fault-injection test')
    _assert_files_match(before)
    assert core.task_get(root, 'prior-1')['object'] == prior
