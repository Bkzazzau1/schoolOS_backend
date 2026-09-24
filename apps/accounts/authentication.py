from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework_simplejwt.tokens import RefreshToken

from .identity import resolve_login_user


class SchoolOSTokenSerializer(serializers.Serializer):
    """Authenticate email, student admission ID, or parent phone number."""

    identifier = serializers.CharField(required=False, allow_blank=False, write_only=True)
    # Backward compatibility for app versions that still send {email, password}.
    email = serializers.CharField(required=False, allow_blank=False, write_only=True)
    password = serializers.CharField(trim_whitespace=False, write_only=True)

    def validate(self, attrs):
        identifier = attrs.get("identifier") or attrs.get("email")
        if not identifier:
            raise AuthenticationFailed("Enter your SchoolOS login ID.")
        user = resolve_login_user(identifier)
        if user is None or not user.is_active or not user.check_password(attrs["password"]):
            raise AuthenticationFailed("The login ID or password is not correct.")

        refresh = RefreshToken.for_user(user)
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }


def complete_initial_password(user, new_password) -> None:
    if not user.must_change_password:
        return
    if not isinstance(new_password, str) or not new_password:
        raise ValidationError({"newPassword": "Enter a new password."})
    try:
        validate_password(new_password, user=user)
    except DjangoValidationError as exc:
        raise ValidationError({"newPassword": list(exc.messages)}) from exc

    user.set_password(new_password)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])
