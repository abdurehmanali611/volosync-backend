"""The wholesaler side of the Apex conversation.

The console opens a thread and replies as APEX; this endpoint lets the property
read it, reply and acknowledge Apex's messages. Replies from here are what the
console's Messages section counts as unread.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ApexChatMessage, ApexChatThread

# The top bar only offers this channel to the manager workspace, and the owner
# runs it, so both may talk to Apex. Staff logins never see the transcript.
ALLOWED_ROLES = ("MANAGER", "OWNER")


def _denied(message, code):
    return Response({"errors": {"message": message}}, status=code)


def _tenant(request):
    profile = getattr(request.user, "profile", None)
    if not profile or not profile.tenant:
        return None, _denied(
            "No property linked to this login.", status.HTTP_404_NOT_FOUND
        )
    if (profile.role or "").upper() not in ALLOWED_ROLES:
        return None, _denied(
            f"Allowed roles: {', '.join(ALLOWED_ROLES)}.", status.HTTP_403_FORBIDDEN
        )
    return profile.tenant, None


def _message_row(message):
    return {
        "id": message.id,
        "side": message.side,
        "body": message.body,
        "image_url": message.image_url or None,
        "sender": "You" if message.side == "TENANT" else "Apex",
        "created_at": message.created_at,
    }


def _transcript(thread):
    if thread is None:
        return None, []
    messages = list(thread.messages.all())
    unread = sum(1 for m in messages if m.side == "APEX" and not m.read_by_tenant)
    row = {
        "subject": thread.subject,
        "status": thread.status,
        "messages": len(messages),
        "unread_for_tenant": unread,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
    }
    return row, [_message_row(m) for m in messages]


def _payload(thread):
    row, messages = _transcript(thread)
    return {"data": {"thread": row, "messages": messages}}


class ApexChatView(APIView):
    """GET the thread, POST {"body": …, "image_url": …} to reply, PATCH {"action": "read"} to ack."""

    def get(self, request):
        tenant, denied = _tenant(request)
        if denied:
            return denied
        thread = ApexChatThread.objects.filter(tenant=tenant).first()
        return Response(_payload(thread))

    def post(self, request):
        tenant, denied = _tenant(request)
        if denied:
            return denied
        body = (request.data.get("body") or "").strip()
        image_url = (request.data.get("image_url") or "").strip()[:2048]
        if not body and not image_url:
            return _denied("Write something first.", status.HTTP_400_BAD_REQUEST)
        thread = ApexChatThread.objects.filter(tenant=tenant).first()
        if thread is None:
            thread = ApexChatThread.objects.create(
                tenant=tenant,
                subject=(request.data.get("subject") or "").strip()[:200],
                opened_by=request.user,
            )
        elif thread.status == "CLOSED":
            # A new message from the property re-opens the conversation.
            thread.status = "OPEN"
            thread.closed_by = None
            thread.closed_at = None
            thread.save(update_fields=["status", "closed_by", "closed_at", "updated_at"])

        ApexChatMessage.objects.create(
            thread=thread,
            side="TENANT",
            body=body,
            image_url=image_url,
            actor=request.user,
        )
        thread.save(update_fields=["updated_at"])
        return Response(_payload(thread), status=status.HTTP_201_CREATED)

    def patch(self, request):
        tenant, denied = _tenant(request)
        if denied:
            return denied
        if (request.data.get("action") or "").strip().lower() != "read":
            return _denied(
                'Only {"action": "read"} is accepted.', status.HTTP_400_BAD_REQUEST
            )
        thread = ApexChatThread.objects.filter(tenant=tenant).first()
        if thread is not None:
            ApexChatMessage.objects.filter(thread=thread, side="APEX").exclude(
                read_by_tenant=True
            ).update(read_by_tenant=True)
        return Response({"data": {"marked_read": True}})
