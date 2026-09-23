from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class RegistrationThrottle(AnonRateThrottle):
    """Keep public account creation bounded independently of other anonymous API use."""

    rate = "5/min"


class EmailVerificationSendThrottle(UserRateThrottle):
    """Prevent verification-email spam from an authenticated account."""

    rate = "3/hour"


class EmailVerificationConfirmThrottle(UserRateThrottle):
    """Bound online guessing independently of the per-code attempt counter."""

    rate = "10/min"
