from .chat.routes import router as chat_router
from .ping.routes import router as ping_router

ROUTERS = [chat_router, ping_router]