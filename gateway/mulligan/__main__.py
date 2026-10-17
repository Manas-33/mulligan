"""`python -m mulligan`: run the gateway."""

from __future__ import annotations

import uvicorn

from mulligan.app import create_app
from mulligan.settings import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.bind_host, port=settings.api_port)


if __name__ == "__main__":
    main()
