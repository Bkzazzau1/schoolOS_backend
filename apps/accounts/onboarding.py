"""Self-service creation of a SchoolOS proprietor account.

Registration deliberately creates only the person and the commercial account.
The first school is provisioned afterwards through the organization school
endpoint, keeping account identity separate from tenant creation.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from apps.organizations.services import create_organization

User = get_user_model()


def _required_text(value, *, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError({"message": f"Enter {label}."})
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError({"message": f"Enter {label}."})
    if len(cleaned) > max_length:
        raise ValidationError({"message": f"{label.capitalize()} is too long."})
    return cleaned


@transaction.atomic
def register_proprietor_account(
    *,
    first_name,
    last_name,
    email,
    password,
    organization_name,
):
    """Create one person and their first organization owner membership atomically."""

    clean_first_name = _required_text(first_name, label="your first name", max_length=150)
    clean_last_name = _required_text(last_name, label="your last name", max_length=150)
    clean_organization_name = _required_text(
        organization_name,
        label="your school or organization name",
        max_length=200,
    )

    if not isinstance(email, str):
        raise ValidationError({"message": "Enter your email address."})
    clean_email = email.strip().lower()
    try:
        validate_email(clean_email)
    except DjangoValidationError as problem:
        raise ValidationError({"message": "Enter a valid email address."}) from problem

    if User.objects.filter(email__iexact=clean_email).exists():
        raise ValidationError(
            {"message": "An account with this email already exists. Sign in instead."}
        )

    if not isinstance(password, str):
        raise ValidationError({"message": "Choose a password."})

    candidate = User(
        email=clean_email,
        first_name=clean_first_name,
        last_name=clean_last_name,
    )
    try:
        validate_password(password, candidate)
    except DjangoValidationError as problem:
        raise ValidationError(
            {
                "message": "Choose a stronger password.",
                "password": list(problem.messages),
            }
        ) from problem

    # The inner savepoint lets us turn a concurrent unique-email collision into
    # a normal validation response without leaving the outer onboarding
    # transaction unusable.
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                clean_email,
                password,
                first_name=clean_first_name,
                last_name=clean_last_name,
            )
    except IntegrityError as problem:
        raise ValidationError(
            {"message": "An account with this email already exists. Sign in instead."}
        ) from problem

    organization, organization_membership = create_organization(
        actor=user,
        name=clean_organization_name,
    )
    return user, organization, organization_membership
