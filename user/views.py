from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .models import NoticeAck
from .serializers import CustomTokenObtainPairSerializer, UserSerializer

User = get_user_model()


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"data": UserSerializer(request.user).data})


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        old_password = request.data.get("old_password", "")
        new_password = request.data.get("new_password", "")
        if not request.user.check_password(old_password):
            return Response(
                {"errors": {"old_password": "Old password is incorrect."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new_password) < 6:
            return Response(
                {"errors": {"new_password": "Use at least 6 characters."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.set_password(new_password)
        request.user.save(update_fields=["password"])
        profile = getattr(request.user, "profile", None)
        if profile:
            profile.must_change_password = False
            profile.save(update_fields=["must_change_password"])
        return Response({"data": {"detail": "Password updated."}})


# Roles a MANAGER may create/remove inside their own tenant. OWNER is excluded
# (owners sign in through the volosync-owner mobile app) and MANAGER is excluded
# (a manager cannot mint peers or lock themselves out).
MANAGEABLE_ROLES = ("STORE", "FINANCE", "HR", "SALES")


def role_of(user):
    return getattr(getattr(user, "profile", None), "role", None)


def staff_row(profile):
    return {
        "id": profile.user_id,
        "profile_id": profile.id,
        "username": profile.user.username,
        "name": profile.user.get_full_name() or profile.user.username,
        "role": profile.role,
        "phone": profile.phone,
        "email": profile.user.email,
        "is_active": profile.user.is_active,
        "must_change_password": profile.must_change_password,
        "last_login": profile.user.last_login,
        "created_at": profile.created_at,
    }


class StaffAccessView(APIView):
    """Manager-only Access section: list and create tenant staff accounts."""

    permission_classes = [IsAuthenticated]

    def _guard(self, request):
        if role_of(request.user) not in ("MANAGER", "OWNER"):
            raise PermissionDenied("Only a manager can manage staff access.")
        profile = getattr(request.user, "profile", None)
        if not profile or not profile.tenant_id:
            raise PermissionDenied("This manager is not attached to a tenant.")
        return profile.tenant

    def get(self, request):
        from .models import Profile

        tenant = self._guard(request)
        # OWNER lives in the owner app and MANAGER is the signed-in account,
        # so neither belongs in the Access management list.
        profiles = (
            Profile.objects.filter(tenant=tenant, role__in=MANAGEABLE_ROLES)
            .select_related("user")
            .order_by("role", "user__username")
        )
        return Response({"data": [staff_row(profile) for profile in profiles]})

    def post(self, request):
        from .models import Profile

        tenant = self._guard(request)
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        role = (request.data.get("role") or "").strip().upper()
        phone = (request.data.get("phone") or "").strip()

        if role not in MANAGEABLE_ROLES:
            return Response(
                {"errors": {"message": f"Role must be one of: {''.join(MANAGEABLE_ROLES)}."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # A switched-off Sales role has no credential to hand out; the role is
        # enabled per tenant from VoloSync Admin. Same for roles whose module
        # is not subscribed.
        if role == "SALES" and not tenant.sales_role_enabled:
            return Response(
                {
                    "errors": {
                        "message": "Sales is switched off for this tenant. Enable it in VoloSync Admin first."
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if role == "HR" and not tenant.module_hr:
            return Response(
                {
                    "errors": {
                        "message": "The HR module is not subscribed for this tenant."
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if role == "STORE" and not tenant.module_inventory:
            return Response(
                {
                    "errors": {
                        "message": "The Inventory module is not subscribed for this tenant."
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not username:
            return Response(
                {"errors": {"message": "A username is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(password) < 6:
            return Response(
                {"errors": {"message": "Use a password of at least 6 characters."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if User.objects.filter(username__iexact=username).exists():
            return Response(
                {"errors": {"message": "That username is already taken."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # One account per role per tenant — the UI mirrors this by disabling
        # roles already in use, but the API must enforce it too.
        if Profile.objects.filter(tenant=tenant, role=role).exists():
            return Response(
                {
                    "errors": {
                        "message": f"A {role.lower()} account already exists. Delete it first."
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.create_user(
            username=username,
            password=password,
            email=(request.data.get("email") or "").strip(),
        )
        Profile.objects.create(user=user, tenant=tenant, role=role, phone=phone)
        return Response({"data": staff_row(user.profile)}, status=status.HTTP_201_CREATED)


class StaffAccessDetailView(APIView):
    """Manager-only: toggle or remove one staff account in their own tenant."""

    permission_classes = [IsAuthenticated]

    def _target(self, request, pk):
        from .models import Profile

        if role_of(request.user) not in ("MANAGER", "OWNER"):
            raise PermissionDenied("Only a manager can manage staff access.")
        manager_profile = getattr(request.user, "profile", None)
        if not manager_profile or not manager_profile.tenant_id:
            raise PermissionDenied("This manager is not attached to a tenant.")
        profile = (
            Profile.objects.filter(pk=pk, tenant=manager_profile.tenant)
            .select_related("user")
            .first()
        )
        if profile is None:
            return None
        if profile.role not in MANAGEABLE_ROLES:
            raise PermissionDenied("That account cannot be removed from Access.")
        return profile

    def patch(self, request, pk):
        profile = self._target(request, pk)
        if profile is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if "is_active" in request.data:
            profile.user.is_active = bool(request.data["is_active"])
            profile.user.save(update_fields=["is_active"])
        return Response({"data": staff_row(profile)})

    def delete(self, request, pk):
        profile = self._target(request, pk)
        if profile is None:
            return Response({"errors": {"message": "Not found."}}, status=404)
        username = profile.user.username
        profile.user.delete()
        return Response({"data": {"id": pk, "username": username, "deleted": True}})


class NoticeAckView(APIView):
    """Read watermarks for the bell's queue notifications."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = NoticeAck.objects.filter(user=request.user)
        return Response(
            {"data": [{"key": row.key, "count": row.count} for row in rows]}
        )

    def post(self, request):
        items = request.data.get("items")
        if not isinstance(items, list):
            return Response(
                {"errors": {"items": "Send a list of {key, count}."}}, status=400
            )
        saved = []
        with transaction.atomic():
            for item in items:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", ""))[:80]
                if not key:
                    continue
                try:
                    count = max(0, int(item.get("count") or 0))
                except (TypeError, ValueError):
                    count = 0
                NoticeAck.objects.update_or_create(
                    user=request.user, key=key, defaults={"count": count}
                )
                saved.append({"key": key, "count": count})
        return Response({"data": saved})
