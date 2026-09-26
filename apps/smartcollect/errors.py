from apps.core.errors import Rejected


class CollectionRefused(Rejected):
    """A change Smart Money Collection will not make, with words the person can act on and a short stable `code` the app can switch on.
    It is a normal outcome, not a crash."""

    def __init__(self, message: str, code: str = "collection_error", **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        #: Facts the app can act on (for example the current hash of a batch a screen has gone stale on).
        self.extra = extra
