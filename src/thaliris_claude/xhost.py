"""Claude's dispatch-side foreign launcher and authority-lowering selector reader."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from thaliris import core

from . import codex_transport, roles, store


_HANDOFF_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
TIMEOUTS = {"investigator": 240, "reviewer": 420, "reasoning-specialist": 540}


class Interrupted(RuntimeError):
    """The persisted run or its launcher was cancelled while waiting."""


def selection(session_id: object) -> Path | None:
    session_id = store.native_id(session_id)
    if session_id is None:
        return None
    digest = core.foreign_session_hash(session_id)
    return Path.home() / '.thaliris' / 'xhost-sessions' / f'{digest}.json'


def has_selector(session_id: object) -> bool:
    path = selection(session_id)
    if path is None:
        return False
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        pass  # An unreadable path must not confer authority.
    return True


def is_incoming(session_id: object) -> bool:
    return os.environ.get(store.FOREIGN_MARKER, '').strip() == '1' or has_selector(session_id)


def incoming_context(session_id: object) -> tuple[dict, Path] | None:
    """Verify an exact pre-bound Claude run and return its selected root.

    This is a read-only selector and Core ledger check. It does not create a Host
    association, take the task lock, or read the other Host's authority store.
    """
    path = selection(session_id)
    if path is None:
        return None
    try:
        if path.is_symlink():
            return None
        with path.open('rb') as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        selector = json.loads(raw)
        if not isinstance(selector, dict) or set(selector) != {'root', 'task_id', 'handoff_id'}:
            return None
        root = Path(selector['root'])
        if not root.is_absolute() or str(root.resolve()) != str(root) or not root.is_dir():
            return None
        handoff_id = selector['handoff_id']
        if not isinstance(handoff_id, str) or not _HANDOFF_ID.fullmatch(handoff_id):
            return None
        task_id = selector['task_id']
        state = _selected_state(root, task_id)
        binding, changed = core.bind_foreign_session(state, path.stem)
        if (not changed and binding and binding['task_id'] == task_id and binding['handoff_id'] == handoff_id
                and binding['executor_host'] == 'claude' and binding['run_state'] == 'bound'
                and binding['role'] in roles.CODEX_ROLES):
            return binding, root
    except (ValueError, OSError, TypeError, KeyError, RuntimeError):
        return None
    return None


def incoming_binding(session_id: object) -> dict | None:
    context = incoming_context(session_id)
    return context[0] if context is not None else None


def _selected_state(root: Path, task_id: str) -> dict:
    previous = core.selected_task(root)
    core.select_task(root, task_id)
    try:
        return core._load_state(root, active=True)
    finally:
        core.select_task(root, previous)


def _publish_selector(session_id: str, root: Path, task_id: str, handoff_id: str) -> None:
    path = selection(session_id)
    if path is None:
        raise ValueError('FOREIGN_RUN_INVALID: invalid Codex thread id')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {'root': str(root.resolve()), 'task_id': task_id, 'handoff_id': handoff_id}
    source = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as body:
            source = Path(body.name)
            body.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            body.flush()
            os.fsync(body.fileno())
        os.link(source, path)  # Atomic publish and refuse an existing selector, including another task's.
    finally:
        if source is not None:
            source.unlink(missing_ok=True)


def dispatch_options(marker: dict) -> tuple[str | None, str | None]:
    model, effort = marker.get('codex_model'), marker.get('codex_effort')
    if 'codex_model' in marker and (not isinstance(model, str) or not _MODEL_ID.fullmatch(model)):
        raise ValueError('`codex_model` must be a Codex model id')
    if 'codex_effort' in marker and (not isinstance(effort, str) or effort not in _EFFORTS):
        raise ValueError('`codex_effort` is not a supported reasoning effort')
    return model, effort


def prompt_envelope(prompt: str, model: str | None, effort: str | None) -> str:
    return json.dumps({'prompt': prompt, 'model': model, 'effort': effort}, ensure_ascii=False, separators=(',', ':'))


def relay_identity(payload: dict) -> bool:
    if roles.codex_relay(payload.get('agent_type')) is not None:
        return True
    agent = store.native_id(payload.get('agent_id'))
    if agent is None or payload.get('agent_type') is not None:
        return False
    association = store.resolve_session(payload.get('session_id'))
    if association is None:
        return False
    lifecycle = store.read_lifecycle(*association)
    current = store.dispatch_for_agent(lifecycle, agent)
    if current is not None:
        return bool(current.get('foreign'))
    meta = store.host_agent_meta(payload.get('transcript_path'), agent)
    return bool(meta and roles.codex_relay(meta.get('agentType')) is not None)


def prompt_for(role: str, selected: str, marker: dict) -> str:
    return (roles.get_role(role).instructions.split('## Who you are working for', 1)[0].strip()
            + '\n\nYou are the managed foreign read-only child for the selected assignment below. '
              'Use read_file, list_files, and search_files for repository-relative source inspection. '
              'You cannot run shell commands, delegate, use MCP or modify anything. Return observations in your final report. '
              'Keep any verification you could not perform UNKNOWN. The Controller judges acceptance. '
              'Use only requirement refs selected by the handoff. End your report with exactly one typed block in this format '
              '(use the body none if you have no claim):\n'
              '```thaliris-claims\n'
              '{"claims":[{"requirement":"R1","polarity":"supports|contradicts|unknown","text":"<=300 chars",'
              '"evidence":"<check id or path:line>","basis":"check|read|inference"}]}\n```\n\n'
              f'Selected candidate: {marker["candidate"]}. Requirement refs: {", ".join(marker["requirement_refs"])}.\n\n'
              f'{selected}')


def _match_entry(ctx: dict, handoff_id: str) -> tuple[dict, dict]:
    entry = next((item for item in ctx['lifecycle']['dispatches'] if item['handoff_id'] == handoff_id), None)
    record = store.handoff(ctx['state'], handoff_id)
    if (entry is None or not isinstance(entry.get('foreign'), dict) or record is None
            or record.get('executor_host') != 'codex' or record.get('producer') != entry['role']
            or entry['role'] not in roles.CODEX_ROLES or entry['parent'] != 'controller'):
        raise ValueError('FOREIGN_RUN_INVALID: no matching managed Codex dispatch')
    return entry, record


def authorize_relay(ctx: dict, handoff_id: str, session: str, agent: str, *, claim: bool = False) -> dict:
    entry, record = _match_entry(ctx, handoff_id)
    if (entry['binding'] not in store.EXACT_BINDINGS or entry['agent_id'] != agent
            or entry['foreign']['session_hash'] != core.foreign_session_hash(session)):
        raise ValueError('FOREIGN_RUN_INVALID: relay identity does not match the reservation')
    if (entry['disposed'] or entry['native_status'] in store.TERMINAL_STATUSES or record['run_state'] != 'reserved'
            or entry['foreign'].get('launcher_claimed') is True):
        raise ValueError('FOREIGN_RUN_STATE_CONFLICT: this dispatch cannot start another launcher')
    expected = record.get('transport_agent_id_hash')
    if expected is not None and expected != core.foreign_session_hash(agent):
        raise ValueError('FOREIGN_RUN_INVALID: relay hash does not match the reservation')
    if claim:
        entry['foreign']['launcher_claimed'] = True
        ctx['lifecycle_changed'] = True
    return entry


def cancel_entry(ctx: dict, entry: dict, reason: str) -> dict | None:
    if not entry.get('foreign'):
        return None
    record = store.handoff(ctx['state'], entry['handoff_id'])
    if not record or record.get('run_state') not in ('reserved', 'launched', 'bound', 'cancelled'):
        return None
    result, changed = core.cancel_foreign_run(ctx['state'], entry['handoff_id'], reason)
    ctx['state_changed'] = ctx['state_changed'] or changed
    return result


def cancel_runs(association: tuple[Path, str], match, reason: str) -> None:
    # An old/unmanaged session can end without reading its authority anchor.
    root, task_id = association
    candidates = [item['handoff_id'] for item in store.read_lifecycle(root, task_id)['dispatches']
                  if item.get('foreign') and match(item)]
    if not candidates:
        return
    state = _selected_state(root, task_id)
    if not any((store.handoff(state, handoff_id) or {}).get('run_state') in ('reserved', 'launched', 'bound')
               for handoff_id in candidates):
        return
    with store.transaction(root, task_id) as ctx:
        for entry in ctx['lifecycle']['dispatches']:
            if match(entry):
                cancel_entry(ctx, entry, reason)


def launch(root: Path, task_id: str, handoff_id: str, session: str, agent: str) -> dict:
    with store.transaction(root, task_id) as ctx:
        entry = authorize_relay(ctx, handoff_id, session, agent, claim=True)
        envelope = json.loads(core.foreign_body(root, ctx['state'], handoff_id, 'prompt'))
        prompt, model, effort = envelope['prompt'], envelope['model'], envelope['effort']
        if (model, effort) != (entry['foreign']['model'], entry['foreign']['effort']) or not isinstance(prompt, str):
            raise ValueError('FOREIGN_RUN_INVALID: launcher configuration differs from the Core prompt')
        role = entry['role']

    began = time.monotonic()
    deadline = began + TIMEOUTS[role]
    next_check = began
    worker = None

    def check_live():
        nonlocal next_check
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError('Codex workload exceeded its wall-clock timeout')
        if now >= next_check:
            next_check = now + 0.3
            record = store.handoff(_selected_state(root, task_id), handoff_id)
            current = store.dispatch_for_agent(store.read_lifecycle(root, task_id), agent)
            if (record is None or record.get('run_state') not in ('reserved', 'bound') or current is None
                    or current['handoff_id'] != handoff_id or current['disposed']
                    or current['native_status'] in store.TERMINAL_STATUSES):
                raise Interrupted('Foreign run was cancelled or is no longer active')

    failure = None
    try:
        check_live()
        with codex_transport.connect(root, check_live) as worker:
            thread_id = worker.start_thread(root, model, effort)
            if not store.native_id(thread_id):
                raise ValueError('Invalid Codex thread id')
            digest = core.foreign_session_hash(thread_id)
            with store.transaction(root, task_id) as ctx:
                current, record = _match_entry(ctx, handoff_id)
                if current['disposed'] or current['native_status'] in store.TERMINAL_STATUSES:
                    raise Interrupted('The relay no longer has a live dispatch')
                core.register_foreign_launch(ctx['state'], handoff_id, digest, core.foreign_session_hash(agent))
                binding, changed = core.bind_foreign_session(ctx['state'], digest)
                if not changed or binding['handoff_id'] != handoff_id or binding['run_state'] != 'bound':
                    raise RuntimeError('Core did not pre-bind the Codex thread')
                ctx['state_changed'] = True
            _publish_selector(thread_id, root, task_id, handoff_id)
            next_check = 0
            outcome = worker.run_turn(root, prompt)
    except KeyboardInterrupt:
        failure = 'Launcher interrupted'
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        failure = str(exc) if isinstance(exc, (TimeoutError, Interrupted, RuntimeError)) else type(exc).__name__

    with store.transaction(root, task_id) as ctx:
        entry, record = _match_entry(ctx, handoff_id)
        if record['run_state'] == 'cancelled':
            return {'ok': False, 'handoff_id': handoff_id, 'run_state': 'cancelled', 'cancel_reason': record['cancel_reason']}
        if entry['disposed'] or entry['native_status'] in store.TERMINAL_STATUSES:
            result = cancel_entry(ctx, entry, 'The native relay was fenced before the launcher recorded its result')
            return {'ok': False, **result}
        if failure and (worker is None or worker.text is None or failure == 'Launcher interrupted'):
            result = cancel_entry(ctx, entry, _reason(failure))
            return {'ok': False, **result}
        outcome = 'failed' if failure else outcome
        result = core.record_foreign_result(ctx['state'], handoff_id, worker.text or '', usage=worker.report(began),
                                            outcome=outcome, root=root)
        ctx['state_changed'] = True
        return {'ok': outcome == 'completed', **{key: result[key] for key in ('handoff_id', 'run_state', 'result_sha256', 'claims')}}


def _reason(message: str) -> str:
    return ' '.join(''.join(char if char.isprintable() else ' ' for char in message).split())[:500] or 'Foreign launcher failed'
