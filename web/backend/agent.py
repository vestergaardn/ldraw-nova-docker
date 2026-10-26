"""Provider-agnostic agent loop on top of LiteLLM.

A turn runs as a background asyncio task per chat (so reloading the page
doesn't kill it). The chat's files (store.py) are the source of truth; live
events only carry what's in flight (streaming text, running tools) plus
"saved" pings telling the UI to refetch.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

import litellm

import booklets
import budgets
import llm_config
import browser_auth
import inference
import model_catalog
import settings
import toolkit
from store import ChatStore
from tools import TOOL_SCHEMAS, ToolContext, ToolResult, dispatch

log = logging.getLogger("agent")

litellm.drop_params = False           # unsupported settings must fail visibly
litellm.suppress_debug_info = True

MAX_STEPS = 150
KEEP_IMAGE_MESSAGES = 2               # only the newest renders are re-sent to vision models
WORK_LISTING_LIMIT = 60               # files of the work folder listed in the system prompt
MAX_RECOVERIES = 3                   # stop a repeatedly crashing turn, rather than retry forever
MAX_CONTINUATIONS = 3                # unfinished final replies do not silently end builds


async def _local_checkpoint() -> None:
    """Local files are durable; Modal installs a volume commit callback."""


checkpoint: Callable[[], Awaitable[None]] = _local_checkpoint
_shutting_down = False


@dataclass
class Run:
    chat_id: str
    task: Optional[asyncio.Task] = None
    draft: str = ""
    tools_running: dict[str, dict] = field(default_factory=dict)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    options: dict = field(default_factory=dict)
    approvals: dict[str, tuple[dict, asyncio.Future]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    phase: str = "Preparing the build workspace"
    store: Optional[ChatStore] = None
    turn_id: str = field(default_factory=lambda: secrets.token_hex(16))
    entry_id: Optional[str] = None
    steps: int = 0
    recoveries: int = 0
    stop_reason: Optional[str] = None
    starting: bool = False
    budget: dict = field(default_factory=budgets.normalize)
    continuations: int = 0
    outcome: dict | None = None
    build_work: bool = False

    def record(self, status: str) -> None:
        if self.store is not None:
            self.store.save_run(self.chat_id, {
                "turn_id": self.turn_id, "status": status, "llm_model_id": self.entry_id,
                "options": self.options, "started_at": self.started_at,
                "steps": self.steps, "recoveries": self.recoveries,
                "budget": self.budget,
                "continuations": self.continuations, "outcome": self.outcome, "build_work": self.build_work,
            })

    def emit(self, event: str, data: dict) -> None:
        self.last_event_at = time.time()
        if event == "progress":
            self.phase = data["summary"]
        elif event == "tool_start":
            data.setdefault("started_at", time.time())
            self.phase = {
                "run_toolkit": "Running the model builder",
                "run_python": "Running the model generator",
                "run_shell": "Running a build command",
                "publish_model": "Checking and publishing the model",
                "view_image": "Reviewing a rendered image",
                "read_file": "Reading build instructions or reports",
                "write_file": "Saving build files",
                "list_files": "Inspecting available files",
            }.get(data.get("name"), self.phase)
        elif event == "tool_output":
            tool = self.tools_running.get(data.get("id"))
            if tool is not None:
                tool["output"] = (tool.get("output", "") + data.get("delta", ""))[-12000:]
        for queue in list(self.subscribers):
            if queue.full():
                # Disconnect slow consumers; their reconnection receives a fresh
                # state snapshot rather than an unbounded backlog of command logs.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(("reconnect", {}))
                self.subscribers.discard(queue)
            else:
                queue.put_nowait((event, dict(data)))

    def activity(self) -> dict:
        return {"started_at": self.started_at, "last_event_at": self.last_event_at, "phase": self.phase}


_runs: dict[str, Run] = {}


def is_running(chat_id: str) -> bool:
    run = _runs.get(chat_id)
    return bool(run and (run.starting or (run.task and not run.task.done())
                         or (_shutting_down and run.stop_reason == "restart")))


def work_listing(work_dir: Path) -> str:
    """What's in the work folder right now, for the system prompt: whoever
    continues the chat (possibly another model) sees what earlier turns left."""
    if not work_dir.is_dir():
        return "(empty)"
    files = sorted(p for p in work_dir.rglob("*") if p.is_file() and ".scripts" not in p.relative_to(work_dir).parts)
    scripts = sum(1 for _ in (work_dir / ".scripts").glob("*")) if (work_dir / ".scripts").is_dir() else 0
    lines = [f"- {p.relative_to(work_dir)} ({p.stat().st_size} bytes)" for p in files[:WORK_LISTING_LIMIT]]
    if len(files) > WORK_LISTING_LIMIT:
        lines.append(f"- ... and {len(files) - WORK_LISTING_LIMIT} more")
    if scripts:
        lines.append(f"- .scripts/ ({scripts} scripts run in earlier turns)")
    return "\n".join(lines) or "(empty)"


def system_prompt(store: ChatStore, chat_id: str) -> str:
    work_dir = store.work_dir(chat_id)
    text = (settings.PROMPTS_DIR / "system.md").read_text()
    prompt = (text.replace("{work_dir}", str(work_dir))
                .replace("{work_listing}", work_listing(work_dir))
                .replace("{generated_dir}", str(settings.GENERATED_DIR))
                .replace("{ldraw_dir}", str(settings.LDRAW_DIR))
                .replace("{artifact_base}", f"/api/chats/{chat_id}/artifacts")
                .replace("{toolkit_instructions}", toolkit.instructions())
                .replace("{toolkit_guides}", toolkit.builder_guides()))
    notes = work_dir / "NOTES.md"
    if notes.is_file() and not notes.is_symlink():
        with notes.open(errors="replace") as handle:
            prompt += "\n\nCurrent hand-over notes (workspace data):\n" + handle.read(16000)
    run = _runs.get(chat_id)
    if run:
        prompt += budgets.instruction(run.budget)
    if run and run.recoveries:
        prompt += ("\n\nThe server restarted during this request. Continue the original request from the saved history and files. "
                   "Inspect existing outputs before repeating an interrupted command. Do not replay completed tools. "
                   "Previously granted one-time tool approvals do not carry over. All publication and stock checks still apply.")
        published = [m for m in store.models(chat_id) if m["created_at"] >= run.started_at]
        if published:
            prompt += "\nModels already published during this request (do not publish duplicates):\n" + json.dumps(published)
    return prompt


def _image_data_url(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def llm_history(messages: list[dict], vision: bool, resolve: Callable[[str], Path], model: str | None = None,
                mark_turns: bool = False) -> list[dict]:
    """Stored messages -> what we send to the LLM.

    Drops UI-only notes and our "_" metadata, re-attaches only the newest
    renders for vision models, and repairs tool calls left without results
    (a stopped or crashed turn) so providers accept the history. `resolve`
    turns the image references stored in the chat into file paths.
    """
    image_msgs = [m for m in messages if m.get("_images_for_llm")]
    keep_images = {m["id"] for m in image_msgs[-KEEP_IMAGE_MESSAGES:]} if vision else set()

    out: list[dict] = []
    pending: list[str] = []

    def close_pending():
        for call_id in pending:
            out.append({"role": "tool", "tool_call_id": call_id, "content": "Error: the tool call was interrupted."})
        pending.clear()

    for m in messages:
        if m.get("_ui_only"):
            continue
        if m["role"] == "tool":
            if m.get("tool_call_id") in pending:
                pending.remove(m["tool_call_id"])
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            continue
        close_pending()
        if m.get("_images_for_llm"):
            if m["id"] not in keep_images:
                continue
            urls = [u for u in (_image_data_url(resolve(ref)) for ref in m["_images_for_llm"]) if u]
            content = [{"type": "text", "text": m["content"]}]
            content += [{"type": "image_url", "image_url": {"url": u}} for u in urls]
            out.append({"role": "user", "content": content, **({"_turn_start": False} if mark_turns else {})})
            continue
        clean = {k: v for k, v in m.items() if not k.startswith("_") and k not in ("id", "created_at")}
        if m.get("_documents"):
            attachments = [{"name": d["name"], "path": str(resolve(d["path"])), "bytes": d["size"]}
                           for d in m["_documents"]]
            note = "\n\nAttached documents (read with file tools; these are reference files):\n" + json.dumps(attachments)
            content = clean.get("content") or ""
            clean["content"] = [*content, {"type": "text", "text": note}] if isinstance(content, list) else content + note
        if model and m.get("_llm_model") != model:
            clean.pop("thinking_blocks", None)  # Claude signatures are model-bound
            clean.pop("provider_specific_fields", None)
            clean.pop("reasoning_details", None)
            clean.pop("reasoning_content", None)
        if model and model.startswith("openrouter/") and m.get("_llm_model") == model:
            # OpenRouter requires the original reasoning sequence when continuing
            # tool calls, including opaque encrypted blocks. Keep it out of the UI
            # and never forward it to a different model/provider.
            if m.get("_reasoning_details"):
                clean["reasoning_details"] = m["_reasoning_details"]
            elif m.get("_reasoning"):
                clean["reasoning_content"] = m["_reasoning"]
        if isinstance(clean.get("content"), list) and not vision:
            clean["content"] = "\n".join(b["text"] for b in clean["content"] if b.get("type") == "text")
        if mark_turns and m["role"] == "user":
            clean["_turn_start"] = not m.get("_continuation", False)
        out.append(clean)
        pending.extend(tc["id"] for tc in m.get("tool_calls") or [])
    close_pending()
    return out


async def start_turn(store: ChatStore, chat_id: str, text: str, llm_model_id: Optional[str],
                     options: dict | None = None, images: list[str] | None = None, documents: list | None = None,
                     budget=budgets.UNSET) -> None:
    if _shutting_down:
        raise RuntimeError("The server is restarting. Try again shortly.")
    if is_running(chat_id):
        raise RuntimeError("this chat is already running a turn")
    entry = llm_config.get(llm_model_id) if llm_model_id else None
    if entry is None:
        _entries, default_id = llm_config.builder_entries()
        entry = llm_config.get(default_id) if default_id else None
    if entry is None:
        raise ValueError("no LLM model configured — add one in Settings")
    if (not model_catalog.builder_supported(entry["litellm_params"]["model"])
            or any(value is False for value in llm_config.capabilities(entry).values())):
        raise ValueError("Choose a model with tool calling and image input support in Settings")

    options = model_catalog.validate_options(entry, options)
    if images and llm_config.capabilities(entry)["vision"] is not True:
        raise ValueError("Select a model with image input support")
    if entry.get("auth_mode") == "browser":
        provider = "anthropic" if entry["litellm_params"]["model"].startswith("anthropic/") else "openai"
        if not await browser_auth.connected(provider):
            raise ValueError("Sign in to the provider in Settings first")
    # A concurrent request may have started while browser auth status was checked.
    if _shutting_down:
        raise RuntimeError("The server is restarting. Try again shortly.")
    if is_running(chat_id):
        raise RuntimeError("this chat is already running a turn")

    chat = store.get_chat(chat_id)
    selected_budget = budgets.normalize(chat.get('budget') if budget is budgets.UNSET else budget)
    if selected_budget['status'] == 'pending' and options['mode'] == 'agent' and options['permissions'] != 'read_only':
        # Old clients have no budget dialog; omission retains their unlimited behavior.
        selected_budget = budgets.normalize()
    if chat["title"] == "New chat":
        title = " ".join(text.split())
        store.update_chat(chat_id, title=title[:60] + ("…" if len(title) > 60 else ""))
    store.update_chat(chat_id, llm_model_id=entry["id"], options=options, budget=selected_budget)
    content = ([{"type": "text", "text": text}, *[{"type": "image_url", "image_url": {"url": u}} for u in images]]
               if images else text)
    from attachments import save_documents
    attached = save_documents(store, chat_id, documents or [])
    store.add_message(chat_id, {"role": "user", "content": content, "_budget": selected_budget,
                               **({"_documents": attached} if attached else {})})

    run = Run(chat_id, store=store, entry_id=entry["id"], budget=selected_budget)
    _runs[chat_id] = run
    run.draft, run.tools_running = "", {}
    run.options, run.approvals = options, {}
    run.started_at = run.last_event_at = time.time()
    run.phase = "Preparing the build workspace"
    await _launch(run, entry)


async def _launch(run: Run, entry: dict) -> None:
    run.starting = True
    try:
        run.record("running")
        # Persist the original request and recovery marker before accepting work.
        await checkpoint()
        if run.stop_reason is None:
            run.task = asyncio.create_task(_run_turn(run.store, run, entry))
    except Exception:
        try:
            run.record("error")
        except OSError:
            log.warning("could not save recovery status for chat %s", run.chat_id)
        raise RuntimeError("The build could not be saved. Try again shortly.") from None
    finally:
        run.starting = False


async def recover(store: ChatStore) -> None:
    """Resume only explicitly recorded interrupted turns on this singleton worker."""
    global _shutting_down
    _shutting_down = False
    for chat in store.list_chats():
        chat_id = chat["id"]
        state = store.run_state(chat_id)
        if not state or state.get("status") not in {"running", "interrupted"} or is_running(chat_id):
            continue
        try:
            # Never silently switch the original request to another model/account.
            entry = llm_config.get(state["llm_model_id"])
            if entry is None:
                raise ValueError("The original AI model is no longer configured. Choose a model and send a message to continue.")
            options = model_catalog.validate_options(entry, state["options"])
            run = Run(chat_id, store=store, turn_id=state["turn_id"], entry_id=entry["id"], options=options,
                      started_at=state["started_at"], steps=state.get("steps", 0),
                      recoveries=state.get("recoveries", 0) + 1, budget=budgets.normalize(state.get('budget')),
                      continuations=state.get('continuations', 0), outcome=state.get('outcome'),
                      build_work=state.get('build_work', False))
            messages = [m for m in store.messages(chat_id) if m.get("_turn_id") == run.turn_id
                        and not m.get("_ui_only") and not m.get("_partial")]
            if messages and messages[-1].get("_final_answer") and completion_status(run):
                # The final answer reached disk just before the process died.
                run.record(completion_status(run))
                await prepare_guide(store, chat_id, run.started_at)
                continue
            if run.recoveries > MAX_RECOVERIES:
                raise ValueError("The build paused after repeated server interruptions. Send a message to continue from the saved work.")
            if run.steps >= MAX_STEPS:
                raise ValueError(f"Stopped after {MAX_STEPS} steps. {budgets.continuation(run.budget)}")
            _runs[chat_id] = run
            store.add_message(chat_id, {"role": "assistant", "_ui_only": True, "_notice": True,
                                       "content": "The server restarted. Resuming your saved build."})
            log.info("resuming chat %s turn %s after restart (%s/%s)", chat_id, run.turn_id, run.recoveries, MAX_RECOVERIES)
            await _launch(run, entry)
        except Exception as exc:
            store.save_run(chat_id, {**state, "status": "error"})
            message = str(exc) if isinstance(exc, ValueError) else "Recovery could not start. Your draft is saved. Send a message to continue."
            store.add_message(chat_id, {"role": "assistant", "_ui_only": True, "_error": True, "content": message})
            log.warning("could not recover chat %s: %s", chat_id, type(exc).__name__)


async def shutdown() -> None:
    global _shutting_down
    _shutting_down = True
    # Stop tools before closing the services they use. A restart is not a user Stop.
    await asyncio.gather(*(cancel(chat_id, reason="restart") for chat_id in list(_runs)))
    await checkpoint()


async def cancel(chat_id: str, *, reason: str = "user") -> bool:
    run = _runs.get(chat_id)
    if not run or not is_running(chat_id):
        return False
    # An explicit Stop always wins over a simultaneous server restart.
    if run.stop_reason != "user":
        run.stop_reason = reason
    run.record("stopped" if run.stop_reason == "user" else "interrupted")
    if run.task:
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
    await checkpoint()
    return True


async def subscribe(chat_id: str) -> AsyncIterator[tuple[str, dict]]:
    run = _runs.get(chat_id)
    if run and _shutting_down and run.stop_reason == "restart":
        yield "snapshot", {"running": True, "draft": "", "tools": [], "approvals": [],
                           "activity": {**run.activity(), "phase": "The server is restarting. Your saved build will resume."}}
        return  # EventSource reconnects to the replacement server after EOF.
    if run is None or not is_running(chat_id):
        yield "snapshot", {"running": False, "draft": "", "tools": []}
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    run.subscribers.add(queue)
    try:
        yield "snapshot", {"running": True, "draft": run.draft, "tools": list(run.tools_running.values()),
                           "approvals": [a for a, _ in run.approvals.values()], "activity": run.activity()}
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                yield "activity", run.activity()
                continue
            yield event, data
            if event in {"done", "reconnect"}:
                return
    finally:
        run.subscribers.discard(queue)


async def prepare_guide(store: ChatStore, chat_id: str, since: float) -> None:
    """Start the assembly guide for the turn's final published model."""
    try:
        models = [store.resolve(chat_id, ref["model"]) for ref in store.models(chat_id) if ref["created_at"] >= since]
        if models and models[-1].is_file():
            await booklets.prepare(models[-1])
    except Exception:  # noqa: BLE001 - the guide can still be created on request
        log.exception("could not prepare the assembly guide for chat %s", chat_id)


def requires_model(options: dict) -> bool:
    return options.get('mode', 'agent') == 'agent' and options.get('permissions') != 'read_only'


def completion_status(run: Run) -> str | None:
    if not requires_model(run.options):
        return 'completed'
    if run.outcome:
        return run.outcome['status']
    # Only a publication made during THIS request can complete a new build/edit.
    # Model records and publication receipts are server-owned, unlike work files.
    import publications
    for model in run.store.models(run.chat_id):
        if model['created_at'] >= run.started_at:
            path = run.store.resolve(run.chat_id, model['model'])
            record = publications.record_for(path)
            if record and record.get('enforced'):
                return 'completed'
    return None


async def prepare_workspace(run: Run) -> None:
    run.emit('progress', {'summary': 'Preparing the LEGO parts index. Saved work will continue when it is ready.'})
    await toolkit.wait_ready()


async def continue_unfinished(run: Run, save) -> bool:
    if run.continuations >= MAX_CONTINUATIONS:
        run.outcome = dict(status='blocked', reason='The AI repeatedly ended before publishing a model.',
                           next_step=budgets.continuation(run.budget))
        save({'role': 'assistant', '_ui_only': True, '_notice': True,
              'content': 'The build paused after three unsuccessful continuation attempts. '
                         'No new model was completed. Your request and any draft files are saved. '
                         + budgets.continuation(run.budget)})
        return False
    run.continuations += 1
    run.record('running')
    save({'role': 'user', '_hidden': True, '_continuation': True,
          'content': 'The requested build has no verified publication from this request. '
                     'Continue from the saved files and completed tools; preserve the requested concept and defining features. '
                     'Complete construction, review, availability and budget checks, then use publish_model. '
                     'A recovered setup delay is not a reason to stop. If a real blocker prevents further work, '
                     'save NOTES.md and use finish_request with its specific reason and next step. '
                     'For a purely informational question, use finish_request(status="answered"). '
                     'All original permissions, budget and remaining work limits still apply.'})
    await checkpoint()
    run.emit('progress', {'summary': 'The build is unfinished. Continuing from the saved work.'})
    return True


async def _run_turn(store: ChatStore, run: Run, entry: dict) -> None:
    chat_id = run.chat_id
    run.store = store
    caps = llm_config.capabilities(entry)
    vision = caps["vision"] is True
    use_tools = caps["tools"] is not False and run.options.get("mode") != "chat"
    ctx = ToolContext(chat_id=chat_id, store=store, emit=run.emit, budget=run.budget)

    def save(message: dict) -> int:
        msg_id = store.add_message(chat_id, {**message, "_turn_id": run.turn_id})
        run.emit("saved", {"message_id": msg_id})
        return msg_id

    stopped = False
    status = "completed"
    try:
        if use_tools and requires_model(run.options):
            await prepare_workspace(run)
        if entry.get("auth_mode") == "browser" and entry["litellm_params"]["model"].startswith("anthropic/"):
            from claude_agent import run_claude
            while run.steps < MAX_STEPS:
                await run_claude(store, run, entry, save, execute_tool, system_prompt(store, chat_id), use_tools)
                status = completion_status(run)
                if status:
                    break
                if run.steps >= MAX_STEPS:
                    continue
                if not await continue_unfinished(run, save):
                    status = 'blocked'
                    break
            else:
                status = 'limited'
                save({'role': 'assistant', '_ui_only': True, '_notice': True,
                      'content': f'Stopped after {MAX_STEPS} steps. {budgets.continuation(run.budget)}'})
            return
        params = await inference.params_for(entry, run.options)
        for _step in range(run.steps, MAX_STEPS):
            run.steps = _step + 1
            run.record("running")
            await checkpoint()
            prompt = system_prompt(store, chat_id) + mode_prompt(run.options)
            messages = [{"role": "system", "content": prompt},
                        *llm_history(store.messages(chat_id), vision, lambda ref: store.resolve(chat_id, ref), entry["litellm_params"]["model"], mark_turns=True)]
            budget = run.options.get("context_tokens") or model_catalog.profile(entry["litellm_params"]["model"])["context_window"]
            messages, removed = inference.bounded_history(messages, params["model"], budget)
            if removed:
                run.emit("context", {"removed": removed})
            kwargs = {"tools": available_tools(run.options)} if use_tools else {}
            run.emit("progress", {"summary": "Agent is reviewing the request and choosing the next step."})
            stream = await litellm.acompletion(**params, messages=messages, stream=True, num_retries=2, **kwargs)
            inference.preserve_openrouter_reasoning_chunks(stream, params["model"])

            run.draft = ""
            chunks = []
            async for chunk in stream:
                chunks.append(chunk)
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is not None and getattr(delta, "content", None):
                    run.draft += delta.content
                    run.emit("text", {"delta": delta.content})

            full = litellm.stream_chunk_builder(chunks, messages=messages)
            reply = full.choices[0].message
            tool_calls = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                for tc in (reply.tool_calls or [])
            ]
            # null (not "") content next to tool calls: some providers reject empty text blocks.
            message: dict = {"role": "assistant", "content": reply.content or (None if tool_calls else "")}
            message["_llm_model"] = entry["litellm_params"]["model"]
            if tool_calls:
                message["tool_calls"] = tool_calls
            elif completion_status(run):
                message["_final_answer"] = True
            if getattr(reply, "thinking_blocks", None):          # must round-trip for Anthropic thinking
                message["thinking_blocks"] = reply.thinking_blocks
            if getattr(reply, "provider_specific_fields", None):
                message["provider_specific_fields"] = reply.provider_specific_fields
            if getattr(reply, "reasoning_content", None):
                message["_reasoning"] = reply.reasoning_content
            if message["_llm_model"].startswith("openrouter/"):
                # LiteLLM 1.102.1 retains delta.reasoning_details but loses them
                # in stream_chunk_builder. OpenRouter specifies concatenation in
                # received order; do not merge/reorder blocks by id or index.
                details = [detail for chunk in chunks if chunk.choices
                           for detail in (getattr(chunk.choices[0].delta, "reasoning_details", None) or [])]
                if details:
                    message["_reasoning_details"] = details
            if message['content'] or tool_calls:
                save(message)
            run.draft = ""
            if not tool_calls:
                status = completion_status(run)
                if status:
                    break
                if run.steps >= MAX_STEPS:
                    continue
                if not await continue_unfinished(run, save):
                    status = 'blocked'
                    break
                continue

            await checkpoint()

            images: list[str] = []                             # relative to the chat folder
            for call in tool_calls:
                name = call["function"]["name"]
                run.tools_running[call["id"]] = {"id": call["id"], "name": name,
                                                 "arguments": call["function"]["arguments"]}
                run.emit("tool_start", run.tools_running[call["id"]])
                result = await execute_tool(run, ctx, call["id"], name, call["function"]["arguments"])
                run.tools_running.pop(call["id"], None)
                refs = [store.ref(chat_id, png) for png in result.images]
                save({"role": "tool", "tool_call_id": call["id"], "name": name, "content": result.content,
                      "_models": [m["id"] for m in result.models], "_images": refs})
                await checkpoint()
                run.emit("tool_end", {"id": call["id"], "name": name})
                images += refs
            if images and vision:
                save({"role": "user", "content": "Renders produced by the tool calls above:",
                      "_images_for_llm": images, "_hidden": True})
        else:
            status = "limited"
            save({"role": "assistant", "_ui_only": True, "_notice": True,
                  "content": f"Stopped after {MAX_STEPS} steps. {budgets.continuation(run.budget)}"})
    except asyncio.CancelledError:
        stopped = True
        status = "stopped" if run.stop_reason == "user" else "interrupted"
        if run.draft:
            save({"role": "assistant", "content": run.draft, "_partial": True})
        save({"role": "assistant", "_ui_only": True, "_notice": True,
              "content": "Stopped." if status == "stopped" else "The server is restarting. Your saved build will resume automatically."})
        raise
    except Exception as exc:  # noqa: BLE001 - provider/auth/network errors end the turn, not the chat
        status = "error"
        log.warning("turn failed for chat %s: %s", chat_id, exc.__class__.__name__)
        message = str(exc) if isinstance(exc, ValueError) else f"{exc.__class__.__name__}: Provider request failed. Check login, model access and settings."
        save({"role": "assistant", "_ui_only": True, "_error": True, "content": message})
        run.emit("turn_error", {"message": message})   # not "error": EventSource reserves it
    finally:
        run.record(status)
        run.draft, run.tools_running = "", {}
        for _, future in run.approvals.values():
            if not future.done():
                future.cancel()
        run.approvals.clear()
        # Queue the guide before "done" so the reloaded card already shows it.
        if not stopped:
            await prepare_guide(store, chat_id, run.started_at)
        await checkpoint()
        run.emit("reconnect" if status == "interrupted" else "done", {})


READ_TOOLS = {"list_files", "read_file", "view_image", "report_progress", "search_available_parts", "check_build_parts", "finish_request"}


def mode_prompt(options: dict) -> str:
    if options.get("mode") == "plan":
        return "\nPLAN MODE: inspect with read-only tools and propose a plan. Do not execute commands or change files."
    if options.get("mode") == "chat":
        return "\nCHAT MODE: answer without using tools."
    return ""


def available_tools(options: dict) -> list[dict]:
    if options.get("mode") == "chat":
        return []
    if options.get("mode") == "plan" or options.get("permissions") == "read_only":
        return [t for t in TOOL_SCHEMAS if t["function"]["name"] in READ_TOOLS]
    return TOOL_SCHEMAS


async def execute_tool(run: Run, ctx: ToolContext, call_id: str, name: str, arguments: str) -> ToolResult:
    allowed = {t["function"]["name"] for t in available_tools(run.options)}
    if name not in allowed:
        return ToolResult("Tool denied by the current mode or permissions.")
    if name not in READ_TOOLS and run.options.get("permissions", "ask") == "ask":
        # Independent approval IDs prevent replay between turns or duplicate provider call IDs.
        import uuid
        approval_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        info = {"id": approval_id, "call_id": call_id, "name": name, "arguments": arguments}
        run.approvals[approval_id] = (info, future)
        run.emit("approval", info)
        try:
            approved = await asyncio.wait_for(future, 600)
        except asyncio.TimeoutError:
            approved = False
        finally:
            run.approvals.pop(approval_id, None)
            run.emit("approval_resolved", {"id": approval_id})
        if not approved:
            run.outcome = dict(status='needs_input', reason='A required tool was declined or its approval expired.',
                               next_step='Send a new instruction to continue with an approved action.')
            run.record('running')
            return ToolResult("Tool denied by the user or approval timed out. Do not retry without a new user instruction.")
    scoped = replace(ctx, emit=lambda event, data: run.emit(event, {**data, "id": call_id} if event == "tool_output" else data))
    if name not in READ_TOOLS:
        run.build_work = True
        run.outcome = None  # A new action invalidates any earlier finish declaration.
        run.record('running')
    result = await dispatch(scoped, name, arguments)
    if result.outcome:
        if result.outcome['status'] == 'answered' and run.build_work:
            return ToolResult('A build action already started. Continue to publish_model, or report a specific blocker with finish_request.')
        run.outcome = result.outcome
        run.record('running')
    return result


def decide(chat_id: str, approval_id: str, approved: bool) -> bool:
    run = _runs.get(chat_id)
    pending = run.approvals.get(approval_id) if run else None
    if not pending or pending[1].done():
        return False
    pending[1].set_result(approved)
    return True
