"""File upload router — parse and record uploaded files."""
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from src.routers.auth import get_current_user
from src.services.file_processor import process_file

router = APIRouter(prefix="/upload", tags=["upload"])

# In-memory upload history (keyed by upload_id)
_history: dict[str, dict] = {}


@router.post("/file", response_model=Dict)
async def upload_file(
    file: UploadFile = File(...),
    _: dict = Depends(get_current_user),
):
    """Upload a file, extract SDLC ontology triples, and store in the knowledge graph."""
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(400, "Empty file")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "File too large — maximum 20 MB")

    upload_id = str(uuid.uuid4())

    try:
        processed = process_file(file.filename, raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))

    record = {
        "upload_id": upload_id,
        "filename": file.filename,
        "file_type": processed.file_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "triples_inserted": 0,
        "entities": {},
        "error": None,
        "metadata": processed.metadata,
    }
    _history[upload_id] = record

    return record


@router.get("/history", response_model=List[Dict])
def get_upload_history(_: dict = Depends(get_current_user)):
    """List the last 50 uploads with their triple counts."""
    items = sorted(_history.values(), key=lambda x: x["uploaded_at"], reverse=True)
    return items[:50]


@router.delete("/{upload_id}", status_code=204)
def delete_upload(upload_id: str, _: dict = Depends(get_current_user)):
    """Remove an upload record from history (does not remove RDF triples)."""
    if upload_id not in _history:
        raise HTTPException(404, f"Upload {upload_id!r} not found")
    del _history[upload_id]
