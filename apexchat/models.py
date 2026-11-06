"""Read/write mirrors of the Apex console's chat tables.

Both projects point at one MySQL database and the console owns this schema, so
the models are unmanaged and this app deliberately ships no `migrations`
package: unmigrated apps are invisible to `makemigrations`, which means nothing
here can ever try to create or alter a table the console already made.

Field names therefore have to stay identical to the console's
`tenants.ChatThread` / `tenants.ChatMessage`, column for column.
"""

from django.conf import settings
from django.db import models


class ApexChatThread(models.Model):
    """The property's single running conversation with the Apex team."""

    STATUS = (("OPEN", "Open"), ("CLOSED", "Closed"))

    tenant = models.OneToOneField(
        "tenants.Tenant",
        on_delete=models.DO_NOTHING,
        related_name="apex_chat",
        db_column="tenant_id",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="OPEN")
    subject = models.CharField(max_length=200, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="apex_chat_threads_opened",
        db_column="opened_by_id",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        related_name="apex_chat_threads_closed",
        db_column="closed_by_id",
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "tenants_chatthread"
        ordering = ["-updated_at"]

    def __str__(self):
        return f"apex chat · tenant {self.tenant_id}"


class ApexChatMessage(models.Model):
    """One bubble in the thread, from either side."""

    SIDES = (("APEX", "Apex"), ("TENANT", "Wholesaler"))

    thread = models.ForeignKey(
        ApexChatThread,
        on_delete=models.DO_NOTHING,
        related_name="messages",
        db_column="thread_id",
    )
    side = models.CharField(max_length=10, choices=SIDES)
    body = models.TextField(blank=True)
    image_url = models.CharField(max_length=2048, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
        db_column="actor_id",
    )
    read_by_apex = models.BooleanField(default=False)
    read_by_tenant = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "tenants_chatmessage"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.side} · thread {self.thread_id}"
