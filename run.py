import asyncio
import logging
import os
import threading
import uvicorn

from bot.main import run_bot
from api.server import app


def start_api():
    render_port = os.getenv("PORT")
    host = "0.0.0.0" if render_port else os.getenv("API_HOST", "127.0.0.1")
    port = int(render_port or os.getenv("API_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
