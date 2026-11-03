from django.conf import settings
from django.db import models

from tenants.models import Tenant


class Profile(models.Model):
    ROLE_CHOICES = (
        ("STORE", "Store"),
        ("FINANCE", "Finance"),
        ("MANAGER", "Manager"),
        ("HR", "HR"),
        ("SALES", "Sales"),
        ("OWNER", "Owner"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="staff"
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="STORE")
    phone = models.CharField(max_length=40, blank=True)
    must_change_password = models.BooleanField(default=False)
    # db_default keeps the shared table writable from the admin console,
    # whose ORM does not know about this volosync-only flag.
    access_password_used = models.BooleanField(default=False, db_default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class NoticeAck(models.Model):
    """Per-user read watermark for a synthesised bell notification.

    Queue notices are recomputed on every poll, so acknowledging stores the
    count that was read; the notice turns unread again once the queue grows.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notice_acks"
    )
    key = models.CharField(max_length=80)
    count = models.PositiveIntegerField(default=0)
    read_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "key"], name="unique_notice_ack")
        ]

    def __str__(self):
        return f"{self.user_id} · {self.key} · {self.count}"


class LoginAttempt(models.Model):
    """R2 guardrail: an audit row for every login attempt, staff or agent."""

    KINDS = (("STAFF", "Staff"), ("AGENT", "Agent"))

    username = models.CharField(max_length=150)
    ip = models.CharField(max_length=45, blank=True)
    success = models.BooleanField(default=False)
    kind = models.CharField(max_length=10, choices=KINDS, default="STAFF")
    agent = models.ForeignKey(
        "sales.SalesAgent",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="login_attempts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        verdict = "ok" if self.success else "failed"
        return f"{self.username} ({self.kind}) {verdict}"
