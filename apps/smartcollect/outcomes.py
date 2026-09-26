"""What a unit of provider work reports back to the job that ran it."""

from dataclasses import dataclass

DONE, RETRY, FAILED = "done", "retry", "failed"


@dataclass(frozen=True)
class Outcome:
    result: str
    code: str = ""
