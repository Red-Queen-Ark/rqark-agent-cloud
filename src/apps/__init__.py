from .auth.routes import router as auth_router
from .chat.routes import router as chat_router
from .ping.routes import router as ping_router

ROUTERS = [auth_router, chat_router, ping_router]
