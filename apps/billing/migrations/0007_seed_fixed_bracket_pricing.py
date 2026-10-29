from django.db import migrations
from django.utils import timezone

# The commercial table authorized in Phase C's final pricing decision: a fixed fee per school
# per academic term, by enrollment bracket. Minor currency units (kobo). Never per-student.
APPROVED_BRACKETS = [
    (1, 100, 4_000_000),
    (101, 300, 7_500_000),
    (301, 600, 12_000_000),
    (601, 1_000, 18_000_000),
    (1_001, 2_000, 25_000_000),
]


def seed_brackets(apps, schema_editor):
    PricingBracket = apps.get_model("billing", "PricingBracket")
    if PricingBracket.objects.exists():
        return
    now = timezone.now()
    PricingBracket.objects.bulk_create(
        [
            PricingBracket(
                min_students=min_students,
                max_students=max_students,
                fixed_fee_minor=fixed_fee_minor,
                currency="NGN",
                effective_from=now,
                effective_until=None,
            )
            for min_students, max_students, fixed_fee_minor in APPROVED_BRACKETS
        ]
    )


def remove_seeded_brackets(apps, schema_editor):
    PricingBracket = apps.get_model("billing", "PricingBracket")
    PricingBracket.objects.filter(
        min_students__in=[row[0] for row in APPROVED_BRACKETS],
        max_students__in=[row[1] for row in APPROVED_BRACKETS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0006_pricingauditevent_enterpriseagreement_pricingbracket"),
    ]

    operations = [
        migrations.RunPython(seed_brackets, remove_seeded_brackets),
    ]
