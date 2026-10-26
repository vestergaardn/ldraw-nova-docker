"""Tools the agents can call. Schemas are OpenAI function-calling format,
which LiteLLM translates for every provider.

Where things go:
  /data/generated/        finished models (flat) + their .png snapshots: the My Models page
  /data/output/<chat>/    the chat's work folder: scripts' cwd, notes, plans, scratch files
  /data/chats/<chat>/renders/   extra renders shown in the chat
"""
from __future__ import annotations

import inspect
import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import sandbox
import settings
import toolkit
import environment_config
import gallery
import budgets
from leocad_render import bom_path_for, list_models, snapshot_path_for
from paths import safe_join
from store import ChatStore

MAX_WRITE_BYTES = 2 * 1024 * 1024


class ToolError(Exception):
    """An error message meant for the agent (it can fix its call and retry)."""


@dataclass
class ToolContext:
    chat_id: str
    store: ChatStore
    emit: Callable[[str, dict], None]
    budget: dict = field(default_factory=budgets.normalize)

    @property
    def work_dir(self) -> Path:
        return self.store.work_dir(self.chat_id)

    @property
    def chat_dir(self) -> Path:
        return self.store.chat_dir(self.chat_id)


@dataclass
class ToolResult:
    content: str                                          # what the LLM sees
    models: list[dict] = field(default_factory=list)      # model references added to the chat
    images: list[Path] = field(default_factory=list)      # PNGs to show (and send to vision models)
    outcome: dict | None = None                         # explicit non-publication turn outcome


# --- helpers ---------------------------------------------------------------

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "model"


def _next_version(slug: str) -> int:
    pattern = re.compile(rf"{re.escape(slug)}-v(\d+)\.(mpd|ldr|dat)", re.IGNORECASE)
    versions = [int(m.group(1)) for p in list_models(settings.GENERATED_DIR) if (m := pattern.fullmatch(p.name))]
    return max(versions, default=0) + 1


def resolve_path(ctx: ToolContext, path: str, *, write: bool = False) -> Path:
    """Paths the agent may use. Reading: the model collection and its own work
    folder. Writing (write_file): only its work folder.
    Relative paths are relative to the work folder."""
    path = (path or "").strip()
    roots = [ctx.work_dir] if write else [ctx.work_dir, settings.GENERATED_DIR, settings.TOOLKIT_DIR]
    if any(p.startswith(".") for p in Path(path).parts if p not in (".", "..")):
        raise ToolError("Hidden configuration and runtime files are not accessible through file tools")
    if path.startswith("output/") or path == "output":
        path = str(ctx.work_dir / path.removeprefix("output").lstrip("/"))
    elif not write and not path.startswith("/") and (settings.TOOLKIT_DIR / path).exists():
        path = str(settings.TOOLKIT_DIR / path)
    if path.startswith("/"):
        for root in roots:
            if path == str(root) or path.startswith(str(root) + "/"):
                resolved = safe_join(root, path[len(str(root)):])
                break
        else:
            raise ToolError(f"{path} is outside the allowed folders: {', '.join(map(str, roots))}")
    else:
        resolved = safe_join(ctx.work_dir, path)
    if resolved is None:
        raise ToolError(f"{path} escapes the allowed folders")
    return resolved


async def _run_and_collect(ctx: ToolContext, argv: list[str], timeout: int) -> ToolResult:
    result = await run_command(ctx, argv, timeout)
    return ToolResult(result.as_text())


async def run_command(ctx: ToolContext, argv: list[str], timeout: int) -> sandbox.RunResult:
    env = toolkit.environment()
    key = environment_config.snapshot().get("TYPESAFE_API_KEY", "")
    if key:
        env["TYPESAFE_API_KEY"] = key
    return await sandbox.run(argv, toolkit.workspace(ctx.store, ctx.chat_id), timeout=max(1, min(timeout, 1800)),
                             env_extra=env, secrets=(key,),
                             on_output=lambda channel, text: ctx.emit("tool_output", {"channel": channel, "delta": text}))


async def t_run_python(ctx: ToolContext, code: str, timeout: int = 60) -> ToolResult:
    scripts = ctx.work_dir / "generators"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.py"
    script.write_text(code)
    sandbox.give_to_agent(scripts)
    sandbox.give_to_agent(script)
    result = await _run_and_collect(ctx, ["python3", str(script)], timeout)
    result.content += f"\nSaved generator: {artifact_url(ctx, script)}"
    return result


async def t_run_shell(ctx: ToolContext, command: str, timeout: int = 60) -> ToolResult:
    return await _run_and_collect(ctx, ["bash", "-o", "pipefail", "-c", command], timeout)


async def t_run_toolkit(ctx: ToolContext, arguments: list[str], timeout: int = 300) -> ToolResult:
    if not arguments or not all(isinstance(a, str) and "\0" not in a for a in arguments):
        raise ToolError("arguments must be a nonempty array of CLI argument strings")
    return await _run_and_collect(ctx, ["./ldraw-agent", *arguments], timeout)


async def t_report_progress(ctx: ToolContext, summary: str) -> ToolResult:
    summary = summary.strip()[:2000]
    ctx.emit("progress", {"summary": summary})
    return ToolResult(summary)


async def t_finish_request(ctx: ToolContext, status: str, reason: str, next_step: str) -> ToolResult:
    if status not in {'answered', 'blocked', 'needs_input'}:
        raise ToolError('Use answered, blocked or needs_input. A completed build requires publish_model.')
    if any(not isinstance(value, str) or not value.strip() or len(value) > 2000 for value in (reason, next_step)):
        raise ToolError('Provide a specific reason and next step, each between 1 and 2000 characters.')
    outcome = dict(status=status, reason=reason.strip(), next_step=next_step.strip())
    return ToolResult(json.dumps(outcome) + '\nNow explain this outcome to the user. No model was approved by this tool.',
                      outcome=outcome)


async def t_view_image(ctx: ToolContext, path: str) -> ToolResult:
    source = resolve_path(ctx, path)
    if not source.is_file() or source.stat().st_size > 16 * 1024 * 1024:
        raise ToolError("Choose an existing PNG smaller than 16 MB")
    data = source.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ToolError("view_image accepts PNG renders")
    target = ctx.chat_dir / "renders" / (hashlib.sha256(data).hexdigest()[:24] + ".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return ToolResult(f"Opened {path} for visual review.", images=[target])


def artifact_url(ctx: ToolContext, path: Path) -> str:
    return f"/api/chats/{ctx.chat_id}/artifacts/" + quote(path.relative_to(ctx.work_dir).as_posix())


async def _trusted_review(ctx, arguments, timeout):
    """Only fixed toolkit review commands, in a server-owned directory."""
    env = toolkit.server_environment()
    import publications
    cwd = settings.DATA_DIR / '.publication-review'
    cwd.mkdir(exist_ok=True)
    publications._owned(cwd, 0o755)
    return await sandbox.run([str(toolkit.root() / 'ldraw-agent'), *arguments],
                             cwd, timeout,
                             env_extra=env, trusted=True,
                             on_output=lambda channel, text: ctx.emit('tool_output', {'channel': channel, 'delta': text}))


def _draft_snapshot(ctx, path):
    import publications
    source = resolve_path(ctx, path, write=True)
    if not source.is_file() or source.suffix.lower() not in {'.mpd', '.ldr'}:
        raise ToolError('Choose a self-contained .mpd or .ldr from this chat’s output folder')
    if source.stat().st_size > 32 * 1024 * 1024:
        raise ToolError('Model exceeds the 32 MB publication limit')
    return source, publications.capture(source.read_bytes())


def _stock_result(ctx, result):
    report = ctx.work_dir / 'stock-check.json'
    report.write_text(json.dumps(result, indent=2))
    sandbox.give_to_agent(report)
    result['report'] = artifact_url(ctx, report)
    return ToolResult(json.dumps(result, indent=2))


async def t_check_build_parts(ctx: ToolContext, path: str, refresh: bool = False) -> ToolResult:
    from purchasing import availability
    ctx.emit('progress', {'summary': 'Checking stock for the draft. The draft remains saved.'})
    try:
        _, snapshot = _draft_snapshot(ctx, path)
        quote_data, problems = await availability.check(snapshot, refresh=refresh)
        result = _build_feedback(ctx, quote_data, problems)
        if problems:
            from purchasing import discovery
            result['available_alternatives'] = await asyncio.to_thread(discovery.alternatives, problems)
        return _stock_result(ctx, result)
    except Exception as exc:
        return _stock_result(ctx, {'status': 'blocked', 'published': False,
                                  'reason': str(exc) if isinstance(exc, (ValueError, ToolError)) else 'The parts service is unavailable. Resume this saved draft later.',
                                  'next_action': 'Repair the draft or retry check_build_parts. Do not deliver a model download.'})


def _build_feedback(ctx, quote, problems):
    from purchasing import availability
    assessment = budgets.assess(ctx.budget, quote)
    budget_problems = budgets.problems(assessment)
    result = availability.feedback(quote, [*problems, *budget_problems])
    result['budget_assessment'] = assessment
    guidance = budgets.design_guidance(assessment)
    if guidance:
        result['budget_guidance'] = guidance
        if not problems and not budget_problems:
            result['next_action'] = guidance['next_action']
    if budget_problems:
        ctx.emit('progress', {'summary': 'Adjusting the build to fit the parts budget.'})
        result['next_action'] = ('Compare the largest costs, search cheaper available colours or parts, and redesign expensive assemblies. '
                                 'Check the repaired draft again. Preserve the requested concept and defining features.')
    return result


async def t_search_available_parts(ctx: ToolContext, query: str, quantity: int = 1,
                                   color: str | None = None, limit: int = 12, max_unit_price: int | None = None) -> ToolResult:
    from purchasing import discovery
    ctx.emit('progress', {'summary': 'Finding verified parts and colours in LEGO’s US catalogue.'})
    try:
        result = await asyncio.to_thread(discovery.search, query, quantity=quantity, color=color, limit=limit,
                                        max_unit_price=max_unit_price)
        return ToolResult(json.dumps(result, indent=2))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'The parts catalogue is unavailable. Keep the draft and retry later.'
        return ToolResult(json.dumps({'status': 'blocked', 'reason': reason, 'results': []}))


async def t_publish_model(ctx: ToolContext, path: str, name: str | None = None) -> ToolResult:
    import publications
    from purchasing import availability
    from purchasing.domain import publication_problems
    source, snapshot = _draft_snapshot(ctx, path)
    revision = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    # A cheap preliminary check prevents rendering an already unpurchasable draft.
    ctx.emit('progress', {'summary': 'Checking stock before final model review.'})
    try:
        quote_data, problems = await availability.check(snapshot)
        if problems:
            quote_data, problems = await availability.check(snapshot, refresh=True)
        if problems:
            ctx.emit('progress', {'summary': 'Replacing unavailable parts. The draft needs changes before completion.'})
            return _stock_result(ctx, {'published': False, **_build_feedback(ctx, quote_data, problems)})
        feedback = _build_feedback(ctx, quote_data, [])
        if feedback['problem_count']:
            return _stock_result(ctx, {'published': False, **feedback})
        review = snapshot.parent / 'review-v1'
        review.mkdir(exist_ok=True)
        publications._owned(review, 0o755)
        report, image, bom = review / 'validation.json', review / 'home.png', review / 'leocad-bom.csv'
        complete = review / 'complete.json'
        if not complete.is_file() or not all(p.is_file() for p in (report, image, bom)):
            ctx.emit('progress', {'summary': 'Checking geometry and rendering the final model.'})
            validation = await _trusted_review(ctx, ['validate', str(snapshot), '--geometry', '--detail', 'summary', '--report', str(report)], 1800)
            if validation.exit_code != 0 or not report.is_file():
                return _stock_result(ctx, {'status': 'blocked', 'published': False, 'issue': 'geometry_validation',
                                          'reason': validation.as_text(), 'next_action': 'Repair the geometry, review the draft, then publish again.'})
            import render
            async with render._rendering(snapshot):
                rendered = await _trusted_review(ctx, ['render', str(snapshot), '--outdir', str(review), '--views', 'home'], 600)
            if rendered.exit_code != 0 or not image.is_file() or not bom.is_file():
                return _stock_result(ctx, {'status': 'blocked', 'published': False, 'issue': 'render_failed',
                                          'reason': rendered.as_text(), 'next_action': 'The draft is saved. Retry publication after fixing the render.'})
            complete.write_text(json.dumps({'revision': revision, 'review_version': 1}))
        ctx.emit('progress', {'summary': 'Checking current LEGO stock after rendering.'})
        after = time.time()
        quote_data, problems = await availability.check(snapshot, refresh=True, final=True)
        problems = problems or publication_problems(quote_data, after=after)
        feedback = _build_feedback(ctx, quote_data, problems)
        if feedback['problem_count']:
            ctx.emit('progress', {'summary': 'Prices or stock changed. Repairing the draft before completion.'})
            return _stock_result(ctx, {'published': False, **feedback})
        target, receipt = await publications.commit(snapshot, _slug(name or source.stem), quote_data, after=after, budget=ctx.budget)
    except Exception as exc:
        # Do not expose connection strings or provider errors to the agent/browser.
        reason = str(exc) if isinstance(exc, (ValueError, ToolError)) else 'The parts service is unavailable. The draft and completed renders are saved.'
        ctx.emit('progress', {'summary': 'Publication paused. The draft is saved for repair or retry.'})
        return _stock_result(ctx, {'status': 'blocked', 'published': False, 'reason': reason,
                                  'next_action': 'Retry this draft when the service responds. Do not deliver a model download.'})
    ctx.emit('progress', {'summary': 'Completing the model. Every required part passed the US stock check.'})
    shutil.copyfile(image, snapshot_path_for(target))
    shutil.copyfile(bom, bom_path_for(target))
    # Copies of reports are convenient working artifacts, never approval evidence.
    public_review = ctx.work_dir / 'publication' / revision[:16]
    public_review.mkdir(parents=True, exist_ok=True)
    sandbox.give_to_agent(public_review)
    for file in (report, image, bom):
        shutil.copyfile(file, public_review / file.name)
    warnings = ['Stock is not reserved. Physical buildability is not certified.']
    ref = ctx.store.add_model(ctx.chat_id, name or source.stem, target, warnings)
    ctx.emit('model', {'id': ref['id'], 'name': ref['name']})
    model_url = '/files/generated/' + quote(target.name)
    result = {'published': True, 'status': 'available', 'model_url': model_url,
              'card_url': f"/chat/{ctx.chat_id}#model-{ref['id']}",
              'viewer_url': '/viewer/viewer.html?model=' + quote(model_url, safe=''),
              'download_url': model_url + '?download=1', 'sha256': revision,
              'build_availability': {'status': 'available', **receipt},
              'verification_quote': quote_data['id'], 'coverage': 1.0,
              'budget_assessment': receipt['budget_assessment'],
              'validation': artifact_url(ctx, public_review / report.name), 'checks_passed': True,
              'validation_path': str(public_review / report.name),
              'preview': artifact_url(ctx, public_review / image.name), 'preview_path': str(public_review / image.name),
              'bom': artifact_url(ctx, public_review / bom.name), 'bom_path': str(public_review / bom.name),
              'physical_validity': 'not_proven', 'warnings': warnings,
              'note': 'Describe any colour, size or construction changes made for availability. No cart action occurred.'}
    return ToolResult(json.dumps(result, indent=2), models=[ref])


async def t_list_files(ctx: ToolContext, path: str = "") -> ToolResult:
    folder = resolve_path(ctx, path) if path else toolkit.workspace(ctx.store, ctx.chat_id)
    if not folder.is_dir():
        raise ToolError(f"{path or folder} is not a folder")
    entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = [f"{p.name}/" if p.is_dir() else f"{p.name}\t{p.stat().st_size} bytes" for p in entries[:500]]
    if len(entries) > 500:
        lines.append(f"... and {len(entries) - 500} more")
    return ToolResult(f"{folder}:\n" + ("\n".join(lines) or "(empty)"))


async def t_read_file(ctx: ToolContext, path: str, max_chars: int = 40_000, offset: int = 0) -> ToolResult:
    file = resolve_path(ctx, path)
    if not file.is_file():
        raise ToolError(f"{path} is not a file")
    if file.stat().st_size > 8 * 1024 * 1024:
        raise ToolError("File exceeds 8 MB; inspect it with a bounded toolkit or shell command")
    data = file.read_bytes()
    if b"\0" in data[:4096]:
        raise ToolError(f"{path} is a binary file ({len(data)} bytes)")
    text = data.decode(errors="replace")
    limit = max(1000, min(max_chars, 200_000))
    total = len(text)
    offset = max(0, offset)
    text = text[offset:offset + limit]
    if offset + limit < total:
        text += f"\n... [truncated: {total} characters total; continue at offset {offset + limit}]"
    return ToolResult(text)


async def t_write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> ToolResult:
    file = resolve_path(ctx, path, write=True)
    if len(content.encode()) > MAX_WRITE_BYTES:
        raise ToolError(f"content is larger than {MAX_WRITE_BYTES} bytes")
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a" if append else "w", encoding="utf-8") as fh:
        fh.write(content)
    for owned in [file, *file.parents]:            # the agent's own scripts must be able to edit them
        if owned == ctx.work_dir or ctx.work_dir not in owned.parents:
            break
        sandbox.give_to_agent(owned)
    return ToolResult(f"{'Appended to' if append else 'Wrote'} {file} ({file.stat().st_size} bytes)")


# --- registry --------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TOOLS: dict[str, tuple[dict, Callable[..., Awaitable[ToolResult]]]] = {
    "finish_request": (_fn("finish_request",
        "Record why a request ends without publishing a model. Use answered ONLY for a purely informational "
        "question before starting build commands or writing files. Use needs_input for a necessary user decision, "
        "or blocked for an actual service failure, denied permission or exhausted design constraint. "
        "Provide specific evidence and a continuation step. Temporary setup delays and unfinished work are NOT "
        "completion: continue building after recovery. Save draft notes first. This tool cannot approve a model.",
        {"status": {"type": "string", "enum": ["answered", "blocked", "needs_input"]},
         "reason": {"type": "string"}, "next_step": {"type": "string"}},
        ["status", "reason", "next_step"]), t_finish_request),
    "search_available_parts": (_fn("search_available_parts",
        "Find construction parts ONLY among verified, available US LEGO Pick a Brick elements. "
        "Returns exact LDraw part and colour codes, USD prices, quantity limits, and observation times. "
        "TypeSafe and its keyword fallback rank the same restricted pool. Use before choosing parts. "
        "Library search and example models do not prove shop availability.",
        {"query": {"type": "string"}, "quantity": {"type": "integer", "description": "Required quantity, default 1"},
         "color": {"type": "string", "description": "Optional exact LDraw colour code"},
         "max_unit_price": {"type": "integer", "description": "Optional maximum price per piece in whole USD cents, before ranking"},
         "limit": {"type": "integer", "description": "Result count, 1 to 40, default 12"}},
        ["query"]), t_search_available_parts),
    "check_build_parts": (_fn("check_build_parts",
        "Check a saved draft’s complete physical parts and colours against the US LEGO catalogue. "
        "Use after substantial assemblies and before rendering. Repair every reported problem. "
        "Returns the parts subtotal, remaining budget, excess cost, largest cost contributors and priced-upgrade guidance. "
        "For budgeted builds, use the remaining allowance for useful improvements toward the permitted maximum. "
        "A passing draft check does not publish or reserve stock; publish_model checks again after rendering.",
        {"path": {"type": "string"}, "refresh": {"type": "boolean", "description": "Refresh selected elements from LEGO; default false uses current catalogue observations"}},
        ["path"]), t_check_build_parts),
    "run_toolkit": (_fn("run_toolkit",
        "Run the standalone model-building CLI. All commands are available: doctor, spec, discover, catalog, "
        "study, extract, examples, build, validate, inspect, render, compare-bom, manual, vehicle, spaceship, "
        "technic, mechanism and more. Read instructions.md and relevant docs first. Output goes under output/. "
        "Arguments are an array, without shell quoting or the executable. Use --help to inspect subcommands.",
        {"arguments": {"type": "array", "items": {"type": "string"}},
         "timeout": {"type": "integer", "description": "Seconds, default 300, maximum 1800"}},
        ["arguments"]), t_run_toolkit),
    "publish_model": (_fn("publish_model",
        "Publish an output MPD revision as an interactive model card in this chat and the My Models collection. "
        "Publishes only the exact snapshot with passing geometry checks and freshly verified US LEGO stock. "
        "Failures return repair instructions and no model card. Use after draft checks and final visual review. "
        "Use the returned card_url for interactive links, not the raw model_url. "
        "Return links to plans, generator, manifests, reports and reviews alongside the card.",
        {"path": {"type": "string"}, "name": {"type": "string"}}, ["path"]), t_publish_model),
    "view_image": (_fn("view_image", "Open a PNG from the toolkit examples or output for actual visual review. "
        "The image is shown to you and the user. Use on rendered views before recording a visual review.",
        {"path": {"type": "string"}}, ["path"]), t_view_image),
    "report_progress": (_fn("report_progress", "Show a concise progress update: current phase, design decisions, "
        "completed checks, remaining work. Use throughout substantial builds, without private internal reasoning.",
        {"summary": {"type": "string"}}, ["summary"]), t_report_progress),
    "run_python": (_fn("run_python", "Run Python with the model builder virtualenv (pyldraw3, numpy, jsonschema). "
        "Working directory has the toolkit repository layout. Use its builder/serializer; write under output/. "
        "Code is saved for reproducibility. Call publish_model for user-visible model cards.",
        {"code": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 60, max 1800"}},
        ["code"]), t_run_python),
    "run_shell": (_fn("run_shell", "Run bash in the toolkit workspace, with pipefail and live output. "
        "./ldraw-agent, ./check-model.sh, ./prepare-glb.sh and global CLIs are available. "
        "TYPESAFE_API_KEY comes from Settings; never print it. Write under output/. "
        "Call publish_model to show a generated model in the chat.",
        {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "Seconds, default 60, max 1800"}},
        ["command"]), t_run_shell),
    "list_files": (_fn("list_files", "List toolkit resources or this chat's output/ (default: repository root).",
        {"path": {"type": "string"}}, []), t_list_files),
    "read_file": (_fn("read_file", "Read toolkit instructions, documentation, examples or your output files. "
        "Use offset to continue a truncated file. Query PDFs through run_toolkit spec.",
        {"path": {"type": "string"}, "max_chars": {"type": "integer"}, "offset": {"type": "integer"}},
        ["path"]), t_read_file),
    "write_file": (_fn("write_file", "Write a plan, generator, notes or report in output/. "
        "Bare relative paths also resolve under output/. Shared toolkit resources cannot be changed.",
        {"path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
        ["path", "content"]), t_write_file),
}

TOOL_SCHEMAS = [schema for schema, _fn_ in TOOLS.values()]


async def dispatch(ctx: ToolContext, name: str, arguments: str | dict) -> ToolResult:
    if name not in TOOLS:
        return ToolResult(f"Error: unknown tool {name!r}. Available: {', '.join(TOOLS)}")
    try:
        args = json.loads(arguments or "{}") if isinstance(arguments, str) else dict(arguments or {})
    except json.JSONDecodeError as exc:
        return ToolResult(f"Error: tool arguments are not valid JSON ({exc}). Call {name} again with valid JSON.")
    _schema, fn = TOOLS[name]
    try:
        inspect.signature(fn).bind(ctx, **args)
    except TypeError as exc:
        return ToolResult(f"Error: bad arguments for {name}: {exc}")
    try:
        return await fn(ctx, **args)
    except ToolError as exc:
        return ToolResult(f"Error: {exc}")
    except Exception as exc:  # noqa: BLE001 - a failing tool must not end the turn
        return ToolResult(f"Error: {name} failed: {exc.__class__.__name__}: {exc}")
