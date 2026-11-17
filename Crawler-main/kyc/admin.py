from django.contrib import admin

from .models import Customer, CustomerContact, Department, DepartmentContact, KycAuditLog, UserDepartment


class CustomerContactInline(admin.TabularInline):
    model = CustomerContact
    extra = 0
    fields = ("category", "name", "designation", "email", "phone", "is_purchaser")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "city", "kyc_status", "updated_at")
    search_fields = ("code", "name", "gstin", "contacts__email")
    list_filter = ("kyc_status",)
    inlines = [CustomerContactInline]


class DepartmentContactInline(admin.TabularInline):
    model = DepartmentContact
    extra = 1


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "sort_order")
    inlines = [DepartmentContactInline]

    def has_delete_permission(self, request, obj=None):
        return False  # departments are switched off, never deleted


@admin.register(UserDepartment)
class UserDepartmentAdmin(admin.ModelAdmin):
    list_display = ("user", "department")


@admin.register(KycAuditLog)
class KycAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "username", "department", "action", "customer_label", "field_changed")
    list_filter = ("action", "department")
    search_fields = ("username", "customer_label")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
