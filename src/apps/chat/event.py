import logging
from dataclasses import dataclass, field

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


def _is_subagent(ev: AgentPayload) -> bool:
    return bool(ev.path)


@dataclass
class RoomStream:
    content: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return ''.join(self.content)


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


async def _emit(room: str, agent_payload: AgentPayload) -> None:
    data = agent_payload_to_send_json(agent_payload)
    await record(room, data)
    await publish(room, data)


async def connect_agent_event_to_room(app: AgentApp, room: str) -> RoomStream:
    stream = RoomStream()

    @app.on_think()
    async def think(ev: AgentPayload[Chunk]) -> None:
        if _is_subagent(ev):
            return
        await _emit(room, ev)

    @app.on_content()
    async def content(ev: AgentPayload[Chunk]) -> None:
        if _is_subagent(ev):
            return
        stream.content.append(str(ev.payload.content))
        await _emit(room, ev)

    @app.on_tool_calls()
    async def tool_calls(ev: AgentPayload[Chunk]) -> None:
        if _is_subagent(ev):
            return
        logger.info('tool calls room=%s agent=%s', room, ev.agent_id)
        await _emit(room, ev)

    @app.on_tool_result()
    async def tool_result(ev: AgentPayload[ToolMessage]) -> None:
        if _is_subagent(ev):
            return
        if str(ev.payload.content).startswith('Error'):
            logger.warning('tool error room=%s tool=%s content=%s', room, ev.payload.tool_name, _clip(ev.payload.content))
        await _emit(room, ev)

    @app.on_error()
    async def error(ev: AgentPayload[Chunk]) -> None:
        if _is_subagent(ev):
            return
        logger.error('agent error room=%s agent=%s content=%s', room, ev.agent_id, _clip(ev.payload.content))
        await _emit(room, ev)

    @app.on_provider_switched()
    async def provider_switched(ev: AgentPayload[ProviderSwitch]) -> None:
        logger.warning('provider switched room=%s old=%s new=%s', room, ev.payload.old.name, ev.payload.new.name)

    @app.on_message()
    async def message(ev: AgentPayload[Message]) -> None:
        if _is_subagent(ev) or isinstance(ev.payload, ToolMessage):
            return
        await _emit(room, ev)

    @app.on_plan()
    async def plan(ev: AgentPayload[list[PlanItem]]) -> None:
        if _is_subagent(ev) or ':' not in ev.agent_id:
            return
        await _emit(room, ev)

    return stream
