from .asr.routes import router as asr_router
from .chat.routes import router as chat_router
from .ping.routes import router as ping_router
from .workspace.routes import router as workspace_router

ROUTERS = [asr_router, chat_router, ping_router, workspace_router]
