"""JWT authentication that also enforces the yearly subscription gate.

The login serializer already refuses to issue a token to a blocked role and
only hands a manager the payment-portal token. That is not enough on its own:
a token issued before the subscription ended stays valid for twelve hours, and
a manager parked on the payment portal could still call workspace endpoints
directly. HotCol enforces this at the resolver layer; this is the DRF
equivalent, so every authenticated request is checked against the same
``tenant_access`` verdict the login gate uses.

The payment portal only needs these endpoints, so they stay reachable while the
subscription is in the portal state.
"""

from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication

from .access import tenant_access

#: Paths the payment portal must be able to call while access is restricted.
PORTAL_PATH_PREFIXES = (
    "/api/tenants/subscription",
    "/api/tenants/me/",
    "/api/user/me/",
)


class SubscriptionAwareJWTAuthentication(JWTAuthentication):
    """Reject workspace calls when the tenant's subscription is not usable."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None

        user, _token = result
        path = request.path or ""
        if path.startswith(PORTAL_PATH_PREFIXES):
            return result

        profile = getattr(user, "profile", None)
        tenant_id = getattr(profile, "tenant_id", None)
        if not tenant_id:
            return result

        access = tenant_access(tenant_id, getattr(profile, "role", ""))
        if not access["allowed"] or access["access_mode"] != "full":
            raise PermissionDenied(
                access["message"] or "Your subscription is ended."
            )
        return result
