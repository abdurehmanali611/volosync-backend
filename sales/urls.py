from django.urls import path

from .views import (
    OpenAssignmentListView,
    SalesAgentChangePasswordView,
    SalesAgentListCreateView,
    SalesAgentLoginView,
    SalesAgentMeView,
    SalesAgentRankingView,
    SettlementDecisionView,
    SettlementListCreateView,
)

urlpatterns = [
    path("open-assignments/", OpenAssignmentListView.as_view()),
    path("agents/", SalesAgentListCreateView.as_view()),
    path("agents/login/", SalesAgentLoginView.as_view()),
    path("agents/me/", SalesAgentMeView.as_view()),
    path("agents/ranking/", SalesAgentRankingView.as_view()),
    path("agents/change-password/", SalesAgentChangePasswordView.as_view()),
    path("settlements/", SettlementListCreateView.as_view()),
    path("settlements/<int:pk>/decision/", SettlementDecisionView.as_view()),
]
