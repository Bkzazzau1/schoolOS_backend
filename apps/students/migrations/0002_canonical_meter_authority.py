from django.db import migrations


def deauthorize_legacy_school_meters(apps, schema_editor):
    meter = apps.get_model("billing", "SchoolBillingMeterSnapshot")
    meter.objects.exclude(source="canonical_student_roster").update(authoritative=False)


class Migration(migrations.Migration):
    dependencies = [
        ("students", "0001_initial"),
        ("billing", "0004_billing_cycle_policy_meter"),
    ]

    operations = [
        migrations.RunPython(
            deauthorize_legacy_school_meters,
            migrations.RunPython.noop,
        )
    ]
