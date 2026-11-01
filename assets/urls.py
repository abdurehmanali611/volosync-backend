from django.urls import path

from .views import (
    AssetAssignView,
    AssetDetailView,
    AssetListCreateView,
    AssetPhotoUploadView,
    AssetRemoveView,
)

urlpatterns = [
    path("", AssetListCreateView.as_view()),
    path("uploads/", AssetPhotoUploadView.as_view()),
    path("<int:pk>/", AssetDetailView.as_view()),
    path("<int:pk>/assign/", AssetAssignView.as_view()),
    path("<int:pk>/remove/", AssetRemoveView.as_view()),
]
