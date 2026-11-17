import logging

from rqark_app.types.session import message_from_dict
from rqark_core.core import STOPPED_TOOL_RESULT
from rqark_core.types.chat import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage

logger = logging.getLogger(__name__)

_ROLES = {'user', 'assistant', 'tool'}


def turn_from_frames(frames: list[dict]) -> list[Message]:
    """Rebuild the turn a process that died mid-answer left in the replay log.

    The frames are what the user watched, not what the model was sent, so the
    turn is reassembled the way the core hands a cancelled turn back: whole
    messages by id, the still streaming answer from its content chunks, and a
    result for every tool call that never got one. A turn that does not start
    with a user message is not a turn and is refused.
    """
    messages: list[Message] = []
    seen: set[str] = set()
    pending: dict[str, ToolCall] = {}
    streaming: list[str] = []
    reasoning: list[str] = []
    for frame in frames:
        if frame.get('path'):
            continue
        payload = frame.get('payload')
        if not isinstance(payload, dict):
            continue
        role = payload.get('role')
        if isinstance(role, str) and role in _ROLES:
            message = _parse(payload)
            if message is None or message.id in seen:
                continue
            seen.add(message.id)
            messages.append(message)
            streaming.clear()
            reasoning.clear()
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls or []:
                    pending[call.id] = call
            elif isinstance(message, ToolMessage):
                pending.pop(message.tool_call_id, None)
            continue
        kind = payload.get('type')
        if kind == 'content':
            streaming.append(str(payload.get('content') or ''))
        elif kind == 'reasoning_content':
            reasoning.append(str(payload.get('content') or ''))
    if not messages or not isinstance(messages[0], UserMessage):
        return []
    messages.extend(ToolMessage(call.id, STOPPED_TOOL_RESULT, call.name, call.arguments) for call in pending.values())
    text = ''.join(streaming)
    thinking = ''.join(reasoning)
    if text or thinking:
        messages.append(AssistantMessage(text, reasoning_content=thinking or None))
    return messages


def _parse(payload: dict) -> Message | None:
    try:
        message = message_from_dict(payload)
    except (KeyError, TypeError, ValueError) as error:
        logger.warning('replay frame skipped error=%s', error)
        return None
    if isinstance(message, (UserMessage, AssistantMessage, ToolMessage)):
        return message
    return None
