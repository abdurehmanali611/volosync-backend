from django.urls import path
from .views import ChangePasswordView, MeView, StaffAccessDetailView, StaffAccessView
urlpatterns = [
    path("me/", MeView.as_view()),
    path("change_password/", ChangePasswordView.as_view()),
    path("staff/", StaffAccessView.as_view()),
    path("staff/<int:pk>/", StaffAccessDetailView.as_view()),
]
