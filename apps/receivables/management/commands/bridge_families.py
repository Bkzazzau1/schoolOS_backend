from django.core.management.base import BaseCommand, CommandError

from apps.receivables import bridge
from apps.schools.models import School


class Command(BaseCommand):
    help = (
        "Bring the older free-text family references (family_account_ref) into the canonical Family model. "
        "Without --apply this only REPORTS what it would do. Students are grouped only where they share exactly "
        "the same explicit reference; everyone else is left alone (or, with --singletons, given a family of "
        "their own). It never guesses from names and never merges existing families."
    )

    def add_arguments(self, parser):
        parser.add_argument("--school", help="Only this school (its slug). Default: every active school.")
        parser.add_argument("--apply", action="store_true", help="Make the families. Without this, report only.")
        parser.add_argument("--singletons", action="store_true", help="Give each student with no usable reference a family of their own.")

    def handle(self, *args, **options):
        schools = School.objects.filter(is_active=True)
        if options["school"]:
            schools = schools.filter(slug=options["school"])
            if not schools.exists():
                raise CommandError("No such school.")
        for school in schools:
            report = bridge.apply(school, singletons=options["singletons"]) if options["apply"] else bridge.analyse(school)
            self.stdout.write(self.style.MIGRATE_HEADING(f"{school.name}"))
            self.stdout.write(f"  already in a family: {report.already_in_family}")
            for group in report.groups:
                state = "family exists" if group.existing else "would make a family"
                self.stdout.write(f"  reference '{group.reference}': {len(group.students)} student(s), {state}")
            for student, references in report.conflicts:
                self.stdout.write(self.style.WARNING(f"  conflict, left alone: {student.student_code} names {references}"))
            self.stdout.write(f"  no usable reference: {len(report.without_reference)} (sibling hints: {len(report.sibling_hints)})")
            if options["apply"]:
                self.stdout.write(self.style.SUCCESS(f"  made {report.families_created} family(ies), linked {report.students_linked} student(s)"))
        if not options["apply"]:
            self.stdout.write("Report only. Nothing was changed. Run again with --apply to make the families.")
