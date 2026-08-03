"""Room file-library and output-grading API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from messenger import room_files
from messenger.deps import current_user, get_store

router = APIRouter(tags=["room-files"])


def _access(store: Any, room_id: str, user_id: str, *, edit: bool = False):
    room = store.room(room_id)
    if not room:
        return None, JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    role = store.room_member_role(room_id, user_id)
    if not role and str(room.get("owner_user_id") or "") == user_id:
        role = "owner"
    if role not in {"owner", "editor", "viewer"}:
        return None, JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if edit and role not in {"owner", "editor"}:
        return None, JSONResponse(
            {
                "ok": False,
                "error": "editor_required",
                "message": "Viewer access cannot change team files or grades.",
            },
            status_code=403,
        )
    return (room, role), None


@router.get("/api/rooms/{room_id}/files")
def room_file_list(
    room_id: str,
    user: dict[str, Any] = Depends(current_user),
    store: Any = Depends(get_store),
) -> JSONResponse:
    access, error = _access(store, room_id, user["user_id"])
    if error:
        return error
    room, role = access
    return JSONResponse(
        {
            "ok": True,
            "room_id": room_id,
            "title": room.get("title") or room_id,
            "can_edit": role in {"owner", "editor"},
            "files": room_files.list_files(room_id),
        }
    )


@router.post("/api/rooms/{room_id}/files")
async def room_file_add(
    room_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    store: Any = Depends(get_store),
) -> JSONResponse:
    _, error = _access(store, room_id, user["user_id"], edit=True)
    if error:
        return error
    form = await request.form()
    upload = form.get("upload")
    pasted = str(form.get("body") or "")
    filename = str(getattr(upload, "filename", "") or "")
    content_type = str(getattr(upload, "content_type", "") or "")
    content = b""
    if upload and hasattr(upload, "read"):
        content = await upload.read(room_files.MAX_FILE_BYTES + 1)
    if len(content) > room_files.MAX_FILE_BYTES:
        return JSONResponse(
            {"ok": False, "error": "file_too_large", "message": "Files must be 10 MB or smaller."},
            status_code=413,
        )
    if pasted.strip():
        content = pasted.encode("utf-8")
        if not filename:
            filename = "note.md"
        if not content_type:
            content_type = "text/markdown"
    if not content:
        return JSONResponse(
            {"ok": False, "error": "content_required", "message": "Attach a file or paste text."},
            status_code=400,
        )
    tags = [part.strip() for part in str(form.get("tags") or "").split(",") if part.strip()]
    use_for_context = str(form.get("use_for_context") or "").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    item = room_files.add_file(
        room_id,
        name=filename or "document",
        content=content,
        title=str(form.get("title") or filename or "Document"),
        kind=str(form.get("kind") or "upload"),
        mime=content_type or "application/octet-stream",
        source="upload",
        tags=tags,
        notes=str(form.get("notes") or ""),
        use_for_context=use_for_context,
        created_by=user["user_id"],
    )
    return JSONResponse({"ok": True, "file": item})


@router.get("/api/rooms/{room_id}/files/{file_id}")
def room_file_get(
    room_id: str,
    file_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    store: Any = Depends(get_store),
):
    _, error = _access(store, room_id, user["user_id"])
    if error:
        return error
    row = room_files.get_file(room_id, file_id)
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    if request.query_params.get("download") == "1":
        path = room_files.content_path(room_id, row)
        if not path.exists():
            return JSONResponse({"ok": False, "error": "content_missing"}, status_code=404)
        return FileResponse(
            path,
            media_type=str(row.get("mime") or "application/octet-stream"),
            filename=Path(str(row.get("name") or "document")).name,
        )
    return JSONResponse(
        {
            "ok": True,
            "file": {key: value for key, value in row.items() if key != "stored_name"},
            "text": room_files.text_content(room_id, row),
        }
    )


@router.patch("/api/rooms/{room_id}/files/{file_id}")
async def room_file_update(
    room_id: str,
    file_id: str,
    request: Request,
    user: dict[str, Any] = Depends(current_user),
    store: Any = Depends(get_store),
) -> JSONResponse:
    _, error = _access(store, room_id, user["user_id"], edit=True)
    if error:
        return error
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        row = room_files.update_file(
            room_id,
            file_id,
            payload,
            grader=user.get("display_name") or user["user_id"],
        )
    except (TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid_grade", "message": "Grade must be from 1 to 5."},
            status_code=400,
        )
    if not row:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "file": row})


@router.delete("/api/rooms/{room_id}/files/{file_id}")
def room_file_delete(
    room_id: str,
    file_id: str,
    user: dict[str, Any] = Depends(current_user),
    store: Any = Depends(get_store),
) -> JSONResponse:
    _, error = _access(store, room_id, user["user_id"], edit=True)
    if error:
        return error
    if not room_files.delete_file(room_id, file_id):
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    return JSONResponse({"ok": True, "file_id": file_id})
