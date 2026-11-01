from django.contrib import admin

from .models import LoginAttempt, Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "role", "tenant", "must_change_password", "created_at"]
    list_filter = ["role", "tenant"]
    search_fields = ["user__username", "user__first_name", "user__last_name"]


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ["username", "kind", "success", "ip", "agent", "created_at"]
    list_filter = ["kind", "success"]
    search_fields = ["username", "ip"]
    readonly_fields = ["username", "ip", "success", "kind", "agent", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
