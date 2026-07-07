from fastapi import APIRouter, HTTPException

from detector import DetectionClientError, detect_objects
from schemas import DetectPayload

router = APIRouter(prefix="/api/detect", tags=["detect"])

@router.post("")
def detect(payload: DetectPayload):
    try:
        response = detect_objects(payload.image, selection=payload.selection)
        return response
    except DetectionClientError as error:
        raise HTTPException(status_code=400, detail=str(error))
    except Exception:
        raise HTTPException(status_code=500, detail="Object detection failed.")
