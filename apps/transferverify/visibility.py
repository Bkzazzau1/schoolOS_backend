from .services import can_classify


def bad_debt_classification_visible_payload(membership, payload):
    """A school's own bad debt classifications are seen only by whoever may
    classify them (the Proprietor, or a Finance delegate holding the
    finance.bad_debt_classification duty) - never by every membership at the
    school, and never by any other school. Nothing here is discoverable
    through TransferVerify; that only ever happens through the separate,
    later, platform-level publish action."""
    if can_classify(membership):
        return payload
    return None
