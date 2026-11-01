import os
from decimal import Decimal
from uuid import uuid4

from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from inventory.models import next_voucher
from inventory.views import require_roles, tenant_of

from .models import Asset, AssetAssignment
from .serializers import AssetInputSerializer, AssetSerializer


class AssetListCreateView(APIView):
    def get(self, request):
        tenant = tenant_of(request.user)
        assets = Asset.objects.filter(tenant=tenant).prefetch_related("assignments")
        category = request.query_params.get("category")
        if category:
            assets = assets.filter(category=category)
        state = request.query_params.get("status")
        if state:
            assets = assets.filter(status=state)
        condition = request.query_params.get("condition")
        if condition:
            assets = assets.filter(condition_at_registration=condition)
        branch = request.query_params.get("branch")
        if branch:
            # Branches are child tenants (spec Q8): only the viewer's own
            # tenant or one of its children may be selected.
            from tenants.models import Tenant

            allowed = branch.isdigit() and (
                int(branch) == tenant.id
                or Tenant.objects.filter(pk=branch, parent=tenant).exists()
            )
            assets = assets.filter(tenant_id=branch) if allowed else assets.none()
        return Response({"data": AssetSerializer(assets, many=True, context={"request": request}).data})

    @require_roles("MANAGER")
    def post(self, request):
        tenant = tenant_of(request.user)
        serializer = AssetInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        asset = Asset.objects.create(
            tenant=tenant,
            asset_tag=data.get("asset_tag") or f"AST-{timezone.now().year}-{Asset.objects.filter(tenant=tenant).count() + 1:06d}",
            created_by=request.user,
            **{key: value for key, value in data.items() if key != "asset_tag"},
        )
        return Response({"data": AssetSerializer(asset, context={"request": request}).data}, status=201)


class AssetPhotoUploadView(APIView):
    """Optional asset photo: the browser uploads first, then sends the path."""

    @require_roles("MANAGER")
    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"errors": {"message": "Attach an image file."}}, status=400
            )
        if upload.size > 5 * 1024 * 1024:
            return Response(
                {"errors": {"message": "Keep the image under 5 MB."}}, status=400
            )
        extension = os.path.splitext(upload.name)[1].lower() or ".jpg"
        path = default_storage.save(f"assets/{uuid4().hex}{extension}", upload)
        return Response(
            {
                "data": {
                    "path": path,
                    "url": request.build_absolute_uri(default_storage.url(path)),
                }
            },
            status=201,
        )


class AssetDetailView(APIView):
    @require_roles("MANAGER")
    def patch(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        serializer = AssetInputSerializer(asset, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": AssetSerializer(asset, context={"request": request}).data})

    @require_roles("MANAGER")
    def delete(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        asset.delete()
        return Response({"data": {"id": int(pk), "deleted": True}})


class AssetAssignView(APIView):
    @require_roles("FINANCE")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        if asset.status == "ASSIGNED":
            return Response(
                {"errors": {"message": "This asset is already assigned."}}, status=400
            )
        assignee = (request.data.get("assignee_name") or "").strip()
        if not assignee:
            return Response(
                {"errors": {"assignee_name": "Type the assignee name."}}, status=400
            )
        expected_return = request.data.get("expected_return_date") or None
        AssetAssignment.objects.create(
            asset=asset,
            assignee_name=assignee,
            assigned_at=request.data.get("assigned_at") or timezone.localdate(),
            expected_return_date=expected_return,
            assigned_by=request.user,
        )
        asset.status = "ASSIGNED"
        asset.save(update_fields=["status"])
        return Response({"data": AssetSerializer(asset, context={"request": request}).data})


class AssetRemoveView(APIView):
    @require_roles("FINANCE")
    def post(self, request, pk):
        tenant = tenant_of(request.user)
        asset = Asset.objects.filter(tenant=tenant, pk=pk).first()
        if not asset:
            return Response({"errors": {"message": "Not found."}}, status=404)
        assignment = asset.assignments.filter(is_active=True).first()
        if not assignment:
            return Response(
                {"errors": {"message": "This asset is not assigned."}}, status=400
            )
        assignment.is_active = False
        assignment.removed_at = timezone.localdate()
        assignment.save(update_fields=["is_active", "removed_at"])
        asset.status = "AVAILABLE"
        asset.save(update_fields=["status"])
        return Response({"data": AssetSerializer(asset, context={"request": request}).data})
