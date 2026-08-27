"""Device debugging endpoints."""

from fastapi import APIRouter, HTTPException, Response

from backend.app.modules.image_receiver.esp32_camera import Esp32Camera

router = APIRouter(prefix="/api/debug", tags=["device"])


@router.get("/camera", summary="Get a single frame from the camera")
def get_camera_frame() -> Response:
    """Fetch a raw JPEG frame directly from the ESP32-CAM.
    
    This is a diagnostic endpoint. It does not start a reading session or
    interact with the reading runtime; it only proxies the camera.
    """
    camera = Esp32Camera()
    if not camera.configured:
        raise HTTPException(
            status_code=503,
            detail="ESP32_CAM_CAPTURE_URL not configured in environment.",
        )

    frame_bytes = camera.frame()
    if frame_bytes is None:
        raise HTTPException(
            status_code=504,
            detail="Camera did not answer or returned no frame.",
        )

    return Response(content=frame_bytes, media_type="image/jpeg")
