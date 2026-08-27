"""Root API router composing all module routers."""

from fastapi import APIRouter

from backend.app.api.health import router as health_router
from backend.app.modules.ai_engine.router import router as ai_engine_router
from backend.app.modules.audio_engine.router import router as audio_engine_router
from backend.app.modules.database.router import router as database_router
from backend.app.modules.gesture_engine.router import router as gesture_engine_router
from backend.app.modules.image_receiver.router import router as image_receiver_router
from backend.app.modules.ocr.router import router as ocr_router
from backend.app.modules.preprocessing.router import router as preprocessing_router
from backend.app.modules.reading_engine.router import router as reading_engine_router
from backend.app.modules.reading_speed.router import router as reading_speed_router
from backend.app.modules.session.router import router as session_router
from backend.app.api.device import router as device_router

api_router = APIRouter()
api_router.include_router(health_router)

for module_router in (
    image_receiver_router,
    preprocessing_router,
    ocr_router,
    reading_engine_router,
    gesture_engine_router,
    ai_engine_router,
    audio_engine_router,
    reading_speed_router,
    database_router,
    session_router,
    device_router,
):
    api_router.include_router(module_router)