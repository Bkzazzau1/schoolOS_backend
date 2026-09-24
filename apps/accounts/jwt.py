from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class SchoolOSJWTAuthentication(JWTAuthentication):
    """JWT authentication with first-login and credential-revocation guards."""

    _BOOTSTRAP_PATHS = {
        "/api/v1/me/",
        "/api/v1/auth/password/initial-change/",
    }

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, validated_token = result

        # Tokens issued before credential-version support are version 1. That
        # keeps rollout backward-compatible while still allowing a later reset
        # or phone-login change to revoke them by incrementing the user version.
        token_version = int(validated_token.get("cv", 1))
        if token_version != user.credential_version:
            raise AuthenticationFailed(
                "This SchoolOS session was replaced. Sign in again with your current credentials."
            )

        if user.must_change_password and request.path not in self._BOOTSTRAP_PATHS:
            raise AuthenticationFailed(
                "Create a private password before using this SchoolOS account."
            )
        return user, validated_token
