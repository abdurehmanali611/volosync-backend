from django.urls import path

from .views import ApexChatView

urlpatterns = [
    path("", ApexChatView.as_view()),
]
