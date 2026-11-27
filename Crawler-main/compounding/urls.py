from django.urls import path

from . import views

app_name = "compounding"

urlpatterns = [
    path("", views.board, name="board"),
    path("reports/", views.production_report, name="production_report"),
    path("reports/export/", views.production_report_csv, name="production_report_csv"),
    path("work/", views.work_queue, name="work_queue"),
    path("record/", views.production_record, name="production_record"),
    path("record/save/", views.save_record_grid, name="save_record_grid"),
    path("record/download/", views.download_production_record, name="download_production_record"),
    path("record/scrap/", views.save_record_scrap, name="save_record_scrap"),
    path("record/summary/", views.compound_record_summary, name="compound_record_summary"),
    path("record/import/", views.import_production_record, name="import_production_record"),
    path("production/", views.production_portal, name="production"),
    path("production/raise/", views.raise_crin_request, name="raise_crin_request"),
    path("stock/", views.stock_ledger, name="stock_ledger"),
    path("issue/", views.fb_issue_page, name="fb_issue"),
    path("issue/<int:crin_id>/", views.issue_fb, name="issue_fb"),
    path("iot-dashboard/", views.iot_dashboard, name="iot_dashboard"),
    path("iot-dashboard/run/save/", views.save_iot_run, name="save_iot_run"),
    path("iot-dashboard/batch/start/", views.start_iot_batch, name="start_iot_batch"),
    path("iot-dashboard/batch/end/", views.end_iot_batch, name="end_iot_batch"),
    # Called by App Engine cron every minute (cron.yaml): store new IoT samples and detect batches.
    path("api/cron/iot-sync/", views.iot_cron_sync, name="iot_cron_sync"),
    # Weekly (cron.yaml): rebuild standard curves from new normal batches.
    path("api/cron/iot-standards/", views.iot_cron_standards, name="iot_cron_standards"),
    path("planning/", views.planning, name="planning"),
    path("board/", views.display, name="display"),
    path("planning/crin/", views.create_crin, name="create_crin"),
    path("planning/crin/<int:crin_id>/apply/", views.apply_crin, name="apply_crin"),
    path("planning/crin/<int:crin_id>/delete/", views.delete_crin, name="delete_crin"),
    path("planning/fb/", views.fb_planning_page, name="fb_planning"),
    path("planning/mb/", views.mb_planning_page, name="mb_planning"),
    path("planning/master-sheet/upload/", views.upload_master_sheet, name="upload_master_sheet"),
    path("planning/master-sheet/save/", views.save_master_sheet, name="save_master_sheet"),
    path("planning/fb/set/", views.fb_planning_set, name="fb_planning_set"),
    path("planning/mb/set/", views.mb_planning_set, name="mb_planning_set"),
    path("planning/mb/send/", views.mb_send_to_board, name="mb_send_to_board"),
    path("planning/crin/<int:crin_id>/send/", views.send_to_board, name="send_to_board"),
    path("board/actuals/set/", views.set_actuals, name="set_actuals"),
    path("board/remaining/send/", views.send_remaining_to_planning, name="send_remaining_to_planning"),
    path("save/", views.save, name="save"),
]
