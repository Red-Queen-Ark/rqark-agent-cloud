from rqark_app.app import AgentApp, Message
from rqark_core.types.chat import AssistantMessage, Chunk, SystemMessage, ToolMessage, UserMessage
from rqark_core.types.event import AgentPayload
from rqark_core.types.tool import PlanItem

from src.apps.chat.room import publish


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
    return {
        'agent_id': agent_payload.agent_id,
        'path': agent_payload.path,
        'payload': data
    }

async def connect_agent_event_to_room(app: AgentApp, session_id: str):
    @app.on_think()
    async def think(ev: AgentPayload[Chunk]) -> None:
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_content()
    async def content(ev: AgentPayload[Chunk]) -> None:
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_tool_calls()
    async def tool_calls(ev: AgentPayload[Chunk]) -> None:
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_tool_result()
    async def tool_result(ev: AgentPayload[ToolMessage]) -> None:
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_error()
    async def error(ev: AgentPayload[Chunk]) -> None:
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_message()
    async def message(ev: AgentPayload[Message]) -> None:
        if isinstance(ev.payload, ToolMessage):
            return
        await publish(session_id, agent_payload_to_send_json(ev))

    @app.on_plan()
    async def plan(ev: AgentPayload[list[PlanItem]]) -> None:
        if ev.path or ':' not in ev.agent_id:
            return
        await publish(session_id, agent_payload_to_send_json(ev))
