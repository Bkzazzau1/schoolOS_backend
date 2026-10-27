from django.db import migrations


def rename_plan(apps, schema_editor):
    """0002_seed_standard_plan already ran, on any database created before this
    migration, with the old "SchoolOS Standard" name and description. Code
    "standard" is the stable lookup key; a fresh database seeds the real name
    directly now, so this is a no-op there."""
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(code="standard", name="SchoolOS Standard").update(
        name="SkulGen Standard",
        description="Baseline SkulGen plan for organization-managed schools.",
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0004_billing_cycle_policy_meter"),
    ]

    operations = [
        migrations.RunPython(rename_plan, noop_reverse),
    ]
