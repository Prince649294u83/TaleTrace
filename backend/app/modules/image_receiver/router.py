"""HTTP entry point for the synchronous single-image OCR transaction."""

from functools import lru_cache
import logging
from pathlib import Path
from time import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status

from backend.app.config.settings import get_settings
from backend.app.modules.image_receiver.schemas import UploadResponse
from backend.app.modules.image_receiver.service import (
    ImageValidationError,
    ImageWriteError,
    PipelineBusyError,
    OutputWriteError,
    SingleImageOcrPipeline,
)
from backend.app.modules.ocr.processor import (
    GoogleVisionOcrProcessor,
    OcrConfigurationError,
    OcrProviderError,
    OcrTimeoutError,
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
    backend_directory = Path(__file__).resolve().parents[3]
    output_directory = backend_directory / "output"
    return SingleImageOcrPipeline(
        ImagePreprocessor(),
        GoogleVisionOcrProcessor(),
        output_directory / "latest.jpg",
        output_directory / "processed.jpg",
        output_directory / "output.txt",
        settings.max_image_bytes,
    )


PipelineDependency = Annotated[SingleImageOcrPipeline, Depends(provide_pipeline)]
ImageUpload = Annotated[UploadFile | None, File(description="One JPEG image")]


@router.post("/upload", response_model=UploadResponse)
async def upload(
    request: Request, pipeline: PipelineDependency, image: ImageUpload = None
) -> UploadResponse:
    logger.info("Request received | time=%.3f | client=%s", time(), request.client)
    if image is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No image was provided")
    content_type = (image.content_type or "").lower()
    if content_type not in {"image/jpeg", "image/jpg"}:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only JPEG images are supported"
        )
    image_bytes = await image.read()
    try:
        result = pipeline.process(image_bytes)
    except PipelineBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ImageValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ImageWriteError as exc:
        logger.exception("Image save failure")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    except ImagePreprocessingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except OcrConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except OcrTimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, str(exc)) from exc
    except OcrProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except OutputWriteError as exc:
        logger.exception("TXT write failure")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected OCR pipeline failure")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "OCR processing failed unexpectedly",
        ) from exc

    return UploadResponse(
        characters=result.characters,
        output_file="backend/output/output.txt",
    )
