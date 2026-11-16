import logging
from types import ModuleType

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.modules as modules_package
from app import __version__
from app.core import health
from app.core.autoload import load_submodules
from app.core.config import Settings
from app.core.config import settings as default_settings
from app.core.middleware import REQUEST_ID_HEADER, RequestIDMiddleware


def discover_routers(package: ModuleType = modules_package) -> list[APIRouter]:
    """Every ``<package>.<module>.router.router``; modules without a router are skipped."""
    return [module.router for module in load_submodules(package, "router")]


def create_app(app_settings: Settings | None = None) -> FastAPI:
    cfg = app_settings or default_settings
    logging.basicConfig(level=cfg.log_level.upper())

    api = FastAPI(title=cfg.app_name, version=__version__)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Added last so it is outermost: the header is set on CORS preflights and errors too.
    api.add_middleware(RequestIDMiddleware)

    api.include_router(health.router)
    for router in discover_routers():
        api.include_router(router)
    return api
