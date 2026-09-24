from django.core.management.base import BaseCommand, CommandError

from apps.students.account_provisioning import provision_registration_accounts
from apps.students.models import RegistrationStatus, StudentRegistration
from apps.sync.models import SyncRecord


class Command(BaseCommand):
    help = "Provision Student/Parent login accounts for existing canonical Active registrations."

    def handle(self, *args, **options):
        provisioned = 0
        skipped = 0
        errors = []

        registrations = (
            StudentRegistration.objects.filter(
                status=RegistrationStatus.ACTIVE,
                student__isnull=False,
            )
            .select_related("student", "school")
            .order_by("school_id", "registration_id")
        )
        for registration in registrations.iterator():
            student = registration.student
            guardian = student.guardians.filter(
                phone=registration.guardian_phone,
            ).first()
            if guardian is None:
                skipped += 1
                errors.append(
                    f"{registration.school_id}/{registration.registration_id}: primary guardian link not found"
                )
                continue
            try:
                credentials = provision_registration_accounts(
                    registration,
                    student,
                    guardian,
                )
                sync_record = SyncRecord.objects.filter(
                    school=registration.school,
                    entity_type="student_registration",
                    entity_id=registration.registration_id,
                    deleted=False,
                ).first()
                if sync_record is not None:
                    payload = dict(sync_record.payload)
                    payload.update(
                        {
                            "admissionNumber": registration.admission_number,
                            "studentId": registration.student_code,
                            "canonicalActive": True,
                            "canonicalStudentId": str(student.id),
                            "credentialsProvisioned": True,
                            **credentials,
                        }
                    )
                    # This is server-owned enrichment of an already accepted
                    # registration, not a new client mutation.
                    SyncRecord.objects.filter(pk=sync_record.pk).update(payload=payload)
                provisioned += 1
            except Exception as exc:  # continue other schools, fail command at end
                errors.append(
                    f"{registration.school_id}/{registration.registration_id}: {str(exc)[:180]}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Provisioned or confirmed {provisioned} existing Student/Parent account pairs."
            )
        )
        if skipped:
            self.stdout.write(f"Skipped {skipped} registrations with missing guardian links.")
        if errors:
            raise CommandError(
                "Some registrations could not be provisioned:\n" + "\n".join(errors[:30])
            )
