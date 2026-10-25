"""FastAPI app: chat API + SSE, LLM settings, file/library routes, 3D viewer and SPA.

Run (the image's default CMD):
    uvicorn main:app --app-dir /app/web/backend --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, Literal
from urllib.parse import quote, unquote, urlsplit

import environment_config

environment_config.initialize()

import litellm
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import agent
import booklets
import gallery
import glb
import llm_config
import model_catalog
import model_discovery
import browser_auth
import inference
import render
import publications
from purchasing import service as pricing
from purchasing.database import Database as PartsDatabase, DatabaseUnavailable
from purchasing.domain import import_csv, reviewed_items
from attachments import validate_documents, validate_images
import sandbox
import settings
from leocad_render import MODEL_SUFFIXES, bom_path_for, snapshot_path_for
from paths import rel_to, safe_join
from store import ChatStore, get_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for folder in (settings.GENERATED_DIR, settings.CHATS_DIR, settings.OUTPUT_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(publications.initialize)
    store = get_store()
    pricing.initialize()
    await agent.recover(store)
    try:
        yield
    finally:
        await agent.shutdown()
        await pricing.shutdown()
        await booklets.shutdown()
        await browser_auth.shutdown()


app = FastAPI(title="LDraw Nova agent chat", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, exc: RequestValidationError):
    # Pydantic includes submitted values by default, including credentials.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()
    ]})


@app.middleware("http")
async def same_origin_api(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and urlsplit(origin).netloc != request.headers.get("host")
        ):
            return JSONResponse({"detail": "Cross-origin API requests are not allowed"}, status_code=403)
        try:
            size = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        if size > 18 * 1024 * 1024:
            return JSONResponse({"detail": "Request exceeds 18 MB"}, status_code=413)
    response = await call_next(request)
    if request.url.path.startswith(("/api/environment", "/api/quotes", "/api/cart-intents")) or request.url.path.endswith("/edit"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _not_found(what: str = "not found"):
    raise HTTPException(status_code=404, detail=what)


# --- environment overrides --------------------------------------------------

@app.get("/api/environment")
def environment_get():
    return {"variables": environment_config.public()}


@app.get("/api/environment/check")
def environment_check(name: str = "", exclude_id: str = ""):
    return environment_config.check_name(name, exclude_id)


@app.put("/api/environment")
async def environment_save(request: Request):
    # Validate without echoing secret input values in Pydantic error responses.
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected environment variables as JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("variables"), list):
        raise HTTPException(400, "Expected a list of environment variables")
    try:
        return {"variables": environment_config.save(body["variables"])}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


# --- LLM models --------------------------------------------------------------

class LlmEntry(BaseModel):
    model_name: Optional[str] = None
    litellm_params: dict[str, Any]
    capabilities: Optional[dict[str, Any]] = None
    auth_mode: str = "api_key"


@app.get("/api/model-catalog")
def model_catalog_list():
    return {"models": [model_catalog.profile(m["model"]) for m in model_catalog.CATALOG]}


@app.get("/api/auth/{provider}")
async def auth_status(provider: str):
    try:
        return await browser_auth.status(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class LoginRequest(BaseModel):
    flow: str = "browser"
    restart: bool = False


@app.post("/api/auth/{provider}/login")
async def auth_login(provider: str, body: Optional[LoginRequest] = None):
    try:
        return await browser_auth.start(provider, body.flow if body else "browser", body.restart if body else False)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class AuthCode(BaseModel):
    code: str = Field(max_length=4096)


@app.post("/api/auth/{provider}/code")
async def auth_code(provider: str, body: AuthCode):
    try:
        await browser_auth.submit_code(provider, body.code)
        return {"submitted": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.delete("/api/auth/{provider}")
async def auth_disconnect(provider: str):
    if any(agent.is_running(chat_id) for chat_id in agent._runs):
        raise HTTPException(409, "Stop active turns before disconnecting a provider")
    try:
        await browser_auth.disconnect(provider)
        return {"status": "disconnected"}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/api/llm-models")
def llm_models_list():
    entries, default_id = llm_config.builder_entries()
    return {"models": [llm_config.public(e) for e in entries], "default_id": default_id}


@app.get("/api/llm-models/{entry_id}/edit")
def llm_models_edit(entry_id: str):
    entry = llm_config.get(entry_id) or _not_found()
    return llm_config.public(entry)


@app.post("/api/llm-models")
def llm_models_create(entry: LlmEntry):
    try:
        return llm_config.public(llm_config.create(entry.model_dump()))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.put("/api/llm-models/{entry_id}")
def llm_models_update(entry_id: str, entry: LlmEntry):
    try:
        updated = llm_config.update(entry_id, entry.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return llm_config.public(updated) if updated else _not_found()


@app.delete("/api/llm-models/{entry_id}")
def llm_models_delete(entry_id: str):
    return {"deleted": llm_config.delete(entry_id)}


@app.post("/api/llm-models/{entry_id}/default")
def llm_models_default(entry_id: str):
    llm_config.set_default(entry_id)
    return {"default_id": llm_config.list_entries()[1]}


@app.post("/api/llm-models/{entry_id}/test")
async def llm_models_test(entry_id: str):
    entry = llm_config.get(entry_id) or _not_found()
    fingerprint = llm_config.connection_fingerprint(entry)
    discovery = asyncio.create_task(model_discovery.discover(entry))
    async def tested(result):
        metadata = await discovery
        result["connection_status"] = llm_config.record_connection_test(entry_id, fingerprint, result["ok"], metadata)
        result["profile"] = model_catalog.entry_profile(llm_config.get(entry_id) or entry)
        return result
    try:
        if entry.get("auth_mode") == "browser" and entry["litellm_params"]["model"].startswith("anthropic/"):
            ready = await browser_auth.connected("anthropic")
            return await tested({"ok": ready, "reply": "Claude login is ready. Send a chat to verify model access." if ready else None,
                           "error": None if ready else "Sign in to Claude in Settings first"})
        params = await inference.params_for(entry, {})
        params.setdefault("max_tokens", 4096)
        params.setdefault("timeout", 60)
        response = await litellm.acompletion(
            **params,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        )
        return await tested({"ok": True, "reply": "Connection successful.",
                       "capabilities": llm_config.capabilities(entry)})
    except Exception as exc:  # noqa: BLE001 - shown to the user as the test result
        return await tested({"ok": False, "error": f"{exc.__class__.__name__}: Check credentials, model access and settings."})


@app.get("/api/llm-models/export", response_class=PlainTextResponse)
def llm_models_export():
    entries, _default = llm_config.builder_entries()
    model_list = [{"model_name": e["model_name"],
                   "litellm_params": llm_config.public(e)["litellm_params"],
                   "capabilities": e["capabilities"], "auth_mode": e.get("auth_mode", "api_key")} for e in entries]
    return yaml.safe_dump({"model_list": model_list}, sort_keys=False, allow_unicode=True)


class ImportBody(BaseModel):
    yaml: str


@app.post("/api/llm-models/import")
def llm_models_import(body: ImportBody):
    try:
        data = yaml.safe_load(body.yaml) or {}
        model_list = data["model_list"] if isinstance(data, dict) else data
        imported = llm_config.import_model_list(model_list)
    except (yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"not a LiteLLM model_list: {exc}") from None
    return {"imported": [llm_config.public(e) for e in imported]}


@app.get("/api/llm-providers")
def llm_providers():
    names = sorted({getattr(p, "value", p) for p in litellm.provider_list})
    return {"providers": names}


@app.get("/api/llm-providers/{provider}/models")
def llm_provider_models(provider: str):
    models = litellm.models_by_provider.get(provider, [])
    presets = [m["model"] for m in model_catalog.CATALOG if m["model"].startswith(provider + "/")]
    candidates = {m if m.startswith(provider + "/") else provider + "/" + m for m in models} | set(presets)
    return {"models": sorted(m for m in candidates if model_catalog.builder_supported(m))}


# --- chats -------------------------------------------------------------------

class NewChat(BaseModel):
    llm_model_id: Optional[str] = None


class ChatPatch(BaseModel):
    title: Optional[str] = None
    llm_model_id: Optional[str] = None


class NewMessage(BaseModel):
    text: str = Field(max_length=200_000)
    llm_model_id: Optional[str] = None
    options: dict[str, Any] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list, max_length=4)
    documents: list[dict[str, Any]] = Field(default_factory=list, max_length=4)


# --- model helpers -------------------------------------------------------------

def is_gallery(path: Path) -> bool:
    """A file from the gallery baked into the image."""
    return path.parent.resolve() == settings.GALLERY_MODELS_DIR.resolve()


def file_url(path: Path, versioned: bool = False) -> Optional[str]:
    """URL of an existing file the web UI may load, else None: /files/... in a
    web-visible folder of /data (generated/, chats/), or /gallery-files/... for gallery
    models. data/output is agent-only: never served.

    `versioned` adds the file's mtime, for images that can be re-rendered under
    the same name: browsers reuse an image already on the page by URL alone."""
    if is_gallery(path):
        url = "/gallery-files/" + quote(path.name)
    else:
        try:
            rel = rel_to(path, settings.DATA_DIR)
        except ValueError:
            return None
        if rel.split("/", 1)[0] not in settings.WEB_DIRS:
            return None
        url = "/files/" + quote(rel)
    if not path.is_file():
        return None
    if path.parent.resolve() == settings.GENERATED_DIR.resolve() and not publications.serving_allowed(path):
        return None
    if path.suffix.lower() in MODEL_SUFFIXES and not is_gallery(path) and not publications.approved(path):
        return None
    return f"{url}?v={path.stat().st_mtime_ns}" if versioned else url


def _status(path: Path, kind: str, in_gallery: bool) -> tuple[str, Optional[str]]:
    if not in_gallery:
        return gallery.status_of(path, kind)
    # Gallery models come with their siblings: nothing is made for them.
    sibling = snapshot_path_for(path) if kind == "snapshot" else bom_path_for(path)
    return ("ready", None) if sibling.exists() else ("failed", "none included with this gallery model")


def model_info(path: Path) -> dict:
    """A model file in the collection, as the UI sees it: its snapshot (status,
    image_url), BOM (bom_status, bom_url), part count (from the BOM), notes
    (info_url and info_heading, from its .md), its collection and any assembly
    guide already started with the default options (guide_id)."""
    exists = path.is_file()
    in_gallery = is_gallery(path)
    status, error = _status(path, "snapshot", in_gallery) if exists else ("missing", None)
    bom_status, bom_error = _status(path, "bom", in_gallery) if exists else ("missing", None)
    stat = path.stat() if exists else None
    return {
        "file": path.name, "name": path.stem, "description": gallery.description_of(path) if exists else "",
        "model_url": file_url(path), "image_url": file_url(snapshot_path_for(path), versioned=True),
        "bom_url": file_url(bom_path_for(path), versioned=True), "parts": gallery.part_count(path) if exists else None,
        "info_url": file_url(gallery.info_path_for(path), versioned=True), "gallery": in_gallery,
        "info_heading": gallery.info_heading_of(path) if exists else None,
        "size": stat.st_size if stat else 0, "mtime": stat.st_mtime if stat else 0,
        "status": status, "error": error, "bom_status": bom_status, "bom_error": bom_error,
        "quote": pricing.summary(path) if exists else None,
        "build_availability": publications.availability(path) if exists else None,
        "guide_id": booklets.prepared(path) if exists and not in_gallery else None,
    }


def _pending(info: dict) -> bool:
    return info["status"] in ("queued", "rendering") or info["bom_status"] in ("queued", "rendering")


def chat_models(store: ChatStore, chat_id: str) -> dict[str, dict]:
    """The models a chat produced (references into data/generated), keyed by id."""
    result = {}
    for ref in store.models(chat_id):
        path = store.resolve(chat_id, ref["model"])
        if path.is_file() and not publications.approved(path):
            continue
        result[ref["id"]] = {**model_info(path), "id": ref["id"], "name": ref["name"],
                             "warnings": ref.get("warnings", []), "created_at": ref["created_at"]}
    return result


def model_chat_index(store: ChatStore) -> dict[Path, list[dict]]:
    """model path -> the chats that produced it."""
    index: dict[Path, list[dict]] = {}
    for chat in store.list_chats():
        for ref in store.models(chat["id"]):
            chats = index.setdefault(store.resolve(chat["id"], ref["model"]), [])
            if not any(c["id"] == chat["id"] for c in chats):
                chats.append({"id": chat["id"], "title": chat["title"]})
    return index


# --- chats ---------------------------------------------------------------------

@app.get("/api/chats")
def chats_list():
    store = get_store()
    chats = store.list_chats()
    for chat in chats:
        chat["running"] = agent.is_running(chat["id"])
        chat["models"] = list(chat_models(store, chat["id"]).values())
    return {"chats": chats}


@app.post("/api/chats")
def chats_create(body: NewChat):
    return get_store().create_chat(llm_model_id=body.llm_model_id)


@app.get("/api/chats/{chat_id}")
async def chats_get(chat_id: str):
    store = get_store()
    chat = store.get_chat(chat_id) or _not_found("no such chat")
    messages = store.messages(chat_id)
    for m in messages:
        m.pop("_reasoning_details", None)  # opaque provider state belongs only in server-side history
        if m.get("_images"):
            m["_image_urls"] = [u for u in (file_url(store.resolve(chat_id, r), versioned=True) for r in m["_images"]) if u]
        if m.get("_documents"):
            for document in m["_documents"]:
                path = store.resolve(chat_id, document["path"])
                try:
                    relative = path.relative_to(store.work_dir(chat_id))
                    document["url"] = f"/api/chats/{chat_id}/artifacts/" + quote(relative.as_posix())
                except ValueError:
                    document["url"] = None
    models = chat_models(store, chat_id)
    gallery.ensure_artifacts(store.resolve(chat_id, ref["model"]) for ref in store.models(chat_id)
                             if publications.approved(store.resolve(chat_id, ref["model"])))
    return {"chat": {**chat, "running": agent.is_running(chat_id)}, "messages": messages, "models": models}


@app.patch("/api/chats/{chat_id}")
def chats_patch(chat_id: str, body: ChatPatch):
    store = get_store()
    store.get_chat(chat_id) or _not_found("no such chat")
    store.update_chat(chat_id, **body.model_dump(exclude_none=True))
    return store.get_chat(chat_id)


@app.delete("/api/chats/{chat_id}")
async def chats_delete(chat_id: str):
    """Deletes data/chats/<id> and its work folder data/output/<id>. The models
    it produced stay in data/generated (they're part of the collection)."""
    store = get_store()
    store.get_chat(chat_id) or _not_found("no such chat")
    await agent.cancel(chat_id)
    store.delete_chat(chat_id)
    return {"deleted": True}


@app.post("/api/chats/{chat_id}/messages", status_code=202)
async def chats_send(chat_id: str, body: NewMessage):
    store = get_store()
    store.get_chat(chat_id) or _not_found("no such chat")
    if not body.text.strip():
        raise HTTPException(400, "empty message")
    try:
        images = validate_images(body.images)
        documents = validate_documents(body.documents, images)
        await agent.start_turn(store, chat_id, body.text, body.llm_model_id, body.options, images, documents)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"started": True}


class ApprovalDecision(BaseModel):
    approved: bool


@app.post("/api/chats/{chat_id}/approvals/{approval_id}")
async def chats_approve(chat_id: str, approval_id: str, body: ApprovalDecision):
    if not agent.decide(chat_id, approval_id, body.approved):
        raise HTTPException(409, "This approval is no longer pending")
    return {"accepted": True}


@app.post("/api/chats/{chat_id}/cancel")
async def chats_cancel(chat_id: str):
    return {"cancelled": await agent.cancel(chat_id)}


@app.get("/api/chats/{chat_id}/stream")
async def chats_stream(chat_id: str):
    async def events():
        async for event, data in agent.subscribe(chat_id):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/chats/{chat_id}/artifacts/{path:path}")
def chat_artifact(chat_id: str, path: str):
    """Explicit downloads from one chat's output; never expose runtime/config.

    HTML catalogs are downloadable, but are not executed with app privileges.
    """
    store = get_store()
    store.get_chat(chat_id) or _not_found("no such chat")
    if any(part.startswith(".") for part in Path(path).parts):
        _not_found()
    file = safe_join(store.work_dir(chat_id), path)
    if not file or not file.is_file():
        _not_found()
    if file.stat().st_size > 32 * 1024 * 1024:
        raise HTTPException(413, 'Artifact exceeds the 32 MB download limit')
    data = file.read_bytes()
    if not publications.artifact_allowed(file.name, data):
        raise HTTPException(409, 'This draft has not passed publication. Ask the builder to repair and publish it.')
    import mimetypes
    return Response(data, media_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(file.name),
        "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


# --- model collections (My Models and Gallery) ---------------------------------


@app.get("/api/models")
async def models_list():
    """Every model in data/generated, newest first. Models without a snapshot
    or BOM get them made in the background; poll until
    `pending` is 0."""
    models = [path for path in gallery.collection(settings.GENERATED_DIR) if publications.approved(path)]
    gallery.ensure_artifacts(models)
    index = model_chat_index(get_store())
    items = [{**model_info(path), "chats": index.get(path, [])} for path in models]
    return {"models": items, "pending": sum(1 for i in items if _pending(i))}


@app.get("/api/gallery")
def gallery_list():
    """The bundled gallery, independent of generated models with the same name."""
    return {"models": [model_info(path) for path in gallery.collection(settings.GALLERY_MODELS_DIR)], "pending": 0}


@app.delete("/api/models/{filename}")
async def models_delete(filename: str):
    root = settings.GENERATED_DIR.resolve()
    # Delete only a direct, regular model file. Never follow a link or accept a
    # URL/path supplied by a card, including links into the read-only Gallery.
    model = root / filename
    if (Path(filename).name != filename or filename.startswith(".")
            or model.suffix.lower() not in MODEL_SUFFIXES or model.is_symlink()
            or not model.is_file()):
        _not_found("Model not found")
    if model in gallery.publishing or gallery._current == model or render.is_busy(model) or glb.is_converting(model) or booklets.is_busy(model):
        raise HTTPException(409, "This model is still being processed. Try deleting it again when processing finishes.")
    # Notes/previews are shared if car.mpd and car.ldr both exist: retain those.
    shared = any(p != model and p.stem == model.stem for p in gallery.collection(root))
    artifacts = [] if shared else [model.with_suffix(ext) for ext in (".png", ".csv", ".md", ".glb")]
    targets = [model, *artifacts, *glb.cache_files(model)]
    deleted = []
    for path in targets:
        if path.is_file() and not path.is_symlink():
            path.unlink()
            deleted.append(path.name)
    gallery.forget(model)
    # Keep chat records as history. Their existing missing-model state explains
    # that a model is gone, while the agent's original work files remain intact.
    return {"deleted": deleted}


def model_from_url(url: str) -> Optional[Path]:
    """Resolve generated and gallery viewer URLs, including old /demo/ links."""
    path = unquote(urlsplit(url).path)
    for prefix, root in (("/files/generated/", settings.GENERATED_DIR),
                         ("/gallery-files/", settings.GALLERY_MODELS_DIR),
                         ("/demo/", settings.GALLERY_MODELS_DIR)):
        if path.startswith(prefix):
            model = safe_join(root, path[len(prefix):])
            if (model is not None and model.is_file() and model.parent == root.resolve()
                    and model.suffix.lower() in MODEL_SUFFIXES
                    and (root == settings.GALLERY_MODELS_DIR or publications.approved(model))):
                return model
    return None


@app.get('/api/chrome-helper/download')
def chrome_helper_download():
    import io
    directory=Path('/opt/chrome-helper')
    if not directory.is_dir():directory=Path(__file__).resolve().parents[2]/'chrome-helper'
    if not (directory/'manifest.json').is_file():_not_found('The Chrome helper package is unavailable')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zip:
        for file in directory.iterdir():
            if file.is_file() and file.suffix in {'.json','.js','.mjs','.html','.css','.md'}:
                zip.writestr('lego-nova-helper/'+file.name,file.read_bytes())
    return StreamingResponse(iter([archive.getvalue()]),media_type='application/zip',headers={
        'Content-Disposition':'attachment; filename="lego-nova-chrome-helper.zip"','Cache-Control':'private, no-store'})


@app.get("/api/glb")
async def model_glb(url: str):
    """The model at `url` (as the viewer loads it) as an uncompressed .glb, made
    with mpd2glb. Can take a minute for big models; cached per model version."""
    model = model_from_url(url) or _not_found("not a model in My Models or Gallery")
    try:
        out = await glb.export_glb(model)
    except glb.GlbError as exc:
        raise HTTPException(500, str(exc)) from None
    return FileResponse(out, media_type="model/gltf-binary", filename=model.stem + ".glb",
                        headers={"Cache-Control": "no-cache"})


class QuoteRequest(BaseModel):
    url: str = Field(max_length=2048)
    refresh: bool = False


@app.post('/api/quotes')
async def quote_create(body: QuoteRequest):
    model = model_from_url(body.url) or _not_found('Unknown model')
    try:
        record = publications.record_for(model)
        result = await pricing.quote_model(model, refresh=body.refresh, strict=bool(record and record['enforced']))
        return {**result, 'build_availability': publications.availability(model)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except DatabaseUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception:
        logging.exception('Quote refresh failed')
        raise HTTPException(503, 'Prices could not refresh. Your previous quote is unchanged.') from None


@app.get('/api/quotes/summary')
def quote_summary(url: str):
    model=model_from_url(url) or _not_found('Unknown model')
    return JSONResponse({**(pricing.summary(model) or {}), 'build_availability': publications.availability(model)},headers={'Cache-Control':'no-store'})


@app.get('/api/quotes/latest')
def quote_latest(url: str):
    # The saved quote opens the parts review at once; the dialog refreshes prices only when they are old.
    model=model_from_url(url) or _not_found('Unknown model')
    return JSONResponse({**(pricing.latest(model) or {}), 'build_availability': publications.availability(model)},headers={'Cache-Control':'no-store'})


@app.get('/api/quotes/{quote_id}')
def quote_detail(quote_id: str):
    try:
        return PartsDatabase().quote(quote_id)
    except (ValueError, FileNotFoundError):
        _not_found('Unknown price quote')
    except DatabaseUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@app.get('/api/quotes/{quote_id}/import/{batch}')
def quote_import(quote_id: str, batch: int, available_only: bool = False):
    quote = quote_detail(quote_id)
    try:
        items = reviewed_items(quote, available_only=available_only)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    selected = items[batch * 400:(batch + 1) * 400] if batch >= 0 else []
    if not selected: _not_found('Unknown import batch')
    return PlainTextResponse(import_csv(selected), media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="lego-parts-{batch + 1}.csv"', 'Cache-Control': 'no-store'})


class CartIntentRequest(BaseModel):
    quote_id: str = Field(max_length=36)
    review_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    available_only: bool = False


class CartClaimRequest(BaseModel):
    token: str = Field(min_length=32, max_length=100)
    claim_id: str = Field(min_length=16, max_length=100)


class CartResultRequest(CartClaimRequest):
    result: dict


@app.post('/api/cart-intents', status_code=201)
def cart_intent_create(body: CartIntentRequest):
    try:
        return PartsDatabase().create_intent(body.quote_id, body.review_hash, body.available_only)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, str(exc)) from None
    except DatabaseUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@app.post('/api/cart-intents/{intent_id}/claim')
def cart_intent_claim(intent_id: str, body: CartClaimRequest):
    try:
        return PartsDatabase().claim_intent(intent_id, body.token, body.claim_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except DatabaseUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


@app.post('/api/cart-intents/{intent_id}/result')
def cart_intent_result(intent_id: str, body: CartResultRequest):
    try:
        if len(json.dumps(body.result)) > 128_000:
            raise ValueError('Cart result is too large')
        return PartsDatabase().intent_result(intent_id, body.token, body.claim_id, body.result)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except DatabaseUnavailable as exc:
        raise HTTPException(503, str(exc)) from None


class AssemblyGuideRequest(BaseModel):
    url: str = Field(max_length=2048)
    template: Literal["creator-31134", "city-clear"] = "creator-31134"
    improve: bool = True
    print_format: Literal["native", "letter"] = "native"


@app.post("/api/assembly-guides", status_code=202)
async def assembly_guide_create(body: AssemblyGuideRequest):
    model = model_from_url(body.url) or _not_found("not a model in My Models or Gallery")
    try:
        return await booklets.start(model, template=body.template, improve=body.improve, print_format=body.print_format)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(429, str(exc)) from None


@app.get("/api/assembly-guides/{job_id}")
def assembly_guide_status(job_id: str):
    try:
        result = booklets.status(job_id)
    except (ValueError, FileNotFoundError):
        return _not_found("Unknown assembly guide")
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/assembly-guides/{job_id}/pdf")
def assembly_guide_pdf(job_id: str):
    try:
        result = booklets.status(job_id)
    except (ValueError, FileNotFoundError):
        return _not_found("Unknown assembly guide")
    if result['status'] != 'ready':
        raise HTTPException(409, "The assembly guide is not ready yet")
    return FileResponse(booklets.directory(job_id) / 'booklet.pdf', media_type='application/pdf',
                        filename=result['filename'], headers={"Cache-Control": "private, no-store"})


def collection_zip(folder: Path, filename: str):
    """Download one collection with its snapshots, BOMs and notes."""
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and not p.name.startswith(".")] if folder.is_dir() else []
    if folder.resolve() == settings.GENERATED_DIR.resolve():
        files = [p for p in files if publications.serving_allowed(p)]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.name)
    tmp.close()
    return FileResponse(tmp.name, filename=filename, media_type="application/zip",
                        background=BackgroundTask(Path(tmp.name).unlink, missing_ok=True))


@app.get("/api/models/zip")
def models_zip():
    return collection_zip(settings.GENERATED_DIR, "generated.zip")


@app.get("/api/gallery/zip")
def gallery_zip():
    return collection_zip(settings.GALLERY_MODELS_DIR, "gallery.zip")


# --- files -------------------------------------------------------------------

def _serve(root: Path, path: str, *, download: bool = False, case_insensitive: bool = False,
           cache: str = "no-cache", media_type: Optional[str] = None,
           headers: Optional[dict[str, str]] = None) -> FileResponse:
    file = safe_join(root, path, case_insensitive=case_insensitive)
    if file is None or not file.is_file():
        _not_found()
    if media_type is None and file.suffix.lower() in (".dat", ".ldr", ".mpd"):
        media_type = "text/plain; charset=utf-8"
    elif media_type is None and file.suffix.lower() == ".md":
        media_type = "text/markdown; charset=utf-8"
    return FileResponse(file, media_type=media_type, headers={"Cache-Control": cache, **(headers or {})},
                        filename=file.name if download else None)


@app.get("/files/{path:path}")
def files(path: str, download: bool = False):
    """Files in data/generated and data/chats. Checked on the resolved path, so
    generated/../output/... can't reach the agents' work folders."""
    file = safe_join(settings.DATA_DIR, path)
    if file is None or file_url(file) is None:
        _not_found()
    if settings.CHATS_DIR.resolve() in file.parents:
        if file.name in {"run.json", "run.tmp"}:
            _not_found()
        if file.stat().st_size > 32 * 1024 * 1024 or not publications.artifact_allowed(file.name, file.read_bytes()):
            _not_found()
    return _serve(settings.DATA_DIR, path, download=download)


@app.get("/demo/{path:path}", include_in_schema=False)  # preserve existing viewer links
@app.get("/gallery-files/{path:path}")
def gallery_files(path: str, download: bool = False):
    """The gallery models baked into the image, with their snapshots, BOMs and notes."""
    return _serve(settings.GALLERY_MODELS_DIR, path, download=download)


LIBRARY_CACHE = "public, max-age=31536000, immutable"   # baked into the image, never changes


@app.get("/ldraw/{path:path}")
def ldraw_library(path: str):
    return _serve(settings.LDRAW_DIR, path, case_insensitive=True, cache=LIBRARY_CACHE)


@app.get("/ldraw-id/{part_id:path}")
def ldraw_by_id(part_id: str):
    """A type-1 reference as LDraw resolves it: parts/, then p/, then models/.
    One request per part for the viewer and player instead of probing each
    folder (404s). X-LDraw-Folder says which folder it came from: the player
    treats p/ files as primitives."""
    for sub in ("parts", "p", "models"):
        file = safe_join(settings.LDRAW_DIR / sub, part_id, case_insensitive=True)
        if file is not None and file.is_file():
            return _serve(settings.LDRAW_DIR / sub, part_id, case_insensitive=True, cache=LIBRARY_CACHE,
                          headers={"X-LDraw-Folder": sub})
    _not_found()


# --- viewer + SPA ------------------------------------------------------------

class RevalidatedStaticFiles(StaticFiles):
    """Static files browsers must check with us before reusing a cached copy (a
    quick 304 while unchanged). Without Cache-Control, browsers reuse one for a
    while without asking, so after an image rebuild a page can run the previous
    version: e.g. an old player's .js and .wasm, whose URLs never change."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


# Vendored viewer and player libraries (added at build time) and our own pages
# (viewer.html, player.html) live in separate folders, so development can mount
# web/viewer/ over the pages.
if settings.VIEWER_VENDOR_DIR.is_dir():
    app.mount("/viewer/vendor", RevalidatedStaticFiles(directory=settings.VIEWER_VENDOR_DIR), name="viewer-vendor")
if settings.PLAYER_VENDOR_DIR.is_dir():
    app.mount("/viewer/player-vendor", RevalidatedStaticFiles(directory=settings.PLAYER_VENDOR_DIR),
              name="player-vendor")
if settings.VIEWER_DIR.is_dir():
    app.mount("/viewer", RevalidatedStaticFiles(directory=settings.VIEWER_DIR, html=True), name="viewer")
# The mixed-reality viewer (web/xr, built in the image): /xr/?model=<url>
if settings.XR_DIR.is_dir():
    app.mount("/xr", RevalidatedStaticFiles(directory=settings.XR_DIR, html=True), name="xr")
if (settings.STATIC_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=settings.STATIC_DIR / "assets"), name="assets")

RESERVED_PREFIXES = ("api/", "files/", "gallery-files/", "demo/", "ldraw/", "ldraw-id/", "viewer/", "xr/", "assets/")


@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str, request: Request):
    if full_path.startswith(RESERVED_PREFIXES):
        return JSONResponse({"detail": "not found"}, status_code=404)
    static = safe_join(settings.STATIC_DIR, full_path) if full_path else None
    if static is not None and static.is_file():
        return FileResponse(static)
    index = settings.STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("Frontend not built: see web/frontend (npm run build).", status_code=404)
