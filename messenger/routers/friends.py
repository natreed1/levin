"""Friends, username search, and in-app notifications."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from messenger.auth import normalize_username
from messenger.db import MessageStore
from messenger.deps import current_user, get_store

router = APIRouter(tags=["friends"])


def _public(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user["user_id"],
        "username": user.get("username"),
        "display_name": user.get("display_name"),
    }


@router.get("/api/users/search")
def search_users(
    q: str = "",
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    query = (q or "").strip()
    if len(query) < 1:
        return JSONResponse({"ok": True, "users": []})
    results = store.search_users_by_username(
        query, exclude_user_id=user["user_id"], limit=20
    )
    enriched = []
    for row in results:
        rel = store.friendship_between(user["user_id"], row["user_id"])
        status = None
        if rel:
            if rel["status"] == "accepted":
                status = "friends"
            elif rel["status"] == "pending":
                if rel["requester_id"] == user["user_id"]:
                    status = "outgoing"
                else:
                    status = "incoming"
            else:
                status = rel["status"]
        enriched.append({**row, "relationship": status})
    return JSONResponse({"ok": True, "users": enriched})


@router.get("/api/friends")
def list_friends(
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    friends = store.list_friends(user["user_id"])
    incoming = store.list_pending_friend_requests(user["user_id"], incoming=True)
    outgoing = store.list_pending_friend_requests(user["user_id"], incoming=False)
    me = store.user_by_id(user["user_id"]) or {}
    return JSONResponse(
        {
            "ok": True,
            "username": me.get("username"),
            "friends": friends,
            "incoming": incoming,
            "outgoing": outgoing,
        }
    )


@router.post("/api/friends/request")
async def send_friend_request(
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    me = store.user_by_id(user["user_id"]) or {}
    if not me.get("username"):
        return JSONResponse(
            {
                "ok": False,
                "error": "username_required",
                "message": "Set a username in Settings → Profile before adding friends.",
            },
            status_code=400,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    target: dict[str, Any] | None = None
    username = normalize_username(str(body.get("username") or ""))
    target_id = str(body.get("user_id") or "").strip()
    if username:
        target = store.user_by_username(username)
    elif target_id:
        target = store.user_by_id(target_id)
    else:
        return JSONResponse(
            {
                "ok": False,
                "error": "bad_target",
                "message": "Provide a username to send a friend request.",
            },
            status_code=400,
        )
    if not target:
        return JSONResponse(
            {
                "ok": False,
                "error": "user_not_found",
                "message": "No account found with that username.",
            },
            status_code=404,
        )
    if not target.get("username"):
        return JSONResponse(
            {
                "ok": False,
                "error": "target_no_username",
                "message": "That person has not set a username yet.",
            },
            status_code=400,
        )
    if target["user_id"] == user["user_id"]:
        return JSONResponse(
            {"ok": False, "error": "self_friend", "message": "You can’t friend yourself."},
            status_code=400,
        )

    try:
        friendship = store.create_friend_request(user["user_id"], target["user_id"])
    except ValueError as exc:
        code = str(exc)
        messages = {
            "already_friends": "You’re already friends.",
            "request_pending": "A friend request is already pending.",
            "self_friend": "You can’t friend yourself.",
        }
        return JSONResponse(
            {
                "ok": False,
                "error": code,
                "message": messages.get(code, code),
            },
            status_code=409 if code in {"already_friends", "request_pending"} else 400,
        )

    store.create_notification(
        target["user_id"],
        "friend_request",
        {
            "from_user_id": user["user_id"],
            "from_username": me.get("username"),
            "from_display_name": me.get("display_name"),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "friendship": friendship,
            "user": _public(target),
            "message": f"Friend request sent to @{target['username']}.",
        }
    )


@router.post("/api/friends/{other_user_id}/accept")
def accept_friend_request(
    other_user_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    other_user_id = (other_user_id or "").strip()
    try:
        result = store.respond_friend_request(
            user["user_id"], other_user_id, accept=True
        )
    except ValueError:
        return JSONResponse(
            {
                "ok": False,
                "error": "request_not_found",
                "message": "No pending friend request from that user.",
            },
            status_code=404,
        )
    me = store.user_by_id(user["user_id"]) or {}
    other = store.user_by_id(other_user_id) or {}
    store.create_notification(
        other_user_id,
        "friend_accepted",
        {
            "from_user_id": user["user_id"],
            "from_username": me.get("username"),
            "from_display_name": me.get("display_name"),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "friendship": result,
            "user": _public(other) if other else {"user_id": other_user_id},
            "message": "Friend request accepted.",
        }
    )


@router.post("/api/friends/{other_user_id}/reject")
def reject_friend_request(
    other_user_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    other_user_id = (other_user_id or "").strip()
    try:
        result = store.respond_friend_request(
            user["user_id"], other_user_id, accept=False
        )
    except ValueError:
        return JSONResponse(
            {
                "ok": False,
                "error": "request_not_found",
                "message": "No pending friend request from that user.",
            },
            status_code=404,
        )
    return JSONResponse(
        {"ok": True, "friendship": result, "message": "Friend request declined."}
    )


@router.delete("/api/friends/{other_user_id}")
def remove_friend(
    other_user_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    other_user_id = (other_user_id or "").strip()
    removed = store.remove_friendship(user["user_id"], other_user_id)
    if not removed:
        return JSONResponse(
            {"ok": False, "error": "not_friends", "message": "Not friends."},
            status_code=404,
        )
    return JSONResponse({"ok": True, "message": "Removed."})


@router.get("/api/notifications")
def list_notifications(
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    notes = store.list_notifications(user["user_id"], limit=50)
    unread = store.count_unread_notifications(user["user_id"])
    return JSONResponse({"ok": True, "notifications": notes, "unread": unread})


@router.post("/api/notifications/{notification_id}/read")
def read_notification(
    notification_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    ok = store.mark_notification_read(user["user_id"], notification_id)
    if not ok:
        return JSONResponse(
            {"ok": False, "error": "not_found"}, status_code=404
        )
    return JSONResponse(
        {
            "ok": True,
            "unread": store.count_unread_notifications(user["user_id"]),
        }
    )


@router.post("/api/notifications/read-all")
def read_all_notifications(
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    count = store.mark_all_notifications_read(user["user_id"])
    return JSONResponse({"ok": True, "marked": count, "unread": 0})


@router.get("/api/rooms/{room_id}/members")
def room_members(
    room_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    if not store.user_in_room(room_id, user["user_id"]):
        return JSONResponse(
            {"ok": False, "error": "forbidden"}, status_code=403
        )
    return JSONResponse(
        {"ok": True, "members": store.list_room_members(room_id)}
    )


@router.post("/api/rooms/{room_id}/members")
async def add_room_member(
    room_id: str,
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    room = store.room(room_id)
    if not room:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    owner = str(room.get("owner_user_id") or "").strip()
    if owner != user["user_id"] and not store.user_in_room(room_id, user["user_id"]):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    if owner and owner != user["user_id"]:
        return JSONResponse(
            {
                "ok": False,
                "error": "owner_required",
                "message": "Only the team owner can add friends to this team.",
            },
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    friend_id = str(body.get("user_id") or "").strip()
    if not friend_id:
        username = normalize_username(str(body.get("username") or ""))
        if username:
            found = store.user_by_username(username)
            friend_id = str((found or {}).get("user_id") or "")
    if not friend_id:
        return JSONResponse(
            {"ok": False, "error": "bad_target", "message": "Pick a friend to add."},
            status_code=400,
        )
    if not store.are_friends(user["user_id"], friend_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "not_friends",
                "message": "You can only add people you’re friends with.",
            },
            status_code=403,
        )
    friend = store.user_by_id(friend_id)
    if not friend:
        return JSONResponse(
            {"ok": False, "error": "user_not_found"}, status_code=404
        )
    store.add_room_member(room_id, friend_id)
    store.create_notification(
        friend_id,
        "room_invite",
        {
            "room_id": room_id,
            "room_title": room.get("title"),
            "from_user_id": user["user_id"],
            "from_username": (store.user_by_id(user["user_id"]) or {}).get("username"),
            "from_display_name": user.get("display_name") or user.get("name"),
        },
    )
    return JSONResponse(
        {
            "ok": True,
            "member": _public(friend),
            "message": f"Added @{friend.get('username') or friend['display_name']} to the team.",
        }
    )
