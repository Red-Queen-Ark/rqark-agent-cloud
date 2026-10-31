from fastapi import APIRouter

from .chat.routes import router as chat_router
from .ping.routes import router as ping_router


def routers() -> list[APIRouter]:
    """The mounted routers, with the audio stack left out when it is switched off.

    The imports are lazy so a deployment that sets SPEECH_ENABLED=false never loads the
    wake word or ASR stacks at all.
    """
    from src import config

    mounted = [chat_router, ping_router]
    if config.SPEECH_ENABLED:
        from .asr.routes import router as asr_router
        from .voice.routes import router as voice_router

        mounted = [asr_router, chat_router, ping_router, voice_router]
    if config.SANDBOX_ENABLED:
        from .workspace.routes import router as workspace_router

        mounted.append(workspace_router)
    return mounted
