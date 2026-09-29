from django.apps import AppConfig


class MediaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.media"
    label = "schoolos_media"  # "media" alone collides with Django's own media-handling vocabulary in log/admin output
    verbose_name = "SchoolOS Media & Files - the one canonical file/attachment service every feature reuses"
