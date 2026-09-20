from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.schools.models import Membership, Role, School


class Command(BaseCommand):
    help = "Create a school and make a person its owner (proprietor)."

    def add_arguments(self, parser):
        parser.add_argument("name", help="The school's name, in quotes.")
        parser.add_argument("--owner-email", required=True)
        parser.add_argument("--slug", help="Short unique name. Defaults to the school name.")

    def handle(self, *args, name, owner_email, slug=None, **options):
        slug = slug or slugify(name)
        if not slug:
            raise CommandError("Could not make a slug from that name. Pass --slug.")
        if School.objects.filter(slug=slug).exists():
            raise CommandError(f"A school with the slug '{slug}' already exists.")

        User = get_user_model()
        email = owner_email.strip().lower()
        with transaction.atomic():
            user = User.objects.filter(email=email).first()
            created = user is None
            if created:
                # No password yet: they cannot sign in until one is set.
                user = User.objects.create_user(email)
            school = School.objects.create(name=name.strip(), slug=slug)
            Membership.objects.create(user=user, school=school, role=Role.PROPRIETOR)

        self.stdout.write(self.style.SUCCESS(f"Created {school.name} (id {school.id})."))
        primary = school.domains.filter(is_primary=True).first()  # given by apps.domains
        if primary:
            self.stdout.write(f"Its web address is {primary.host}.")
        else:
            self.stdout.write(
                "It has no web address yet (no platform domain is set, or its short name "
                "is reserved). Fix that in the admin under Domains."
            )
        if created:
            self.stdout.write(
                f"Created the account {email} with no password. "
                f"Set one with: python manage.py changepassword {email}"
            )
        else:
            self.stdout.write(f"{email} is now its owner.")
