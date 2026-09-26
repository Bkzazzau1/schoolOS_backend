"""Where a provider credential and a payer's bank account rest: sealed before they reach the database, opened only inside the server, never
handed to a client. The same key ring as the rest of SchoolOS's sealed secrets (`BANKCONNECT_SECRET_KEYS`), with a context of its own, so a
blob copied from one row to another - or from another part of SchoolOS to a mandate - will not open."""

from apps.bankconnect.vault import SecretVault, VaultError, VaultNotConfigured, get_vault  # noqa: F401 - re-exported

DOMAIN = "mandates"


def context_for(connection) -> dict:
    """A provider connection's credential is bound to the school and the connection."""
    return {"domain": DOMAIN, "kind": "provider_credentials", "school": str(connection.school_id), "connection": str(connection.id)}


def account_context_for(mandate) -> dict:
    """A payer's bank account is bound to the school and the mandate it was given for."""
    return {"domain": DOMAIN, "kind": "payer_account", "school": str(mandate.school_id), "mandate": str(mandate.id)}
