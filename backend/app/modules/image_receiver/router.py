"""HTTP entry point for the synchronous single-image OCR transaction."""

from functools import lru_cache
import logging
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status

from backend.app.config.settings import get_settings
from backend.app.modules.image_receiver.schemas import UploadFrameResponse
from backend.app.modules.image_receiver.service import (
    ImageValidationError,
    PipelineBusyError,
    SingleImageOcrPipeline,
)
from backend.app.modules.ocr.processor import (
    GoogleVisionOcrProcessor,
    OcrConfigurationError,
    OcrProviderError,
)
from backend.app.modules.preprocessing.processor import (
    ImagePreprocessingError,
    ImagePreprocessor,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["image_receiver"])


@lru_cache
def provide_pipeline() -> SingleImageOcrPipeline:
    settings = get_settings()
    return SingleImageOcrPipeline(
        ImagePreprocessor(),
        GoogleVisionOcrProcessor(settings.google_cloud_vision_api_key),
        settings.latest_image_path,
        settings.processed_image_path,
        settings.ocr_output_path,
        settings.max_image_bytes,
    )


PipelineDependency = Annotated[SingleImageOcrPipeline, Depends(provide_pipeline)]
ImageBody = Annotated[bytes, Body(media_type="image/jpeg")]


@router.post("/upload_frame", response_model=UploadFrameResponse)
async def upload_frame(
    request: Request, image: ImageBody, pipeline: PipelineDependency
) -> UploadFrameResponse:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"image/jpeg", "image/jpg"}:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Content-Type must be image/jpeg"
        )
    try:
        result = pipeline.process(image)
    except PipelineBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ImageValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ImagePreprocessingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except OcrConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OcrProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except OSError as exc:
        logger.exception("Local OCR artifact persistence failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"Could not save OCR artifact: {exc}",
        ) from exc

    return UploadFrameResponse(
        text=result.text,
        latest_image=str(result.latest_image),
        processed_image=str(result.processed_image),
        output_file=str(result.output_file),
    )