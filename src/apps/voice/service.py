import asyncio
import json
import logging
import time
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect

from src.apps.asr import engine as asr_engine
from src.apps.chat.helper import authenticate
from src.apps.voice import vad, wake
from src.apps.voice.protocol import error_event, final_event, ready_event, started_event, wake_event
from src.apps.voice.registry import registry
from src.apps.voice.session import PcmStream, duration_ms
from src.config import ASR_CONCURRENCY, ASR_SAMPLE_RATE, VOICE_IDLE_TIMEOUT, VOICE_MAX_BYTES, VOICE_MAX_FRAME_BYTES, VOICE_WAKE_GUARD_MS, VOICE_WAKE_WINDOW

logger = logging.getLogger(__name__)

LISTENING = 'listening'
CAPTURING = 'capturing'


class _Connection:
    """One socket's pipeline: an optional wake gate in front of the VAD.

    In wake mode the audio reaches the wake model and nothing else until the word is heard,
    so an idle always-on socket costs one small model and no transcription. After a wake the
    VAD cuts the utterance, and the audio already spoken in front of the wake word is handed
    to it as pre-roll so the first word is not lost.
    """

    def __init__(self, websocket: WebSocket, user_id: str, detector: wake.Detector | None, segmenter: vad.Segmenter | None):
        self.websocket = websocket
        self.user_id = user_id
        self.detector = detector
        self.segmenter = segmenter
        self.stream = PcmStream()
        self.utterance_bytes = 0
        self.wake_mode = False
        self.gated = False
        self.since_wake = 0.0
        self.utterances = 0

    def restart_utterance(self) -> None:
        """Start a fresh utterance: clear the partial sample and the byte budget.

        The budget is per utterance, not per connection, because an always-on socket sends
        audio for as long as it stays open and would otherwise be closed mid session.
        """
        self.stream.reset()
        self.utterance_bytes = 0

    async def send(self, payload: dict) -> bool:
        """Send an event, reporting whether the socket is still there."""
        try:
            await self.websocket.send_json(payload)
            return True
        except (RuntimeError, WebSocketDisconnect, ConnectionError) as error:
            logger.info('voice send failed user=%s error=%s', self.user_id, error)
            return False

    def rearm(self) -> bool:
        """Look for the wake word again, if this socket is an always-on one."""
        if self.detector is None or not self.wake_mode:
            return False
        self.detector.reset()
        if self.segmenter is not None:
            self.segmenter.reset()
        self.since_wake = 0.0
        return True

    async def rearm_async(self) -> bool:
        return await asyncio.to_thread(self.rearm)

    async def handle_audio(self, chunk: bytes) -> bool:
        if not chunk:
            return True
        if len(chunk) > VOICE_MAX_FRAME_BYTES:
            return await self.send(error_event('FRAME_TOO_LARGE', f'a frame may carry at most {VOICE_MAX_FRAME_BYTES} bytes'))
        if not self.gated:
            self.utterance_bytes += len(chunk)
            if self.utterance_bytes > VOICE_MAX_BYTES:
                await self.send(error_event('AUDIO_TOO_LARGE', f'an utterance budget of {VOICE_MAX_BYTES} bytes is exhausted'))
                await self.websocket.close(code=1009)
                return False
        audio = self.stream.feed(chunk)
        if audio.size == 0:
            return True
        if self.since_wake and time.monotonic() - self.since_wake > VOICE_WAKE_WINDOW and (self.segmenter is None or not self.segmenter.speaking):
            logger.info('voice wake went unanswered user=%s window=%s', self.user_id, VOICE_WAKE_WINDOW)
            if not await self.send(error_event('VOICE_WAKE_TIMEOUT', 'no speech followed the wake word')):
                return False
            self.gated = await self.rearm_async()
        if self.gated and self.detector is not None:
            detection = await asyncio.to_thread(self.detector.push, audio)
            if not detection.woke:
                return True
            self.gated = False
            self.since_wake = time.monotonic()
            self.restart_utterance()
            logger.info('voice wake detected user=%s model=%s score=%.3f preroll=%d', self.user_id, detection.name or '?', detection.score or 0.0, detection.preroll.size)
            if not await self.send(wake_event(detection.score or 0.0, detection.name or '')):
                return False
            if self.segmenter is not None:
                await asyncio.to_thread(self.segmenter.reset)
                if detection.preroll.size:
                    await asyncio.to_thread(self.segmenter.prime, detection.preroll)
            audio = detection.rest
            if audio.size and self.segmenter is not None:
                guard = min(audio.size, int(VOICE_WAKE_GUARD_MS * ASR_SAMPLE_RATE / 1000))
                await asyncio.to_thread(self.segmenter.extend_pending, audio[:guard])
                audio = audio[guard:]
            if audio.size == 0:
                return True
        if self.segmenter is None:
            return True
        return await self.drain(await asyncio.to_thread(self.segmenter.push, audio))

    async def drain(self, segments: list[vad.Segment]) -> bool:
        for segment in segments:
            self.restart_utterance()
            self.utterances += 1
            if not await self.transcribe(segment):
                return False
            self.gated = await self.rearm_async()
        return True

    async def transcribe(self, segment: vad.Segment) -> bool:
        started = time.monotonic()
        try:
            text = await asr_engine.transcribe(segment.samples)
        except Exception as error:
            logger.warning('voice transcription failed user=%s error=%s', self.user_id, error)
            code = getattr(error, 'code', 'ASR_ERROR')
            message = getattr(error, 'message', str(error)) or code
            return await self.send(error_event(code, message))
        elapsed = int((time.monotonic() - started) * 1000)
        duration = duration_ms(segment.samples.size * 2)
        logger.info('voice utterance user=%s duration_ms=%d elapsed_ms=%d chars=%d', self.user_id, duration, elapsed, len(text))
        return await self.send(final_event(text, duration, elapsed, segment.truncated))


async def voice_websocket(websocket: WebSocket):
    """Audio ingress and turn boundary for the voice pipeline.

    The browser streams 16kHz mono PCM as binary frames. A client that wants an always-on
    socket sends `{"type": "mode", "value": "wake"}`: until the wake word is heard the audio
    only reaches the wake model. Otherwise it sends `start` / `stop` and cuts turns itself.
    Control frames accept either `type` or `kind` as the discriminator.
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

    socket_id = uuid4().hex
    refusal = await registry.acquire(user_id, socket_id)
    if refusal is not None:
        logger.warning('voice refused user=%s reason=%s', user_id, refusal)
        await websocket.send_json(error_event('VOICE_BUSY', refusal))
        await websocket.close(code=4429)
        return

    websocket.state.user = user
    connection: _Connection | None = None
    try:
        detector, wake_error = await _build_detector()
        segmenter, vad_error = await _build_segmenter()
        if detector is not None and segmenter is None:
            logger.warning('voice wake word disabled without vad user=%s', user_id)
            detector = None
        connection = _Connection(websocket, user_id, detector, segmenter)
        names = detector.names if detector is not None else []
        await connection.send(ready_event(ASR_CONCURRENCY, bool(names), segmenter is not None, names))
        if wake_error is not None:
            await connection.send(error_event('WAKE_UNAVAILABLE', wake_error))
        if vad_error is not None:
            await connection.send(error_event('VAD_UNAVAILABLE', vad_error))
        logger.info('voice connected user=%s wake=%s vad=%s', user_id, ','.join(names) or 'off', segmenter is not None)

        refreshed = time.monotonic()
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=VOICE_IDLE_TIMEOUT)
            except TimeoutError:
                logger.info('voice idle timeout user=%s seconds=%s', user_id, VOICE_IDLE_TIMEOUT)
                await connection.send(error_event('VOICE_IDLE', f'no frames for {VOICE_IDLE_TIMEOUT:g}s'))
                await websocket.close(code=4408)
                return
            if message.get('type') == 'websocket.disconnect':
                return
            now = time.monotonic()
            if now - refreshed > registry.ttl / 3:
                refreshed = now
                await registry.refresh(user_id, socket_id)
            chunk = message.get('bytes')
            if chunk is not None:
                if not await connection.handle_audio(chunk):
                    return
                continue
            text = message.get('text')
            if text is None:
                continue
            if not await _handle_control(connection, text):
                return
    except WebSocketDisconnect:
        pass
    finally:
        logger.info('voice closed user=%s utterances=%d bytes=%d', user_id, connection.utterances if connection else 0, connection.stream.total if connection else 0)
        await registry.release(user_id, socket_id)
        if connection is not None:
            await _close_stage(connection.detector)
            await _close_stage(connection.segmenter)


async def _close_stage(stage: object | None) -> None:
    close = getattr(stage, 'close', None)
    if close is None:
        return
    try:
        await asyncio.to_thread(close)
    except Exception as error:
        logger.warning('voice stage close failed error=%s', error)


async def _handle_control(connection: _Connection, text: str) -> bool:
    try:
        control = json.loads(text)
    except ValueError as error:
        return await connection.send(error_event('BAD_FRAME', f'the frame is not json: {error}'))
    if not isinstance(control, dict):
        return await connection.send(error_event('BAD_FRAME', 'a control frame must be a json object'))
    kind = control.get('kind', control.get('type'))
    if kind == 'mode':
        connection.wake_mode = control.get('value') == 'wake'
        connection.gated = await connection.rearm_async()
        return True
    if kind == 'start':
        connection.restart_utterance()
        connection.gated = await connection.rearm_async()
        if connection.segmenter is not None:
            await asyncio.to_thread(connection.segmenter.reset)
        return await connection.send(started_event())
    if kind == 'cancel':
        connection.restart_utterance()
        connection.gated = await connection.rearm_async()
        if connection.segmenter is not None:
            await asyncio.to_thread(connection.segmenter.reset)
        return True
    if kind == 'stop':
        segment = None
        if connection.segmenter is not None:
            segment = await asyncio.to_thread(connection.segmenter.flush)
        connection.restart_utterance()
        if segment is None:
            connection.gated = await connection.rearm_async()
            return await connection.send(error_event('AUDIO_EMPTY', 'no speech arrived before the utterance ended'))
        connection.utterances += 1
        sent = await connection.transcribe(segment)
        connection.gated = await connection.rearm_async()
        return sent
    return await connection.send(error_event('BAD_FRAME', f'unknown frame type: {kind!r}'))


async def _build_detector() -> tuple[wake.Detector | None, str | None]:
    """Load the wake word stage, keeping a broken model loud instead of silent."""
    if not wake.available():
        return None, 'openwakeword is not installed, the wake word stage is off'
    try:
        return await asyncio.to_thread(wake.for_connection), None
    except wake.WakeUnavailableError as error:
        logger.warning('voice wake word unavailable error=%s', error.message)
        return None, error.message
    except Exception as error:
        logger.exception('voice wake word crashed')
        return None, f'the wake word stage failed: {error}'


async def _build_segmenter() -> tuple[vad.Segmenter | None, str | None]:
    try:
        return await asyncio.to_thread(vad.Segmenter), None
    except vad.VadUnavailableError as error:
        logger.warning('voice vad unavailable error=%s', error.message)
        return None, error.message
    except Exception as error:
        logger.exception('voice vad crashed')
        return None, f'the vad stage failed: {error}'
