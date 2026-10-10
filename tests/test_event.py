from rqark_core.types.chat import AssistantMessage, Chunk, ToolCall, ToolMessage, UserMessage
from rqark_core.types.event import AgentPayload
from rqark_core.types.tool import PlanItem

from src.apps.chat import event as event_module
from src.apps.chat import replay

ROOM = 'u1:s1'
MAIN = 'agent:uuid'
SUBAGENT = 'sub-uuid'
PATH = (MAIN,)
NESTED = (MAIN, SUBAGENT)


class FakeApp:
    def __init__(self):
        self.handlers: dict = {}

    def _register(self, name):
        def register(fn):
            self.handlers[name] = fn
            return fn

        return register

    def on_think(self):
        return self._register('think')

    def on_content(self):
        return self._register('content')

    def on_tool_calls(self):
        return self._register('tool_calls')

    def on_tool_result(self):
        return self._register('tool_result')

    def on_error(self):
        return self._register('error')

    def on_message(self):
        return self._register('message')

    def on_plan(self):
        return self._register('plan')

    def on_provider_switched(self):
        return self._register('provider_switched')


async def connected():
    app = FakeApp()
    await event_module.connect_agent_event_to_room(app, ROOM)
    return app


async def test_subagent_chunks_are_dropped(room_env):
    app = await connected()

    await app.handlers['think'](AgentPayload(SUBAGENT, PATH, Chunk('reasoning_content', 'hmm')))
    await app.handlers['content'](AgentPayload(SUBAGENT, PATH, Chunk('content', 'working on it')))
    await app.handlers['tool_calls'](AgentPayload(SUBAGENT, PATH, Chunk('tool_calls', [ToolCall('c1', 'function', 'read_file', '{}')])))
    await app.handlers['tool_result'](AgentPayload(SUBAGENT, PATH, ToolMessage('c1', 'file contents', 'read_file')))
    await app.handlers['error'](AgentPayload(SUBAGENT, PATH, Chunk('error', 'boom')))

    assert await replay.read(ROOM) == []


async def test_subagent_messages_are_dropped(room_env):
    app = await connected()

    await app.handlers['message'](AgentPayload(SUBAGENT, PATH, AssistantMessage('found it')))
    await app.handlers['message'](AgentPayload(SUBAGENT, PATH, UserMessage('go')))

    assert await replay.read(ROOM) == []


async def test_nested_subagent_events_are_dropped(room_env):
    app = await connected()

    await app.handlers['content'](AgentPayload(SUBAGENT, NESTED, Chunk('content', 'deeper')))
    await app.handlers['message'](AgentPayload(SUBAGENT, NESTED, AssistantMessage('deeper')))

    assert await replay.read(ROOM) == []


async def test_subagent_plans_are_dropped(room_env):
    app = await connected()

    await app.handlers['plan'](AgentPayload(SUBAGENT, PATH, [PlanItem('p1', 'step', 'pending')]))

    assert await replay.read(ROOM) == []


async def test_main_agent_chunks_are_emitted(room_env):
    app = await connected()

    await app.handlers['think'](AgentPayload(MAIN, (), Chunk('reasoning_content', 'hmm')))
    await app.handlers['content'](AgentPayload(MAIN, (), Chunk('content', 'hi')))
    await app.handlers['tool_calls'](AgentPayload(MAIN, (), Chunk('tool_calls', [ToolCall('c1', 'function', 'subagent', '{}')])))
    await app.handlers['tool_result'](AgentPayload(MAIN, (), ToolMessage('c1', 'done', 'subagent')))
    await app.handlers['error'](AgentPayload(MAIN, (), Chunk('error', 'boom')))

    events = await replay.read(ROOM)

    assert [event['agent_id'] for event in events] == [MAIN] * 5
    assert [event['path'] for event in events] == [[]] * 5
    assert [event['payload'].get('type') for event in events] == ['reasoning_content', 'content', 'tool_calls', None, 'error']
    assert events[3]['payload']['role'] == 'tool'


async def test_main_agent_messages_are_emitted(room_env):
    app = await connected()

    await app.handlers['message'](AgentPayload(MAIN, (), AssistantMessage('done')))

    events = await replay.read(ROOM)

    assert [event['agent_id'] for event in events] == [MAIN]
    assert events[0]['payload']['content'] == 'done'


async def test_tool_messages_are_dropped(room_env):
    app = await connected()

    await app.handlers['message'](AgentPayload(MAIN, (), ToolMessage('c1', 'done', 'read_file')))

    assert await replay.read(ROOM) == []


async def test_main_agent_plans_are_emitted(room_env):
    app = await connected()

    await app.handlers['plan'](AgentPayload(MAIN, (), [PlanItem('p1', 'step', 'pending')]))

    events = await replay.read(ROOM)

    assert [event['agent_id'] for event in events] == [MAIN]
    assert events[0]['payload'] == [{'id': 'p1', 'content': 'step', 'status': 'pending', 'icon': ' '}]


async def test_agentless_plans_are_dropped(room_env):
    app = await connected()

    await app.handlers['plan'](AgentPayload('root', (), [PlanItem('p1', 'step', 'pending')]))

    assert await replay.read(ROOM) == []


async def test_room_stream_keeps_only_main_agent_content(room_env):
    app = FakeApp()
    stream = await event_module.connect_agent_event_to_room(app, ROOM)

    await app.handlers['content'](AgentPayload(MAIN, (), Chunk('content', 'hi')))
    await app.handlers['content'](AgentPayload(SUBAGENT, PATH, Chunk('content', 'noise')))
    await app.handlers['content'](AgentPayload(SUBAGENT, NESTED, Chunk('content', 'deeper')))
    await app.handlers['content'](AgentPayload(MAIN, (), Chunk('content', ' there')))

    assert stream.text == 'hi there'
    assert [event['payload']['content'] for event in await replay.read(ROOM)] == ['hi', ' there']
