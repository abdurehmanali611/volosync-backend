from datetime import timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed, Throttled
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from billing.access import tenant_access
from sales.models import SalesAgent

from .models import LoginAttempt, Profile

# R2 guardrails: five consecutive failures inside fifteen minutes lock the
# username out for fifteen minutes; a successful login clears the counter.
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=15)


def login_locked_out(username):
    """Locked while the newest 15 minutes hold 5 consecutive failures.

    The scan stops at the first success, so one good login clears the
    counter, and the lockout naturally expires once the fifth failure
    ages out of the window.
    """
    if not username:
        return False
    since = timezone.now() - LOCKOUT_WINDOW
    attempts = LoginAttempt.objects.filter(
        username__iexact=username, created_at__gte=since
    ).order_by("-created_at")
    failures = 0
    for attempt in attempts:
        if attempt.success:
            break
        failures += 1
        if failures >= LOCKOUT_THRESHOLD:
            return True
    return False


def client_ip(request):
    if request is None:
        return ""
    return request.META.get("REMOTE_ADDR", "")


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """One login for staff users and legacy sales agents (spec Q7 + R2).

    Staff authenticate by username. Agents created today are records on the
    tenant's shared Sales login and never reach this path; legacy agents that
    still own a user keep the password-matching fallback, which verifies every
    active candidate so the response never reveals which field was wrong.
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        profile = getattr(user, "profile", None)
        token["role"] = profile.role if profile else ""
        token["tenant_id"] = profile.tenant_id if profile else None
        token["tenant_name"] = profile.tenant.name if profile and profile.tenant else ""
        token["name"] = user.get_full_name() or user.username
        return token

    def validate(self, attrs):
        username = attrs.get("username", "")
        password = attrs.get("password", "")

        if login_locked_out(username):
            raise Throttled(detail="Too many attempts. Try again later.")

        ip = client_ip(self.context.get("request"))

        # 1. Staff first: the existing behavior, unchanged.
        self.user = authenticate(username=username, password=password)
        if self.user is not None and self.user.is_active:
            profile = getattr(self.user, "profile", None)
            # A switched-off Sales role means no Sales login at all — staff or
            # agent. Keep the tenant flag check here as well as on the agent path.
            if profile is not None and profile.role == "SALES":
                tenant = profile.tenant
                if not (tenant and tenant.sales_role_enabled):
                    LoginAttempt.objects.create(
                        username=username, ip=ip, success=False, kind="STAFF"
                    )
                    raise AuthenticationFailed(
                        detail="Sales is switched off for this tenant."
                    )
            # Role-module gates: a role whose module is not subscribed has no
            # login at all — mirrors the sidebar's module filtering.
            if profile is not None and profile.role == "HR":
                tenant = profile.tenant
                if not (tenant and tenant.module_hr):
                    LoginAttempt.objects.create(
                        username=username, ip=ip, success=False, kind="STAFF"
                    )
                    raise AuthenticationFailed(
                        detail="The HR module is not subscribed for this tenant."
                    )
            if profile is not None and profile.role == "STORE":
                tenant = profile.tenant
                if not (tenant and tenant.module_inventory):
                    LoginAttempt.objects.create(
                        username=username, ip=ip, success=False, kind="STAFF"
                    )
                    raise AuthenticationFailed(
                        detail="The Inventory module is not subscribed for this tenant."
                    )
            if profile is not None and profile.role == "FINANCE":
                tenant = profile.tenant
                if not (tenant and tenant.module_inventory):
                    LoginAttempt.objects.create(
                        username=username, ip=ip, success=False, kind="STAFF"
                    )
                    raise AuthenticationFailed(
                        detail="The Inventory module is not subscribed for this tenant."
                    )
            # Yearly billing gate. The credentials were right, so a denial here
            # is not a failed attempt and must not feed the lockout counter.
            access = tenant_access(
                profile.tenant_id if profile else None,
                profile.role if profile else "",
            )
            if not access["allowed"]:
                raise AuthenticationFailed(detail=access["message"])
            LoginAttempt.objects.create(
                username=username, ip=ip, success=True, kind="STAFF"
            )
            return self._success_data(access)

        # 2. Sales agent path — only tenants with the sales role enabled.
        agent, kind = self._match_agent(username, password)
        if agent is not None:
            if not agent.tenant.sales_role_enabled:
                LoginAttempt.objects.create(
                    username=username, ip=ip, success=False, kind=kind
                )
                raise AuthenticationFailed(
                    detail="Sales is switched off for this tenant."
                )
            # Agents created through the Manager UI predate profiles; make
            # sure role gating works on every later request.
            Profile.objects.get_or_create(
                user=agent.user,
                defaults={"tenant": agent.tenant, "role": "SALES"},
            )
            self.user = agent.user
            access = tenant_access(agent.tenant_id, "SALES")
            if not access["allowed"]:
                raise AuthenticationFailed(detail=access["message"])
            LoginAttempt.objects.create(
                username=username, ip=ip, success=True, kind="AGENT", agent=agent
            )
            data = self._success_data(access)
            data["user"] = {
                "id": agent.user_id,
                "username": agent.user.username,
                "name": agent.full_name,
                "role": "SALES",
                "tenant": agent.tenant.name,
                "tenant_id": agent.tenant_id,
                "must_change_password": agent.must_change_password,
            }
            return data

        # 3. Generic failure: never say which field was wrong.
        LoginAttempt.objects.create(username=username, ip=ip, success=False, kind=kind)
        raise AuthenticationFailed(detail="Invalid credentials.")

    def _match_agent(self, username, password):
        """Resolve an agent by shared username + per-agent password.

        Every candidate's password is verified — no early exit — so the
        response time does not reveal which candidate matched (spec §5.4).
        Returns ``(agent_or_None, audit_kind)``.
        """
        candidates = list(
            SalesAgent.objects.filter(
                login_username__iexact=username, is_active=True
            ).select_related("user", "tenant")
        )
        match = None
        for agent in candidates:
            if agent.user is not None and agent.user.check_password(password):
                match = match or agent
        return match, ("AGENT" if candidates else "STAFF")

    def _success_data(self, access=None):
        refresh = self.get_token(self.user)
        data = {"refresh": str(refresh), "access": str(refresh.access_token)}
        profile = getattr(self.user, "profile", None)
        data["user"] = {
            "id": self.user.id,
            "username": self.user.username,
            "name": self.user.get_full_name() or self.user.username,
            "role": profile.role if profile else "",
            "tenant": profile.tenant.name if profile and profile.tenant else "",
            "tenant_id": profile.tenant_id if profile else None,
            "must_change_password": profile.must_change_password if profile else False,
        }
        # An ended subscription still hands the manager a token, but only for
        # the payment portal; every other role was denied above.
        data["access_mode"] = access["access_mode"] if access else "full"
        data["payment_kind"] = access["payment_kind"] if access else None
        data["access_message"] = access["message"] if access else ""
        return data


class UserSerializer(serializers.ModelSerializer):
    role = serializers.CharField(source="profile.role", read_only=True)
    tenant = serializers.CharField(source="profile.tenant.name", read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "tenant"]
