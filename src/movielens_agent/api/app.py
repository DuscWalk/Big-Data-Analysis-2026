"""Loopback-only application API and static frontend."""
from contextlib import asynccontextmanager
import fcntl
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..agent.service import AgentService
from ..agent.settings import Settings
from ..agent.preferences import ModelPreferences, ModelEdit, ModelCheck, SettingsConflict
from ..agent.probe import list_models, tool_probe
from ..contracts import ArtifactRef, Contract, ToolContext
from ..governance.config import GovernanceConfig
from ..storage.catalog import DatasetCatalog
from ..storage.conversations import ConversationBusy, ConversationConflict, ConversationStore
from ..storage.tasks import TaskStore, file_digest, now
from ..storage.governance_configs import ConfigurationConflict


class SessionInput(Contract):
    title: str = Field(default="新会话", min_length=1, max_length=80)


class MessageInput(Contract):
    request_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=6000)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    configuration_ref: ArtifactRef | None = None


class ConfigurationInput(Contract):
    name: str = Field(min_length=1, max_length=80)
    configuration: GovernanceConfig
    set_default: bool = False
    revision: str | None = Field(default=None, max_length=64)


class DefaultConfigurationInput(Contract):
    config_ref: ArtifactRef
    revision: str = Field(min_length=1, max_length=64)


class RetryInput(Contract):
    request_id: str = Field(min_length=1, max_length=128)


def public_task(task):
    result = {key: task[key] for key in ("task_id", "workflow", "status", "stage", "error", "created_at", "updated_at")}
    result.update({key: task["payload"].get(key) for key in
                   ("configuration", "config_refs", "configuration_ref", "configuration_name")})
    result["attempts"] = [{key: item[key] for key in
                          ("sequence", "stage", "status", "external_ids", "error", "started_at", "ended_at", "progress")}
                         for item in task["attempts"]]
    result["artifacts"] = [
        {key: item[key] for key in ("ref", "kind", "state")} | {
            "files": [{key: file[key] for key in ("name", "size_bytes", "sha256")}
                      for file in item["files"]]} for item in task["artifacts"]]
    return result


def create_app(settings=None, model=None):
    settings = settings or Settings.load()
    settings.catalog = settings.catalog.resolve()
    preferences = ModelPreferences(settings)
    settings = preferences.snapshot()
    conversations = ConversationStore(settings.catalog)
    conversations.initialize()
    tasks, catalog = TaskStore(settings.catalog), DatasetCatalog(settings.catalog)
    configuration = GovernanceConfig.read(settings.governance_config)
    agent = AgentService(settings, conversations, tasks, catalog, configuration, model=model)

    @asynccontextmanager
    async def lifespan(app):
        lock_path = Path(str(settings.catalog) + ".api.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another API process owns this catalog.") from error
            conversations.recover_interrupted_messages()
            yield

    app = FastAPI(title="MovieLens 数据治理", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.agent, app.state.conversations, app.state.tasks = agent, conversations, tasks
    app.state.model_preferences = preferences
    allowed_hosts = ["127.0.0.1", "localhost", "[::1]", "testserver"]
    if settings.public_origin:
        allowed_hosts.append(urlsplit(settings.public_origin).hostname)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            expected_origin = request.url.scheme + "://" + request.headers.get("host", "")
            if origin != expected_origin:
                return JSONResponse({"detail": "只接受同一页面来源的请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(KeyError)
    async def unavailable(request, error):
        return JSONResponse({"detail": "会话、任务、产物或治理方案不存在，或在当前会话不可见。"}, status_code=404)

    @app.exception_handler(ConversationBusy)
    @app.exception_handler(ConversationConflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Validation must never echo API keys or other submitted field values.
        return JSONResponse({"detail": "请求参数无效，请检查字段格式和数值范围。"}, status_code=422)

    @app.exception_handler(SettingsConflict)
    @app.exception_handler(ConfigurationConflict)
    async def settings_conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/v1/governance-configs")
    def governance_configs(offset: int = Query(0, ge=0, le=100000), limit: int = Query(20, ge=1, le=100)):
        return agent.configurations.list(offset, limit)

    @app.post("/api/v1/governance-configs", status_code=201)
    def save_governance_config(body: ConfigurationInput):
        try:
            item = agent.configurations.register(body.name, body.configuration, make_default=body.set_default, revision=body.revision)
            return item | {"default": agent.configurations.default()}
        except ConfigurationConflict:
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.put("/api/v1/governance-configs/default")
    def default_governance_config(body: DefaultConfigurationInput):
        item = agent.configurations.set_default(body.config_ref, body.revision)
        return item | {"default": agent.configurations.default()}

    @app.get("/api/v1/model-settings")
    def model_settings():
        return preferences.public()

    @app.put("/api/v1/model-settings")
    def save_model_settings(body: ModelEdit):
        nonlocal agent, settings
        try:
            with preferences.lock:
                candidate = preferences.prepare(body)
                replacement = AgentService(candidate, conversations, tasks, catalog, configuration, model=model)
                settings = preferences.save(body)
                agent = replacement
                app.state.agent = replacement
                return preferences.public()
        except SettingsConflict:
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, "模型设置未保存，请检查本地目录权限与磁盘空间。") from None

    def check_settings(body):
        try:
            return preferences.prepare(body.configuration).model_copy(update={"model_provider": body.provider})
        except SettingsConflict:
            raise
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.post("/api/v1/model-settings/models")
    def model_list(body: ModelCheck):
        candidate = check_settings(body)
        return candidate.redact(list_models(candidate))

    @app.post("/api/v1/model-settings/probe")
    def model_probe(body: ModelCheck):
        candidate = check_settings(body)
        started = time.monotonic()
        try:
            result = tool_probe(candidate)
        except Exception:
            result = {"status": "failed", "error": "PROBE_FAILED", "message": "模型检测未完成，请检查配置后重试。"}
        return candidate.redact(result | {"checked_at": now(), "duration_ms": int((time.monotonic() - started) * 1000),
                                          "checked_provider": body.provider})

    @app.get("/api/v1/status")
    def status():
        current = app.state.agent
        active = current.settings
        default = current.configurations.default()
        scheme = current.configurations.get(ArtifactRef.model_validate(default["ref"]))
        return {"model_configured": active.configured,
                "model_name": active.model_name if active.model_provider != "backup" else active.backup_name,
                "model_provider": active.model_provider,
                "backup_model_name": active.backup_name if active.provider_configured("backup") else None,
                "datasets": current.datasets(), "configuration": scheme["configuration"],
                "configuration_refs": scheme["config_refs"], "default_configuration": default}

    @app.get("/api/v1/sessions")
    def list_sessions(q: str = Query("", max_length=100), offset: int = Query(0, ge=0, le=100000),
                      limit: int = Query(20, ge=1, le=100)):
        return settings.redact(conversations.list_sessions(q.strip(), offset, limit))

    @app.post("/api/v1/sessions", status_code=201)
    def create_session(body: SessionInput):
        return conversations.create_session(settings.redact(body.title))

    @app.get("/api/v1/sessions/{session_id}")
    def session(session_id: str):
        return conversations.session(session_id)

    @app.get("/api/v1/sessions/{session_id}/messages")
    def messages(session_id: str, offset: int = Query(0, ge=0, le=100000),
                 limit: int = Query(100, ge=1, le=100)):
        items = conversations.messages(session_id, offset, limit + 1)
        return {"items": items[:limit], "has_more": len(items) > limit, "offset": offset}

    @app.post("/api/v1/sessions/{session_id}/messages")
    def message(session_id: str, body: MessageInput):
        if not body.content.strip():
            raise HTTPException(422, "消息不能为空。")
        result = agent.respond(session_id, body.request_id, body.content, body.task_id,
                               configuration_ref=body.configuration_ref.model_dump() if body.configuration_ref else None)
        return JSONResponse(result, status_code=503 if result["status"] == "failed" else 200)

    @app.post("/api/v1/sessions/{session_id}/messages/{message_id}/retry")
    def retry_message(session_id: str, message_id: str, body: RetryInput):
        result = agent.retry(session_id, message_id, body.request_id)
        return JSONResponse(result, status_code=503 if result["status"] == "failed" else 200)

    @app.get("/api/v1/sessions/{session_id}/messages/{message_id}/calls")
    def calls(session_id: str, message_id: str):
        return {"items": conversations.calls(session_id, message_id),
                "models": conversations.model_calls(session_id, message_id)}

    @app.get("/api/v1/sessions/{session_id}/tasks")
    def list_tasks(session_id: str, offset: int = Query(0, ge=0, le=100000),
                   limit: int = Query(50, ge=1, le=100)):
        conversations.session(session_id)
        with tasks.connect() as conn:
            rows = conn.execute("SELECT task_id,status,stage,error,created_at,updated_at FROM tasks "
                                "WHERE session_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                (session_id, limit + 1, offset)).fetchall()
        return {"items": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @app.get("/api/v1/sessions/{session_id}/tasks/{task_id}")
    def task(session_id: str, task_id: str):
        conversations.session(session_id)
        return public_task(tasks.get(task_id, session_id))

    def artifact_result(session_id, ref, mode, file_name=None, offset=0, limit=3, reason=None):
        conversations.session(session_id)
        result = agent.registry.call("artifacts.get", {
            "artifact_ref": ref.model_dump(), "mode": mode, "file_name": file_name,
            "offset": offset, "limit": limit, "reason": reason,
        }, ToolContext(session_id=session_id, call_id="http-artifact"))
        if result.status != "completed":
            code = result.error.code
            status = 404 if code == "ARTIFACT_NOT_FOUND" else 409 if code == "ARTIFACT_INVALID" else 422
            raise HTTPException(status, {"code": code, "message": result.error.message})
        return result.model_dump(mode="json")

    @app.get("/api/v1/sessions/{session_id}/artifacts/{artifact_id}/versions/{version}")
    def artifact(session_id: str, artifact_id: str, version: str,
                 mode: Literal["metadata", "summary", "sample", "examples"] = "metadata",
                 file_name: str | None = None, reason: str | None = Query(None, max_length=120), offset: int = Query(0, ge=0, le=10000),
                 limit: int = Query(3, ge=1, le=20)):
        return artifact_result(session_id, ArtifactRef(artifact_id=artifact_id, version=version),
                               mode, file_name, offset, limit, reason)

    @app.get("/api/v1/sessions/{session_id}/tasks/{task_id}/quality")
    def quality(session_id: str, task_id: str):
        conversations.session(session_id)
        value = tasks.get(task_id, session_id)
        reports = [item for item in value["artifacts"] if item["kind"] == "quality_report"]
        if value["status"] != "succeeded" or len(reports) != 1:
            raise HTTPException(409, "任务尚无已发布的完整质量结果。")
        return artifact_result(session_id, ArtifactRef.model_validate(reports[0]["ref"]), "summary")

    @app.post("/api/v1/sessions/{session_id}/tasks/{task_id}/explanation")
    def explanation(session_id: str, task_id: str):
        conversations.session(session_id)
        value = tasks.get(task_id, session_id)
        if value["status"] != "succeeded":
            raise HTTPException(409, "任务完成后才能生成结果解释。")
        result = agent.respond(session_id, "explain:" + task_id,
            "请读取这次任务的实际质量摘要，解释五维前后变化、数据处置损失、时间边界与仍无法核验的问题。",
            task_id, require_quality=True)
        return JSONResponse(result, status_code=503 if result["status"] == "failed" else 200)

    @app.get("/api/v1/sessions/{session_id}/artifacts/{artifact_id}/versions/{version}/download")
    def download(session_id: str, artifact_id: str, version: str, file_name: str | None = None):
        conversations.session(session_id)
        value = tasks.artifact(ArtifactRef(artifact_id=artifact_id, version=version), session_id)
        matching = [file for file in value["files"] if file_name is None or file["name"] == file_name]
        if len(matching) != 1:
            raise HTTPException(422, "请选择产物中登记的具体文件。")
        file = matching[0]
        path = Path(file["path"])
        if not path.is_file() or file_digest(path) != file["sha256"]:
            raise HTTPException(409, "产物文件缺失或内容已改变。")
        return FileResponse(path, filename=file["name"], media_type="application/octet-stream")

    web = Path(__file__).resolve().parents[1] / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app
