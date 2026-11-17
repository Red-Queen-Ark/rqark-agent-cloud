from rqark_core.core import STOPPED_TOOL_RESULT
from rqark_core.types.chat import AssistantMessage, ToolCall, ToolMessage, UserMessage

from src.apps.chat.recover import turn_from_frames


def frame(payload, path=()):
    return {'agent_id': 'a', 'path': list(path), 'payload': payload}


def chunk(kind, content):
    return frame({'type': kind, 'content': content})


def user(text='hi', id='u1'):
    return frame(UserMessage(text, id=id).to_dict())


def assistant(text='', tool_calls=None, id='a1'):
    return frame(AssistantMessage(text, tool_calls=tool_calls, id=id).to_dict())


def tool(tool_call_id='c1', id='t1'):
    return frame(ToolMessage(tool_call_id, 'result', 'name', '{}', id=id).to_dict())


def test_a_streamed_answer_becomes_one_message():
    turn = turn_from_frames([user(), chunk('content', 'he'), chunk('content', 'llo'), chunk('reasoning_content', 'why')])

    assert [type(message).__name__ for message in turn] == ['UserMessage', 'AssistantMessage']
    assert turn[1].content == 'hello'
    assert turn[1].reasoning_content == 'why'


def test_the_answer_already_sent_whole_is_not_streamed_again():
    turn = turn_from_frames([user(), chunk('content', 'hel'), assistant('hello')])

    assert [type(message).__name__ for message in turn] == ['UserMessage', 'AssistantMessage']
    assert turn[1].content == 'hello'


def test_a_tool_iteration_is_kept_in_order():
    call = ToolCall('c1', 'function', 'name', '{}')
    turn = turn_from_frames([user(), chunk('content', 'let me check'), assistant('', [call]), tool(), chunk('content', 'done')])

    assert [type(message).__name__ for message in turn] == ['UserMessage', 'AssistantMessage', 'ToolMessage', 'AssistantMessage']
    assert turn[1].tool_calls == [call]
    assert turn[2].tool_call_id == 'c1'
    assert turn[3].content == 'done'


def test_a_tool_call_that_never_returned_is_marked_stopped():
    call = ToolCall('c1', 'function', 'name', '{}')
    turn = turn_from_frames([user(), assistant('', [call]), chunk('content', 'waiting')])

    assert [type(message).__name__ for message in turn] == ['UserMessage', 'AssistantMessage', 'ToolMessage', 'AssistantMessage']
    assert turn[2].content == STOPPED_TOOL_RESULT
    assert turn[2].tool_name == 'name'
    assert turn[3].content == 'waiting'


def test_subagent_frames_are_left_out():
    turn = turn_from_frames([user(), chunk('content', 'main'), {'agent_id': 'sub', 'path': ['a', 'b'], 'payload': {'type': 'content', 'content': 'sub'}}])

    assert len(turn) == 2
    assert turn[1].content == 'main'


def test_a_frame_seen_twice_is_folded_in_once():
    turn = turn_from_frames([user(), assistant('hi'), assistant('hi')])

    assert len(turn) == 2


def test_a_replay_without_a_user_message_is_not_a_turn():
    assert turn_from_frames([chunk('content', 'orphan')]) == []
    assert turn_from_frames([]) == []


def test_a_corrupt_frame_is_skipped():
    turn = turn_from_frames([user(), frame({'role': 'assistant'}), chunk('content', 'ok')])

    assert [type(message).__name__ for message in turn] == ['UserMessage', 'AssistantMessage']
    assert turn[1].content == 'ok'
