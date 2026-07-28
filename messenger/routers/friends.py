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
    room = store.room(room_id) or {}
    return JSONResponse(
        {
            "ok": True,
            "members": store.list_room_members(room_id),
            "pending_invites": store.list_pending_room_invites_for_room(room_id),
            "owner_user_id": room.get("owner_user_id"),
            "my_role": store.room_member_role(room_id, user["user_id"]),
        }
    )


@router.post("/api/rooms/{room_id}/members")
async def invite_room_member(
    room_id: str,
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Owner invites a friend — they must accept before joining the team."""
    from messenger.db import normalize_room_role

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
                "message": "Only the team owner can invite people to this team.",
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
            {"ok": False, "error": "bad_target", "message": "Pick a friend to invite."},
            status_code=400,
        )
    if not store.are_friends(user["user_id"], friend_id):
        return JSONResponse(
            {
                "ok": False,
                "error": "not_friends",
                "message": "You can only invite people you’re friends with.",
            },
            status_code=403,
        )
    friend = store.user_by_id(friend_id)
    if not friend:
        return JSONResponse(
            {"ok": False, "error": "user_not_found"}, status_code=404
        )
    role = normalize_room_role(body.get("role"), default="editor")
    if role == "owner":
        role = "editor"
    try:
        invite = store.create_room_invite(
            room_id,
            inviter_id=user["user_id"],
            invitee_id=friend_id,
            role=role,
        )
    except ValueError as exc:
        code = str(exc)
        messages = {
            "already_member": "They’re already on this team.",
            "self_invite": "You can’t invite yourself.",
        }
        return JSONResponse(
            {
                "ok": False,
                "error": code,
                "message": messages.get(code, "Could not send invite."),
            },
            status_code=409 if code == "already_member" else 400,
        )
    store.create_notification(
        friend_id,
        "room_invite",
        {
            "invite_id": invite["invite_id"],
            "room_id": room_id,
            "room_title": room.get("title"),
            "role": invite.get("role") or role,
            "from_user_id": user["user_id"],
            "from_username": (store.user_by_id(user["user_id"]) or {}).get("username"),
            "from_display_name": user.get("display_name") or user.get("name"),
        },
    )
    handle = friend.get("username") or friend["display_name"]
    return JSONResponse(
        {
            "ok": True,
            "pending": True,
            "invite": invite,
            "member": {**_public(friend), "role": invite.get("role") or role},
            "message": (
                f"Invite sent to @{handle} as {invite.get('role') or role}. "
                "They’ll see it in Notifications."
            ),
        }
    )


@router.post("/api/rooms/invites/{invite_id}/accept")
def accept_room_invite(
    invite_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        invite = store.respond_room_invite(
            invite_id, user["user_id"], accept=True
        )
    except ValueError as exc:
        code = str(exc)
        return JSONResponse(
            {
                "ok": False,
                "error": code,
                "message": (
                    "Invite not found or already handled."
                    if code == "invite_not_found"
                    else "You can’t accept this invite."
                ),
            },
            status_code=404 if code == "invite_not_found" else 403,
        )
    room = store.room(str(invite["room_id"])) or {}
    inviter_id = str(invite.get("inviter_id") or "")
    if inviter_id:
        store.create_notification(
            inviter_id,
            "room_invite_accepted",
            {
                "invite_id": invite["invite_id"],
                "room_id": invite["room_id"],
                "room_title": room.get("title"),
                "from_user_id": user["user_id"],
                "from_username": (store.user_by_id(user["user_id"]) or {}).get(
                    "username"
                ),
                "from_display_name": user.get("display_name") or user.get("name"),
            },
        )
    return JSONResponse(
        {
            "ok": True,
            "invite": invite,
            "room": {
                "room_id": invite["room_id"],
                "title": room.get("title"),
            },
            "message": f"Joined {room.get('title') or 'the team'}.",
        }
    )


@router.post("/api/rooms/invites/{invite_id}/reject")
def reject_room_invite(
    invite_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    try:
        invite = store.respond_room_invite(
            invite_id, user["user_id"], accept=False
        )
    except ValueError as exc:
        code = str(exc)
        return JSONResponse(
            {
                "ok": False,
                "error": code,
                "message": (
                    "Invite not found or already handled."
                    if code == "invite_not_found"
                    else "You can’t decline this invite."
                ),
            },
            status_code=404 if code == "invite_not_found" else 403,
        )
    return JSONResponse(
        {"ok": True, "invite": invite, "message": "Invite declined."}
    )


@router.patch("/api/rooms/{room_id}/members/{member_user_id}")
async def patch_room_member(
    room_id: str,
    member_user_id: str,
    request: Request,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Owner-only: change a member’s access (editor / viewer), Docs-style."""
    from messenger.db import normalize_room_role

    room = store.room(room_id)
    if not room:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    owner = str(room.get("owner_user_id") or "").strip()
    if not owner or owner != user["user_id"]:
        return JSONResponse(
            {
                "ok": False,
                "error": "owner_required",
                "message": "Only the team owner can change access.",
            },
            status_code=403,
        )
    target_id = str(member_user_id or "").strip()
    if not target_id:
        return JSONResponse({"ok": False, "error": "bad_target"}, status_code=400)
    if target_id == owner:
        return JSONResponse(
            {
                "ok": False,
                "error": "cannot_change_owner",
                "message": "Owner access can’t be changed here.",
            },
            status_code=400,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    role = normalize_room_role((body or {}).get("role"), default="editor")
    if role == "owner":
        return JSONResponse(
            {
                "ok": False,
                "error": "cannot_transfer_owner",
                "message": "Ownership transfer isn’t supported yet.",
            },
            status_code=400,
        )
    if not store.user_in_room(room_id, target_id):
        return JSONResponse(
            {"ok": False, "error": "not_a_member", "message": "Not on this team."},
            status_code=404,
        )
    updated = store.set_room_member_role(room_id, target_id, role)
    if not updated:
        return JSONResponse(
            {"ok": False, "error": "update_failed"}, status_code=400
        )
    member = store.user_by_id(target_id) or {"user_id": target_id}
    return JSONResponse(
        {
            "ok": True,
            "member": {**_public(member), "role": updated},
            "message": f"Access updated to {updated}.",
        }
    )


@router.delete("/api/rooms/{room_id}/members/{member_user_id}")
def remove_room_member(
    room_id: str,
    member_user_id: str,
    store: MessageStore = Depends(get_store),
    user: dict[str, Any] = Depends(current_user),
) -> JSONResponse:
    """Owner removes someone, or a member leaves themselves."""
    room = store.room(room_id)
    if not room:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    owner = str(room.get("owner_user_id") or "").strip()
    target_id = str(member_user_id or "").strip()
    if not target_id:
        return JSONResponse({"ok": False, "error": "bad_target"}, status_code=400)
    if target_id == owner:
        return JSONResponse(
            {
                "ok": False,
                "error": "cannot_remove_owner",
                "message": "The owner can’t be removed from the team.",
            },
            status_code=400,
        )
    is_owner = bool(owner and owner == user["user_id"])
    is_self = target_id == user["user_id"]
    if not is_owner and not is_self:
        return JSONResponse(
            {
                "ok": False,
                "error": "owner_required",
                "message": "Only the owner can remove other people.",
            },
            status_code=403,
        )
    if not store.remove_room_member(room_id, target_id):
        return JSONResponse(
            {"ok": False, "error": "not_a_member", "message": "Not on this team."},
            status_code=404,
        )
    return JSONResponse({"ok": True, "removed": target_id})

