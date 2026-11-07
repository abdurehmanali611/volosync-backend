from django.urls import path

from billing.views import TenantPaymentSubmitView, TenantSubscriptionView

from .views import (
    BranchCreateView,
    TenantDetailView,
    TenantListView,
    TenantMeView,
)

urlpatterns = [
    path("", TenantListView.as_view()),
    path("<int:pk>/", TenantDetailView.as_view()),
    path("<int:pk>/branches/", BranchCreateView.as_view()),
    path("me/", TenantMeView.as_view()),
    path("subscription/", TenantSubscriptionView.as_view()),
    path("subscription/payment/", TenantPaymentSubmitView.as_view()),
]
