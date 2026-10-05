import logging

from rqark_app.app import AgentApp, Message
from rqark_app.types.event import ProviderSwitch
from rqark_core.types.chat import AssistantMessage, Chunk, SystemMessage, ToolMessage, UserMessage
from rqark_core.types.event import AgentPayload
from rqark_core.types.tool import PlanItem

from src.apps.chat.replay import record
from src.apps.chat.room import publish

logger = logging.getLogger(__name__)


def _clip(value: object, limit: int = 500) -> str:
    text = str(value)
    return text if len(text) <= limit else f'{text[:limit]}...'


def agent_payload_to_send_json(agent_payload: AgentPayload):
    payload = agent_payload.payload
    data = str(payload)
    if isinstance(payload, (Chunk, UserMessage, AssistantMessage, ToolMessage, SystemMessage)):
        data = payload.to_dict()
    elif isinstance(payload, list):
        data = []
        for p in payload:
            if isinstance(p, PlanItem):
                data.append(p.to_dict())
    return {'agent_id': agent_payload.agent_id, 'path': agent_payload.path, 'payload': data}


def error_event(message: str) -> dict:
    return agent_payload_to_send_json(AgentPayload('', (), Chunk('error', message)))


async def _emit(session_id: str, agent_payload: AgentPayload) -> None:
    data = agent_payload_to_send_json(agent_payload)
    await record(session_id, data)
    await publish(session_id, data)


async def connect_agent_event_to_room(app: AgentApp, session_id: str):
    @app.on_think()
    async def think(ev: AgentPayload[Chunk]) -> None:
        await _emit(session_id, ev)

    @app.on_content()
    async def content(ev: AgentPayload[Chunk]) -> None:
        await _emit(session_id, ev)

    @app.on_tool_calls()
    async def tool_calls(ev: AgentPayload[Chunk]) -> None:
        logger.info('tool calls session=%s agent=%s', session_id, ev.agent_id)
        await _emit(session_id, ev)

    @app.on_tool_result()
    async def tool_result(ev: AgentPayload[ToolMessage]) -> None:
        if str(ev.payload.content).startswith('Error'):
            logger.warning('tool error session=%s tool=%s content=%s', session_id, ev.payload.tool_name, _clip(ev.payload.content))
        await _emit(session_id, ev)

    @app.on_error()
    async def error(ev: AgentPayload[Chunk]) -> None:
        logger.error('agent error session=%s agent=%s content=%s', session_id, ev.agent_id, _clip(ev.payload.content))
        await _emit(session_id, ev)

    @app.on_provider_switched()
    async def provider_switched(ev: AgentPayload[ProviderSwitch]) -> None:
        logger.warning('provider switched session=%s old=%s new=%s', session_id, ev.payload.old.name, ev.payload.new.name)

    @app.on_message()
    async def message(ev: AgentPayload[Message]) -> None:
        if isinstance(ev.payload, ToolMessage):
            return
        await _emit(session_id, ev)

    @app.on_plan()
    async def plan(ev: AgentPayload[list[PlanItem]]) -> None:
        if ev.path or ':' not in ev.agent_id:
            return
        await _emit(session_id, ev)
