from django.apps import AppConfig


class MandatesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mandates"
    label = "mandates"
    verbose_name = "SchoolOS Mandates & Direct Debit - payer mandates, approved debits"
