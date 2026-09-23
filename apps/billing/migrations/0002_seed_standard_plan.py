from django.db import migrations


def seed_standard_plan(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanEntitlement = apps.get_model("billing", "PlanEntitlement")
    OrganizationSubscription = apps.get_model("billing", "OrganizationSubscription")
    Organization = apps.get_model("organizations", "Organization")

    plan, _ = Plan.objects.get_or_create(
        code="standard",
        defaults={
            "name": "SchoolOS Standard",
            "description": "Baseline SchoolOS plan for organization-managed schools.",
            "currency": "NGN",
            "billing_interval": "",
            "base_amount_minor": 0,
            "student_unit_amount_minor": 50_000,
            "is_public": True,
            "is_active": True,
            "sort_order": 10,
        },
    )
    for code in ("school_provisioning", "multi_school"):
        PlanEntitlement.objects.get_or_create(
            plan=plan,
            code=code,
            defaults={"enabled": True, "limit_value": None, "metadata": {}},
        )

    for organization in Organization.objects.filter(is_active=True).iterator():
        OrganizationSubscription.objects.get_or_create(
            organization=organization,
            defaults={"plan": plan, "status": "active"},
        )


def noop_reverse(apps, schema_editor):
    # Commercial history should not be destroyed on a schema rollback.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_standard_plan, noop_reverse),
    ]
