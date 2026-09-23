from rest_framework.throttling import AnonRateThrottle


class RegistrationThrottle(AnonRateThrottle):
    """Keep public account creation bounded independently of other anonymous API use."""

    rate = "5/min"
