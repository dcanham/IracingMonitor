import logging

import uvicorn

from vrmon import config, server
from vrmon.collector_hub import CollectorHub


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    hub = CollectorHub()
    hub.start()
    server.app.state.hub = hub

    try:
        uvicorn.run(server.app, host=config.SERVER_HOST, port=config.SERVER_PORT, log_level="warning", log_config=None)
    finally:
        hub.stop()


if __name__ == "__main__":
    run()
