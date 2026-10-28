import asyncio
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from src.apps.asr import engine as asr_engine
from src.apps.asr.errors import AsrError
from src.apps.chat.helper import authenticate
from src.apps.voice import vad, wake
from src.apps.voice.protocol import error_event, final_event, ready_event, started_event, wake_event
from src.apps.voice.registry import registry
from src.apps.voice.session import duration_ms, int16_from_bytes
from src.config import ASR_CONCURRENCY, VOICE_IDLE_TIMEOUT

logger = logging.getLogger(__name__)


async def _transcribe(samples, websocket: WebSocket, user_id: str, truncated: bool) -> bool:
    started = time.monotonic()
    try:
        text = await asr_engine.transcribe(samples)
    except AsrError as error:
        logger.warning('voice transcription failed user=%s code=%s error=%s', user_id, error.code, error.message)
        await websocket.send_json(error_event(error.code, error.message))
        return False
    elapsed = int((time.monotonic() - started) * 1000)
    duration = duration_ms(samples.size * 2)
    logger.info('voice utterance user=%s duration_ms=%d elapsed_ms=%d chars=%d', user_id, duration, elapsed, len(text))
    await websocket.send_json(final_event(text, duration, elapsed, truncated))
    return True


async def voice_websocket(websocket: WebSocket):
    """Audio ingress and turn boundary for the voice pipeline.

    The browser streams PCM as binary frames. A wake word model scores the stream as it
    arrives, then the VAD marks where an utterance starts and ends; each utterance is
    transcribed and answered with a final event. A client that would rather cut the turn
    itself sends `start` and `stop` control frames instead.
    """
    await websocket.accept()
    token = websocket.query_params.get('token')
    user = await authenticate(token)
    if user is None:
        logger.warning('voice auth failed')
        await websocket.close(code=4401)
        return
    user_id = str(user.get('id') or '')
    if not user_id:
        logger.warning('voice auth missing user id')
        await websocket.close(code=4401)
        return

    refusal = registry.reason_to_refuse(user_id)
    if refusal is not None:
        logger.warning('voice refused user=%s reason=%s', user_id, refusal)
        await websocket.send_json(error_event('VOICE_BUSY', refusal))
        await websocket.close(code=4429)
        return

    websocket.state.user = user
    registry.add(user_id, websocket)
    received = 0
    utterances = 0
    try:
        detector = await asyncio.to_thread(wake.for_connection)
        segmenter = await asyncio.to_thread(build_segmenter)
        await websocket.send_json(ready_event(ASR_CONCURRENCY, detector is not None, segmenter is not None))
        logger.info('voice connected user=%s wake=%s vad=%s', user_id, wake.model_name() or 'off', segmenter is not None)
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=VOICE_IDLE_TIMEOUT)
            except TimeoutError:
                logger.info('voice idle timeout user=%s seconds=%s', user_id, VOICE_IDLE_TIMEOUT)
                await websocket.send_json(error_event('VOICE_IDLE', f'no frames for {VOICE_IDLE_TIMEOUT:g}s'))
                await websocket.close(code=4408)
                return
            if message.get('type') == 'websocket.disconnect':
                logger.info('voice disconnected user=%s utterances=%d bytes=%d', user_id, utterances, received)
                return
            chunk = message.get('bytes')
            if chunk is not None:
                received += len(chunk)
                audio = int16_from_bytes(chunk)
                if detector is not None:
                    score = detector.push(audio)
                    if detector.fired(score):
                        if segmenter is not None:
                            segmenter.reset()
                        logger.info('voice wake detected user=%s score=%.3f', user_id, score or 0.0)
                        await websocket.send_json(wake_event(score or 0.0))
                        continue
                if segmenter is not None:
                    for segment in segmenter.push(audio):
                        utterances += 1
                        await _transcribe(segment.samples, websocket, user_id, segment.truncated)
                continue

            text = message.get('text')
            if text is None:
                continue
            try:
                control: dict[str, str] = json.loads(text)
            except ValueError as error:
                await websocket.send_json(error_event('BAD_FRAME', f'the frame is not json: {error}'))
                continue

            kind = control.get('type')
            if kind == 'start':
                if segmenter is not None:
                    segmenter.reset()
                await websocket.send_json(started_event())
            elif kind == 'cancel':
                if segmenter is not None:
                    segmenter.reset()
            elif kind == 'stop':
                segment = segmenter.flush() if segmenter is not None else None
                if segment is None:
                    await websocket.send_json(error_event('AUDIO_EMPTY', 'no speech arrived before the utterance ended'))
                    continue
                utterances += 1
                await _transcribe(segment.samples, websocket, user_id, segment.truncated)
            else:
                await websocket.send_json(error_event('BAD_FRAME', f'unknown frame type: {kind!r}'))
    except WebSocketDisconnect:
        logger.info('voice disconnected user=%s utterances=%d bytes=%d', user_id, utterances, received)
    finally:
        registry.remove(user_id, websocket)


def build_segmenter() -> vad.Segmenter | None:
    try:
        return vad.Segmenter()
    except vad.VadUnavailableError:
        return None
