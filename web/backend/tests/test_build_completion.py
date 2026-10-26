"""Regression for a final AI reply abandoning an unpublished build."""
import asyncio

import pytest

import agent
import toolkit
from test_agent import chunk, entry, scripted, store, tool_call_chunks


@pytest.fixture(autouse=True)
def fast_workspace(monkeypatch):
    async def ready(*args):
        pass
    monkeypatch.setattr(agent, 'prepare_workspace', ready, raising=False)
    monkeypatch.setattr(agent, 'prepare_guide', ready)
    monkeypatch.setattr(agent, 'system_prompt', lambda *args: 'Complete the requested build.')


def play(monkeypatch, store, entry, responses, *, options=None, budget=None):
    fake, calls = scripted(responses)
    monkeypatch.setattr(agent.litellm, 'acompletion', fake)
    chat = store.create_chat(**({'budget': budget} if budget else {}))
    async def run():
        await agent.start_turn(store, chat['id'], 'Build a detailed barn', entry['id'],
                               options or {'permissions': 'full'})
        await agent._runs[chat['id']].task
    asyncio.run(run())
    return chat['id'], calls


def test_unfinished_final_reply_continues_then_records_explicit_blocker(monkeypatch, store, entry):
    budget = {'status': 'set', 'amount_cents': 20000}
    chat_id, calls = play(monkeypatch, store, entry, [
        [chunk(content='I have not completed the barn yet. Parts setup succeeded; construction remains unfinished.')],
        tool_call_chunks('pause', 'finish_request', {'status': 'blocked', 'reason': 'LEGO stock service is offline.',
                                                  'next_step': 'Continue when the service returns.'}),
        [chunk(content='The draft is saved. LEGO stock checking is unavailable.')],
    ], budget=budget)
    assert len(calls) == 3
    state = store.run_state(chat_id)
    assert state['status'] == 'blocked'
    assert state['continuations'] == 1
    assert state['budget']['maximum_cents'] == 21000
    assert not store.models(chat_id)
    messages = store.messages(chat_id)
    first = next(m for m in messages if isinstance(m.get('content'), str) and m['content'].startswith('I have not'))
    assert not first.get('_final_answer')
    assert messages[-1]['_final_answer']
    assert any('publish_model' in str(m.get('content')) for m in calls[1]['messages'])


def test_repeated_unfinished_replies_stop_with_a_saved_reason(monkeypatch, store, entry):
    chat_id, calls = play(monkeypatch, store, entry, [[chunk(content='More work remains.')] for _ in range(4)])
    assert len(calls) == 4
    assert store.run_state(chat_id)['status'] == 'blocked'
    assert store.run_state(chat_id)['continuations'] == 3
    assert 'draft' in store.messages(chat_id)[-1]['content'].lower()
    assert not agent.is_running(chat_id)


@pytest.mark.parametrize('options', [{'mode': 'plan'}, {'permissions': 'read_only'}])
def test_planning_and_read_only_answers_do_not_require_a_model(monkeypatch, store, entry, options):
    chat_id, calls = play(monkeypatch, store, entry, [[chunk(content='Here is the plan.')]], options=options)
    assert len(calls) == 1
    assert store.run_state(chat_id)['status'] == 'completed'


def test_question_can_end_explicitly_without_creating_a_model(monkeypatch, store, entry):
    chat_id, calls = play(monkeypatch, store, entry, [
        tool_call_chunks('answer', 'finish_request', {'status': 'answered', 'reason': 'The user asked how pricing works.',
                                                    'next_step': 'Choose a design when ready.'}),
        [chunk(content='Prices come from the US LEGO catalogue.')],
    ])
    assert len(calls) == 2
    assert store.run_state(chat_id)['status'] == 'answered'


def test_existing_model_does_not_complete_an_edit(monkeypatch, store, entry, tmp_path):
    chat = store.create_chat()
    store.add_model(chat['id'], 'Old barn', tmp_path / 'old.mpd', [])
    fake, calls = scripted([[chunk(content='I still need to edit the roof.')] for _ in range(4)])
    monkeypatch.setattr(agent.litellm, 'acompletion', fake)
    async def run():
        await agent.start_turn(store, chat['id'], 'Edit the roof', entry['id'], {'permissions': 'full'})
        await agent._runs[chat['id']].task
    asyncio.run(run())
    assert len(calls) == 4
    assert store.run_state(chat['id'])['status'] == 'blocked'


def test_build_actions_cannot_be_dismissed_as_an_informational_answer(monkeypatch, store, entry):
    import os
    import settings
    os.chmod(settings.DATA_DIR.parent, 0o755)
    chat_id, calls = play(monkeypatch, store, entry, [
        tool_call_chunks('notes', 'write_file', {'path': 'output/NOTES.md', 'content': 'Build the red barn next.'}),
        tool_call_chunks('answer', 'finish_request', {'status': 'answered', 'reason': 'Work remains.', 'next_step': 'Continue.'}),
        [chunk(content='I have not built it yet.')],
        tool_call_chunks('pause', 'finish_request', {'status': 'needs_input', 'reason': 'The user must choose between two required layouts.',
                                                  'next_step': 'Choose the accessible stall layout.'}),
        [chunk(content='Please choose the stall layout.')],
    ])
    assert len(calls) == 5
    assert store.run_state(chat_id)['status'] == 'needs_input'
    assert 'already started' in next(m['content'] for m in store.messages(chat_id) if m.get('tool_call_id') == 'answer')


def test_restart_keeps_continuation_count_and_does_not_accept_an_old_final_marker(monkeypatch, store, entry):
    chat = store.create_chat(budget={'status': 'set', 'amount_cents': 20000})
    run = agent.Run(chat['id'], store=store, entry_id=entry['id'], options={'permissions': 'full'},
                    budget=store.get_chat(chat['id'])['budget'], steps=34, continuations=3)
    store.add_message(chat['id'], {'role': 'user', 'content': 'Build a barn'})
    store.add_message(chat['id'], {'role': 'assistant', 'content': 'Work remains.', '_final_answer': True, '_turn_id': run.turn_id})
    run.record('running')
    fake, calls = scripted([[chunk(content='Still unfinished.')]])
    monkeypatch.setattr(agent.litellm, 'acompletion', fake)
    async def recover():
        await agent.recover(store)
        await agent._runs[chat['id']].task
    asyncio.run(recover())
    state = store.run_state(chat['id'])
    assert len(calls) == 1
    assert state['status'] == 'blocked'
    assert state['continuations'] == 3 and state['steps'] == 35
    assert state['budget']['maximum_cents'] == 21000


def test_claude_adapter_uses_the_same_completion_guard(monkeypatch, store, entry):
    import claude_agent
    calls = []
    async def connected(*args):
        return True
    async def claude(store, run, entry, save, execute, prompt, use_tools):
        calls.append(1)
        run.steps += 1
        save({'role': 'assistant', 'content': 'Work remains.'})
    monkeypatch.setattr(agent.browser_auth, 'connected', connected)
    monkeypatch.setattr(claude_agent, 'run_claude', claude)
    entry = {**entry, 'auth_mode': 'browser', 'litellm_params': {'model': 'anthropic/claude-sonnet-4-6'}}
    monkeypatch.setattr(agent.llm_config, 'get', lambda _: entry)
    chat_id, _ = play(monkeypatch, store, entry, [])
    assert len(calls) == 4
    assert store.run_state(chat_id)['status'] == 'blocked'


def test_setup_failure_retains_request_and_budget_without_calling_ai(monkeypatch, store, entry):
    async def unavailable(run):
        raise ValueError('The LEGO parts index could not initialize. Your draft is saved.')
    monkeypatch.setattr(agent, 'prepare_workspace', unavailable)
    chat_id, calls = play(monkeypatch, store, entry, [], budget={'status': 'set', 'amount_cents': 20000})
    assert not calls and not store.models(chat_id)
    assert store.run_state(chat_id)['budget']['amount_cents'] == 20000
    assert store.run_state(chat_id)['status'] == 'error'
    assert 'parts index' in store.messages(chat_id)[-1]['content']
