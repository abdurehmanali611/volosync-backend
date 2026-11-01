from django.urls import path

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
]
