from django.db import migrations


def number_existing(apps, schema_editor):
    """Give records saved before change numbers existed one each, oldest first."""
    SyncRecord = apps.get_model("sync", "SyncRecord")
    SchoolSequence = apps.get_model("sync", "SchoolSequence")
    school_ids = SyncRecord.objects.values_list("school_id", flat=True).distinct()
    for school_id in school_ids:
        last = 0
        for record in SyncRecord.objects.filter(school_id=school_id).order_by("updated_at", "id"):
            last += 1
            SyncRecord.objects.filter(pk=record.pk).update(seq=last)
        SchoolSequence.objects.update_or_create(school_id=school_id, defaults={"last": last})


class Migration(migrations.Migration):

    dependencies = [("sync", "0002_change_numbers")]

    operations = [migrations.RunPython(number_existing, migrations.RunPython.noop)]
