from django.contrib import admin

from .models import Customer, CustomerContact


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
