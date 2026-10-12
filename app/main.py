"""Sev One: a LaunchDarkly AgentControl demo. FastAPI app entry point.

Run:  python -m app        (then open http://localhost:8000)

Two audiences, two kinds of endpoints:
- Engineer endpoints (/api/bootstrap, /api/incident, /api/chat) only ever return what the on-call
  engineer may see: their incident, its runbook steps, and the answer they were sent.
- Operator endpoints (/api/operator/*, /api/local/*) carry everything else: what LaunchDarkly
  is serving, blocked answers, checks, judge scores, tool calls, and the rollback controls.
  They are open from this computer only, unless you set OPERATOR_TOKEN (see .env.example).
  Requests that change something must also carry the X-Sev-One header, which a form posted
  from another website cannot add, so another site can't flip the flag through your browser.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import tools
from .agent import BLOCK_REASONS, OperatorLog, TurnEngine, build_handlers, public_incident
from .ld import LDRuntime
from .live_updates import LiveHub
from .local_flags import STAGES
from .operator_actions import fire_trigger
from .personas import PERSONAS, get_persona
from .settings import Settings, load_settings

STATIC = Path(__file__).resolve().parent.parent / "static"
OPERATOR_COOKIE = "sev_one_operator"
CSRF_HEADER = "x-sev-one"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The AI SDK warns once per start when the optional OpenTelemetry packages are not installed.
# Tracing is optional for this demo (see README), so keep the console readable.
logging.getLogger("launchdarkly_ai_server.lifecycle").setLevel(logging.ERROR)
# httpx logs every request URL at INFO. The trigger URL is a secret, so keep these quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ChatRequest(BaseModel):
    persona: str
    question: str = Field(min_length=1, max_length=500)


class StageRequest(BaseModel):
    stage: str


class ConnectionRequest(BaseModel):
    state: str  # "valid" or "interrupted"


class EditRequest(BaseModel):
    variation_key: str
    sentence: str = Field(min_length=1, max_length=300)


class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


def _host_name(host_header: str) -> str:
    """'localhost:8000' -> 'localhost', '[::1]:8000' -> '::1'."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def engineer_view(result: dict) -> dict:
    """Only what the on-call engineer may see. No blocked text, checks, scores, or tool calls."""
    return {
        "turn_id": result["turn_id"],
        "persona_key": result["persona"]["key"],
        "answer": result["answer"],
        "incident": result["incident"],
        "steps": result["steps"],
        # The step the AI recommended, already checked against this incident's runbook. The step
        # itself is still built from runbook data. None unless an AI answer was released.
        "recommended_step_id": result.get("recommended_step_id"),
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or load_settings()
        runtime = LDRuntime(s)
        await runtime.start()
        hub = LiveHub(runtime, watched_keys={s.flag_key, s.agent_config_key})
        hub.attach(asyncio.get_running_loop())
        oplog = OperatorLog()
        app.state.settings = s
        app.state.runtime = runtime
        app.state.hub = hub
        app.state.oplog = oplog
        app.state.engine = TurnEngine(s, runtime, build_handlers(s), oplog)
        logging.getLogger("sev_one").info(
            "Sev One ready: LaunchDarkly=%s, model=%s, flag=%s, config=%s, judge=%s, operator access=%s",
            s.ld_mode,
            s.llm_mode,
            s.flag_key,
            s.agent_config_key,
            s.judge_config_key or "off",
            "token" if s.operator_token else "this computer only",
        )
        yield
        hub.close()
        await runtime.stop()

    app = FastAPI(title="Sev One", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # -- access control ---------------------------------------------------------------------

    def require_operator(request: Request) -> None:
        s: Settings = request.app.state.settings
        if s.operator_token:
            supplied = request.headers.get("x-operator-token") or request.cookies.get(OPERATOR_COOKIE) or ""
            if not secrets.compare_digest(supplied.encode(), s.operator_token.encode()):
                raise HTTPException(status_code=401, detail="Operator sign-in required.")
            return
        peer = request.client.host if request.client else ""
        # Check the Host header too. Otherwise a website could point its own name at 127.0.0.1
        # (DNS rebinding), count as the same site, and reach the console from a browser.
        if peer not in LOCAL_HOSTS or _host_name(request.headers.get("host", "")) not in LOCAL_HOSTS:
            raise HTTPException(
                status_code=403,
                detail="The operator console is open from this computer only (open http://localhost). Set OPERATOR_TOKEN to allow other machines.",
            )

    def require_operator_change(request: Request) -> None:
        require_operator(request)
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="Missing X-Sev-One header.")

    # -- on-call engineer ---------------------------------------------------------------------

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request):
        s: Settings = request.app.state.settings
        return {
            "personas": [p.public() for p in PERSONAS.values()],
            "modes": {"launchdarkly": s.ld_mode, "llm": s.llm_mode},
        }

    @app.get("/api/incident")
    async def incident(persona: str):
        """The engineer's incident and runbook steps, straight from the backend (no AI)."""
        try:
            p = get_persona(persona)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown persona") from None
        record = tools.lookup_incident_for(p.key, p.incident_id)
        return {
            "incident": public_incident(record),
            "steps": tools.lookup_steps_for(p.key, p.incident_id) or [],
        }

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request):
        st = request.app.state
        try:
            persona = get_persona(body.persona)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown persona") from None
        result = await st.engine.run(persona, body.question.strip())

        # Everything about how the answer was decided goes to the operator channel only.
        await st.hub.broadcast("turn", result)
        decision = result["decision"]
        used = result.get("config_used") or {}
        if decision["reason"] in BLOCK_REASONS:
            await st.hub.note("answer_blocked", persona=persona.name, reason=decision["reason"], variation_key=used.get("variation_key"))
            await st.hub.broadcast("operator", st.oplog.snapshot())
        elif decision["released"]:
            await st.hub.note("answer_released", persona=persona.name, variation_key=used.get("variation_key"), version=used.get("version"))
        return engineer_view(result)

    # -- operator ------------------------------------------------------------------------------

    @app.post("/api/operator/session")
    async def operator_session(body: SessionRequest, request: Request, response: Response):
        """Exchange OPERATOR_TOKEN for a same-site cookie, so the live event stream can use it."""
        s: Settings = request.app.state.settings
        if request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=403, detail="Missing X-Sev-One header.")
        if not s.operator_token or not secrets.compare_digest(body.token.encode(), s.operator_token.encode()):
            raise HTTPException(status_code=401, detail="That operator token is not right.")
        response.set_cookie(OPERATOR_COOKIE, s.operator_token, httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/operator/bootstrap", dependencies=[Depends(require_operator)])
    async def operator_bootstrap(request: Request):
        st = request.app.state
        s: Settings = st.settings
        return {
            "keys": {"flag": s.flag_key, "agent_config": s.agent_config_key, "judge": s.judge_config_key or None},
            "judge_threshold": s.judge_pass_threshold,
            "trigger": {
                # Only whether a URL is configured. The URL itself never leaves the server.
                "off_configured": bool(s.trigger_off_url) or not s.live,
                "on_configured": bool(s.trigger_on_url) or not s.live,
            },
            "local": st.runtime.local.snapshot() if st.runtime.local else None,
            "operator": st.oplog.snapshot(),
            "fixture_armed": st.engine.fixture_armed,
            "connection": st.runtime.connection(),
        }

    @app.get("/api/operator/events", dependencies=[Depends(require_operator)])
    async def operator_events(persona: str, request: Request):
        hub: LiveHub = request.app.state.hub
        try:
            sub = await hub.subscribe(persona)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown persona") from None

        async def body():
            async for chunk in hub.stream(sub):
                if await request.is_disconnected():
                    break
                yield chunk

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/operator/rollback", dependencies=[Depends(require_operator_change)])
    async def rollback(request: Request):
        return await _trigger(request, turn_on=False)

    @app.post("/api/operator/restore", dependencies=[Depends(require_operator_change)])
    async def restore(request: Request):
        return await _trigger(request, turn_on=True)

    async def _trigger(request: Request, turn_on: bool):
        st = request.app.state
        outcome = await fire_trigger(st.settings, st.runtime, turn_on=turn_on)
        await st.hub.note("trigger_fired", action=outcome["action"], ok=outcome["ok"], via=outcome["via"], error=outcome.get("error"))
        return outcome

    @app.post("/api/operator/fixture", dependencies=[Depends(require_operator_change)])
    async def arm_fixture(request: Request):
        """Arm the labeled test fixture: the next AI answer (any engineer) is a known bad reply
        instead of a model call. Works in live and local mode, so the failure happens on cue."""
        st = request.app.state
        st.engine.fixture_armed = True
        await st.hub.note("fixture_armed")
        return {"armed": True}

    # -- local test mode only: stand-ins for changes you make in the LaunchDarkly UI ------------

    def _local(request: Request):
        local = request.app.state.runtime.local
        if local is None:
            raise HTTPException(status_code=404, detail="Only available in local test mode. In live mode, make this change in LaunchDarkly.")
        return local

    @app.post("/api/local/stage", dependencies=[Depends(require_operator_change)])
    async def set_stage(body: StageRequest, request: Request):
        local = _local(request)
        if body.stage not in STAGES:
            raise HTTPException(status_code=400, detail=f"Unknown stage. Use one of: {', '.join(STAGES)}")
        local.apply_stage(body.stage)
        await request.app.state.hub.note("local_stage", stage=body.stage, description=STAGES[body.stage])
        return local.snapshot()

    @app.post("/api/local/connection", dependencies=[Depends(require_operator_change)])
    async def set_connection(body: ConnectionRequest, request: Request):
        local = _local(request)
        if body.state not in ("valid", "interrupted"):
            raise HTTPException(status_code=400, detail="state must be 'valid' or 'interrupted'")
        local.simulate_connection(body.state)
        return local.snapshot()

    @app.post("/api/local/edit", dependencies=[Depends(require_operator_change)])
    async def edit_instructions(body: EditRequest, request: Request):
        local = _local(request)
        try:
            version = local.edit_instructions(body.variation_key, body.sentence)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        await request.app.state.hub.note("local_edit", variation_key=body.variation_key, version=version)
        return {"variation_key": body.variation_key, "version": version}

    @app.get("/api/health")
    async def health(request: Request):
        return {"ok": True, "connection": request.app.state.runtime.connection()}

    return app


app = create_app()
