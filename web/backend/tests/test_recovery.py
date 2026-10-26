"""Exercise the same application shutdown/startup boundary used by Modal."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import agent
import llm_config
import main
from store import ChatStore
from test_agent import chunk, scripted, tool_call_chunks


@pytest.fixture
def recovery_app(monkeypatch, tmp_path):
    store = ChatStore(tmp_path / "chats", tmp_path / "output")
    entry = llm_config.create({"model_name": "recovery-test", "litellm_params": {
        "model": "openai/gpt-6-luna", "api_key": "test"},
        "capabilities": {"tools": True, "vision": True}})

    async def nothing(*args):
        pass

    monkeypatch.setattr(agent, "_runs", {})
    monkeypatch.setattr(main, "get_store", lambda: store)
    monkeypatch.setattr(main.publications, "initialize", lambda: None)
    monkeypatch.setattr(main.pricing, "initialize", lambda: None)
    monkeypatch.setattr(main.pricing, "shutdown", nothing)
    monkeypatch.setattr(main.booklets, "shutdown", nothing)
    monkeypatch.setattr(main.browser_auth, "shutdown", nothing)
    monkeypatch.setattr(agent, "system_prompt", lambda *args: "Build the requested model.")
    monkeypatch.setattr(agent, "prepare_guide", nothing)
    return store, entry


def test_application_restart_continues_saved_turn_without_another_user_message(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    requests = []
    waiting = asyncio.Event()

    async def completion(**kwargs):
        requests.append(kwargs)

        async def stream():
            if len(requests) == 1:
                waiting.set()
                await asyncio.Future()
            if len(requests) == 2:
                for part in tool_call_chunks('pause', 'finish_request', {'status': 'blocked',
                        'reason': 'The stock service is unavailable.', 'next_step': 'Continue after recovery.'}):
                    yield part
                return
            yield chunk(content="Recovered the saved build.")
        return stream()

    monkeypatch.setattr(agent.litellm, "acompletion", completion)

    async def check():
        async with main.lifespan(main.app):
            await agent.start_turn(store, chat_id, "Build a harbour crane", entry["id"], {"permissions": "full"})
            await asyncio.wait_for(waiting.wait(), 2)
        # A replacement process has only the persistent chat/work folders.
        agent._runs.clear()
        replacement = ChatStore(store.chats_dir, store.output_dir)
        monkeypatch.setattr(main, "get_store", lambda: replacement)
        async with main.lifespan(main.app):
            assert agent.is_running(chat_id), "The server forgot the interrupted build"
            await asyncio.wait_for(agent._runs[chat_id].task, 2)

    asyncio.run(check())
    assert len(requests) == 3
    messages = store.messages(chat_id)
    assert sum(m["role"] == "user" for m in messages) == 1
    assert messages[-1]["content"] == "Recovered the saved build."
    assert not any(m.get("content") == "Stopped." for m in messages)
    assert store.run_state(chat_id)["status"] == "blocked"
    assert store.run_state(chat_id)["steps"] == 3
    assert store.run_state(chat_id)["recoveries"] == 1


def test_user_stop_is_not_recovered(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    waiting = asyncio.Event()
    requests = []

    async def completion(**kwargs):
        requests.append(kwargs)
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(agent.litellm, "acompletion", completion)

    async def check():
        async with main.lifespan(main.app):
            await agent.start_turn(store, chat_id, "Build a crane", entry["id"])
            await asyncio.wait_for(waiting.wait(), 2)
            assert await agent.cancel(chat_id)
        agent._runs.clear()
        async with main.lifespan(main.app):
            assert not agent.is_running(chat_id)
    asyncio.run(check())
    assert len(requests) == 1
    assert store.run_state(chat_id)["status"] == "stopped"
    assert store.messages(chat_id)[-1]["content"] == "Stopped."


def test_abrupt_process_death_recovers_durable_request(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    script = '''
import asyncio, os, sys
from pathlib import Path
import agent
from store import ChatStore
store = ChatStore(Path(sys.argv[1]), Path(sys.argv[2]))
agent.system_prompt = lambda *args: "Build the model."
async def ready(*args): pass
agent.prepare_workspace = ready
async def die(**kwargs):
    os._exit(33)  # No cancellation handler, shutdown hook or finally block.
agent.litellm.acompletion = die
async def run():
    await agent.start_turn(store, sys.argv[3], "Build a crane", sys.argv[4], {"permissions": "full"})
    await agent._runs[sys.argv[3]].task
asyncio.run(run())
'''
    result = subprocess.run([sys.executable, "-c", script, str(store.chats_dir), str(store.output_dir),
                             chat_id, entry["id"]], cwd=Path(__file__).resolve().parents[1], timeout=30, capture_output=True)
    assert result.returncode == 33, result.stderr.decode()
    assert store.run_state(chat_id)["status"] == "running"
    fake, requests = scripted([
        tool_call_chunks('pause', 'finish_request', {'status': 'blocked', 'reason': 'The stock service is unavailable.',
                                                   'next_step': 'Continue after recovery.'}),
        [chunk(content="Recovered after process death.")]])
    monkeypatch.setattr(agent.litellm, "acompletion", fake)

    async def check():
        async with main.lifespan(main.app):
            await asyncio.wait_for(agent._runs[chat_id].task, 2)
    asyncio.run(check())
    assert len(requests) == 2
    assert store.messages(chat_id)[-1]["content"] == "Recovered after process death."
    assert store.run_state(chat_id)["steps"] == 3


@pytest.mark.parametrize("status", [None, "stopped", "completed", "error", "limited"])
def test_startup_leaves_old_and_finished_chats_alone(monkeypatch, recovery_app, status):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    store.add_message(chat_id, {"role": "user", "content": "An older request"})
    if status:
        agent.Run(chat_id, store=store, entry_id=entry["id"]).record(status)

    async def forbidden(**kwargs):
        raise AssertionError("Must not restart this chat")

    monkeypatch.setattr(agent.litellm, "acompletion", forbidden)

    async def check():
        async with main.lifespan(main.app):
            assert not agent.is_running(chat_id)
    asyncio.run(check())
    assert len(store.messages(chat_id)) == 1


def test_recovery_does_not_duplicate_completed_tool_or_user_request(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    run = agent.Run(chat_id, store=store, entry_id=entry["id"], steps=2,
                    options={"mode": "agent", "permissions": "read_only", "effort": "medium"})
    store.add_message(chat_id, {"role": "user", "content": "Build a crane"})
    for call_id in ["finished", "interrupted"]:
        store.add_message(chat_id, {"role": "assistant", "content": None, "_turn_id": run.turn_id,
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"NOTES.md"}'}}]})
        if call_id == "finished":
            store.add_message(chat_id, {"role": "tool", "tool_call_id": call_id, "content": "Saved design", "_turn_id": run.turn_id})
    run.record("running")
    requests = []
    waiting, finish = asyncio.Event(), asyncio.Event()

    async def completion(**kwargs):
        requests.append(kwargs)
        waiting.set()
        await finish.wait()

        async def stream():
            yield chunk(content="Continued from saved tools.")
        return stream()
    monkeypatch.setattr(agent.litellm, "acompletion", completion)

    async def check():
        await agent.recover(store)
        await asyncio.wait_for(waiting.wait(), 2)
        await agent.recover(store)  # A second startup/recovery call cannot start another task.
        assert len(requests) == 1
        assert agent._runs[chat_id].options["permissions"] == "read_only"
        finish.set()
        await agent._runs[chat_id].task
    asyncio.run(check())
    tools = [m for m in requests[0]["messages"] if m["role"] == "tool"]
    assert tools[0]["content"] == "Saved design"
    assert tools[1]["tool_call_id"] == "interrupted" and "interrupted" in tools[1]["content"]
    assert len([m for m in store.messages(chat_id) if m["role"] == "user"]) == 1
    assert all(t["function"]["name"] in agent.READ_TOOLS for t in requests[0]["tools"])


@pytest.mark.parametrize("reason", ["repeated_restarts", "step_limit", "missing_model", "final_answer"])
def test_recovery_bounds_and_completed_answer_reconciliation(monkeypatch, recovery_app, reason):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    run = agent.Run(chat_id, store=store, entry_id=entry["id"], options={"permissions": "full"})
    if reason == "repeated_restarts":
        run.recoveries = agent.MAX_RECOVERIES
    elif reason == "step_limit":
        run.steps = agent.MAX_STEPS
    elif reason == "missing_model":
        run.entry_id = "deleted-model"
    else:
        run.outcome = dict(status='answered', reason='An informational question was answered.', next_step='Choose a model.')
        store.add_message(chat_id, {"role": "assistant", "content": "Already delivered.",
                                   "_turn_id": run.turn_id, "_final_answer": True})
    run.record("running")
    requests = []
    async def forbidden(**kwargs):
        requests.append(kwargs)
        raise AssertionError("Must not make an AI call")
    monkeypatch.setattr(agent.litellm, "acompletion", forbidden)
    asyncio.run(agent.recover(store))
    assert not requests
    assert not agent.is_running(chat_id)
    assert store.run_state(chat_id)["status"] == ("answered" if reason == "final_answer" else "error")


def test_checkpoint_failure_does_not_start_unsaved_work(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    async def failed():
        raise OSError("storage offline")
    monkeypatch.setattr(agent, "checkpoint", failed)
    async def check():
        with pytest.raises(RuntimeError, match="could not be saved"):
            await agent.start_turn(store, chat_id, "Build a crane", entry["id"])
        assert not agent.is_running(chat_id)
    asyncio.run(check())
    assert store.run_state(chat_id)["status"] == "error"


def test_stop_during_initial_save_prevents_execution(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    saving, finish = asyncio.Event(), asyncio.Event()
    requests = []
    async def checkpoint():
        saving.set()
        await finish.wait()
    async def forbidden(**kwargs):
        requests.append(kwargs)
        raise AssertionError("The stopped request must not start")
    monkeypatch.setattr(agent, "checkpoint", checkpoint)
    monkeypatch.setattr(agent.litellm, "acompletion", forbidden)
    async def check():
        task = asyncio.create_task(agent.start_turn(store, chat_id, "Build a crane", entry["id"]))
        await saving.wait()
        stop = asyncio.create_task(agent.cancel(chat_id))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(task, stop)
        assert not agent.is_running(chat_id)
    asyncio.run(check())
    assert not requests
    assert store.run_state(chat_id)["status"] == "stopped"


def test_restart_stream_keeps_client_connected_for_recovery(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    run = agent.Run(chat_id, store=store, entry_id=entry["id"], stop_reason="restart")
    monkeypatch.setattr(agent, "_shutting_down", True)
    agent._runs[chat_id] = run
    async def check():
        return [event async for event in agent.subscribe(chat_id)]
    events = asyncio.run(check())
    assert len(events) == 1
    event, data = events[0]
    assert event == "snapshot" and data["running"] is True
    assert "restarting" in data["activity"]["phase"]


def test_recovery_record_is_private(monkeypatch, recovery_app):
    import settings
    from fastapi.testclient import TestClient
    store, entry = recovery_app
    monkeypatch.setattr(settings, "DATA_DIR", store.chats_dir.parent)
    monkeypatch.setattr(settings, "CHATS_DIR", store.chats_dir)
    monkeypatch.setattr(main, "file_url", lambda file: "/private-test")
    chat_id = store.create_chat()["id"]
    agent.Run(chat_id, store=store, entry_id=entry["id"]).record("completed")
    assert (store.chat_dir(chat_id) / "run.json").stat().st_mode & 0o077 == 0
    client = TestClient(main.app)
    assert client.get(f"/files/chats/{chat_id}/run.json").status_code == 404


def test_restart_never_reuses_a_pending_approval(monkeypatch, recovery_app):
    store, entry = recovery_app
    chat_id = store.create_chat()["id"]
    fake, requests = scripted([
        tool_call_chunks("first", "run_shell", {"command": "touch must-not-exist"}),
        tool_call_chunks("retry", "run_shell", {"command": "touch must-not-exist"}),
        [chunk(content="The command was declined.")],
    ])
    monkeypatch.setattr(agent.litellm, "acompletion", fake)

    async def pending():
        async with asyncio.timeout(3):
            while not agent._runs[chat_id].approvals:
                await asyncio.sleep(0.01)
        return next(iter(agent._runs[chat_id].approvals))

    async def check():
        async with main.lifespan(main.app):
            await agent.start_turn(store, chat_id, "Inspect first", entry["id"], {"permissions": "ask"})
            original = await pending()
        agent._runs.clear()
        async with main.lifespan(main.app):
            new = await pending()
            assert new != original
            assert agent.decide(chat_id, original, True) is False
            assert agent.decide(chat_id, new, False)
            await agent._runs[chat_id].task
    asyncio.run(check())
    assert len(requests) == 3
    assert not (store.work_dir(chat_id) / "must-not-exist").exists()
