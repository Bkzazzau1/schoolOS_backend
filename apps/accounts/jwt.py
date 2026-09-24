from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class SchoolOSJWTAuthentication(JWTAuthentication):
    """JWT authentication with a narrow first-login bootstrap state.

    School-provisioned Student/Parent accounts initially know a predictable
    first-name password. After that password authenticates once, the token may
    only read `/me/` and replace the bootstrap password. All normal school APIs
    remain unavailable until the replacement succeeds.
    """

    _BOOTSTRAP_PATHS = {
        "/api/v1/me/",
        "/api/v1/auth/password/initial-change/",
    }

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, validated_token = result
        if user.must_change_password and request.path not in self._BOOTSTRAP_PATHS:
            raise AuthenticationFailed(
                "Create a private password before using this SchoolOS account."
            )
        return user, validated_token
