import sys

import uvicorn

from src.utils.logging import setup_logging


def main():
    setup_logging()
    reload = 'debug' in sys.argv
    uvicorn.run('src.app:create_fastapi_app', host="0.0.0.0", port=8000, reload=reload, factory=True)

if __name__ == '__main__':
    main()
