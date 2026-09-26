from apps.core.errors import Rejected


class Refused(Rejected):
    """A change the receivables domain will not make, with words the person can act on and a short
    stable `code` the app can switch on. It is a normal outcome, not a crash."""

    def __init__(self, message: str, code: str = "receivables_error"):
        super().__init__(message)
        self.code = code
