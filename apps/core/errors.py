class Rejected(Exception):
    """A change the server refuses, with a reason the app can show the user.

    Feature handlers raise this for anything that is not allowed or not valid.
    It is a normal outcome (the app receives a 422 `rejected`), not a crash.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
