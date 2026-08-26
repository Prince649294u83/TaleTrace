"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.api.companion import router as companion_router
from backend.app.api.router import api_router
from backend.app.config.settings import get_settings
from backend.app.core.environment import load_environment
from backend.app.core.logging_config import configure_logging
from backend.app.modules.database.recording import hydrate_reading_speed
from backend.app.modules.database.session import init_db

load_environment()
settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Make the tables, then load the reader's baseline back into memory.

    Order matters: `hydrate_reading_speed` reads the `readers` table, which does
    not exist on a fresh checkout until `init_db` has run.

    The same function both session runners call, from the database module rather
    than from the API: a calibration is only worth storing if the rig reads the
    same row the browser wrote, and one implementation is what guarantees that.
    """

    init_db()
    hydrate_reading_speed()
    yield


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

# The website is served by Vite on another port, which makes every call to this
# process cross-origin. Without this the browser blocks the response *after* the
# server has already handled the request — so the backend log shows 200s while
# the dashboard stays blank, and the only evidence is in the browser console.
#
# An explicit origin list, not "*": `allow_credentials` and a wildcard are
# mutually exclusive per the CORS spec, and the day this grows a session cookie
# is not the day to discover that.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(companion_router)
