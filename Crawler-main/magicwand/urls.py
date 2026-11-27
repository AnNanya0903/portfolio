"""
URL configuration for MagicWand project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
"""
from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.http import JsonResponse
from django.conf import settings
from django.conf.urls.static import static
from . import views
from compounding import iot_views as compounding_iot_views

def healthcheck(request):
    """Health check endpoint for load balancers."""
    return JsonResponse({'status': 'healthy', 'service': 'magicwand'})

urlpatterns = [
    # Admin
    path('admin/', admin.site.urls),
    
    # Authentication
    path('accounts/', include('allauth.urls')),
    path('accounts/logout/', views.logout_view, name='account_logout'),
    path('dev-login/', views.dev_login, name='dev_login'),
    
    # Main application routes
    path('', views.home, name='home'),
    path('dashboard/', include('dashboard.urls')),
    path('hub/', views.dashboard_hub, name='dashboard_hub'),
    path('ai-portal/action/', views.ai_portal_action, name='ai_portal_action'),
    path('gmail-ai/', views.gmail_ai, name='gmail_ai'),
    path('gmail-ai/search/', views.gmail_ai_search, name='gmail_ai_search'),
    path('gmail-ai/email/<str:message_id>/', views.gmail_ai_email, name='gmail_ai_email'),
    path('api/iot-ingest/', views.iot_ingest, name='iot_ingest'),
    # Lab result for an IoT batch (compounding mixing mill).
    path('api/batches/<int:batch_id>/lab-result/', compounding_iot_views.lab_result_api, name='iot_batch_lab_result'),
    path('api/iot-dashboard-data/', views.iot_dashboard_data, name='iot_dashboard_data'),
    path('api/iot-dashboard-summary/', views.iot_dashboard_summary, name='iot_dashboard_summary'),
    path('api/iot-history-export/', views.iot_history_export, name='iot_history_export'),
    path('iot-dashboard/', views.iot_dashboard, name='iot_dashboard'),
    path('cad/', views.cad_library, name='cad_library'),
    path('design-library/', views.design_library_folder, name='design_library_folder'),
    path('design-library/upload/prepare/', views.design_library_prepare_upload, name='design_library_prepare_upload'),
    path('design-library/download/<path:file_path>/', views.design_library_download, name='design_library_download'),
    path('enquiries/', include('enquiries.urls')),
    path('marketing/', include('marketing.urls')),
    path('kyc/', include('kyc.urls')),
    path('quotations/', include('quotations.urls')),
    path('compounding/', include('compounding.urls')),
    path('discussion/', include('discussion.urls')),
    path('documents/', include('docscanner.urls')),
    path('gasket-counter/', include('gasket_counter.urls')),
    path('push/', include('push_notifications.urls')),
    path('accounts-portal/', views.accounts_portal, name='accounts_portal'),
    # Accounts microservice proxy (supports both with and without trailing slash)
    path('accounts-service', views.accounts_service_proxy, name='accounts_proxy_root'),
    path('accounts-service/', views.accounts_service_proxy),
    path('accounts-service/<path:subpath>', views.accounts_service_proxy, name='accounts_proxy'),
    path('deskera/', views.deskera_portal, name='deskera_portal'),
    path('mrp/', include('mrp.urls')),
    path('deskera/mtc/', views.deskera_mtc_generator, name='deskera_mtc_generator'),
    path('deskera/mtc/template/', views.deskera_mtc_template_download, name='deskera_mtc_template_download'),
    path('deskera/workorders/', views.deskera_workorders, name='deskera_workorders'),
    path('deskera/workorders/mtc/', views.deskera_workorder_mtc, name='deskera_workorder_mtc'),
    path('deskera/workorders/copilot-status/', views.deskera_workorders_copilot_status, name='deskera_workorders_copilot_status'),
    path('deskera/workorders/summary/', views.deskera_workorders_summary, name='deskera_workorders_summary'),
    path('deskera/workorders/chat/', views.deskera_workorders_chat, name='deskera_workorders_chat'),
    path('deskera/workstations/', views.deskera_workstations, name='deskera_workstations'),
    path('deskera/workstations/chat/', views.deskera_workstations_chat, name='deskera_workstations_chat'),
    path('deskera/jobcards/', views.deskera_jobcards, name='deskera_jobcards'),
    path('deskera/jobcards/copilot-status/', views.deskera_jobcards_copilot_status, name='deskera_jobcards_copilot_status'),
    path('deskera/jobcards/summary/', views.deskera_jobcards_summary, name='deskera_jobcards_summary'),
    path('deskera/jobcards/chat/', views.deskera_jobcards_chat, name='deskera_jobcards_chat'),
    path('kanban/', views.deskera_kanban, name='kanban_portal'),
    path('kanban/', views.deskera_kanban, name='deskera_kanban'),
    path('settings', views.magicwand_settings, name='settings_portal'),
    path('settings/', views.magicwand_settings),
    path('_ah/warmup', views.app_engine_warmup, name='app_engine_warmup'),
    path('internal/warmup/deskera/', views.internal_deskera_warmup, name='internal_deskera_warmup'),

    # Microservice routes (will be integrated later)
    # path('kanban/', include('microservices.kanban_service.urls')),
    # path('cad/', include('microservices.cad_service.urls')),
    # path('enquiries/', include('microservices.enquiry_service.urls')),
    # path('accounts-service/', include('microservices.accounts_service.urls')),
    
    # Health check
    path('healthz/', healthcheck, name='healthz'),
    
    # API routes for frontend
    path('api/', include([
        # path('auth/', include('microservices.auth_service.urls')),
        path('gmail/', include('dashboard.api_urls')),
        # path('kanban/', include('microservices.kanban_service.api_urls')),
        # path('cad/', include('microservices.cad_service.api_urls')),
        # path('enquiries/', include('microservices.enquiry_service.api_urls')),
    ])),
    
    # Auth check endpoint
    path('api/auth/check/', views.auth_check, name='auth_check'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
