import re

#: Must match `financeSyncSafeId` in the app's finance_ledger_models.dart exactly: both sides compute
#: the same sync entity id independently from the same free text (a term name, which can contain '/',
#: spaces and '·') - the sync system's own entity id pattern allows only letters, digits, '.', '_',
#: ':' and '-' (see apps/sync/serializers.py).
_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]")


def sync_safe_id(raw: str) -> str:
    return _UNSAFE.sub("-", raw)
