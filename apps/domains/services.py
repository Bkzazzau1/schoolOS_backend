from collections.abc import Callable

from django.db import transaction
from django.utils import timezone

from apps.schools.models import School

from .hostnames import DomainError, normalize_host, platform_domain, platform_host_for
from .models import SchoolDomain

TxtResolver = Callable[[str], list[str]]


def ensure_platform_domain(school: School) -> SchoolDomain | None:
    """Give a school its platform subdomain, and make it primary if it has none.

    Safe to call again. Returns None when no platform domain is configured.
    Raises DomainError when the school's short name cannot be a web address.
    """
    if not platform_domain():
        return None
    host = platform_host_for(school.slug)
    with transaction.atomic():
        domain, _ = SchoolDomain.objects.get_or_create(
            host=host,
            defaults={
                "school": school,
                "kind": SchoolDomain.Kind.PLATFORM,
                "status": SchoolDomain.Status.VERIFIED,
                "verified_at": timezone.now(),
            },
        )
        if domain.school_id != school.id:
            raise DomainError(f"{host} already belongs to another school.")
        if not school.domains.filter(is_primary=True).exists():
            domain.is_primary = True
            domain.save(update_fields=["is_primary"])
    return domain


def add_custom_domain(school: School, host: str) -> SchoolDomain:
    """Register a domain the school owns. It stays pending until verified."""
    host = normalize_host(host)
    if SchoolDomain.objects.filter(host=host).exists():
        raise DomainError("That domain is already registered.")
    return SchoolDomain.objects.create(
        school=school, host=host, kind=SchoolDomain.Kind.CUSTOM, status=SchoolDomain.Status.PENDING
    )


def dns_txt_records(name: str) -> list[str]:
    """The TXT records published at `name`. Empty when there are none."""
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=5)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers, dns.exception.Timeout):
        return []
    return [b"".join(record.strings).decode("utf-8", "ignore") for record in answer]


def verify_custom_domain(domain: SchoolDomain, resolver: TxtResolver = dns_txt_records) -> SchoolDomain:
    """Mark a custom domain verified if its owner published the token in DNS."""
    if domain.kind != SchoolDomain.Kind.CUSTOM:
        raise DomainError("Only custom domains need verifying.")
    if domain.status == SchoolDomain.Status.DISABLED:
        raise DomainError("This domain is disabled.")
    if domain.status == SchoolDomain.Status.VERIFIED:
        return domain
    if domain.verification_token not in resolver(domain.dns_record_name):
        raise DomainError(
            f"We could not find the verification record yet. Add a TXT record named "
            f"{domain.dns_record_name} with the value {domain.verification_token}, "
            "then try again. DNS changes can take a while to appear."
        )
    domain.status = SchoolDomain.Status.VERIFIED
    domain.verified_at = timezone.now()
    domain.save(update_fields=["status", "verified_at"])
    return domain


def set_primary(domain: SchoolDomain) -> SchoolDomain:
    """Use this domain in the school's emailed links."""
    domain.refresh_from_db(fields=["status"])
    if domain.status != SchoolDomain.Status.VERIFIED:
        raise DomainError("Only a verified domain can be the primary domain.")
    with transaction.atomic():
        SchoolDomain.objects.filter(school=domain.school, is_primary=True).exclude(pk=domain.pk).update(
            is_primary=False
        )
        domain.is_primary = True
        domain.save(update_fields=["is_primary"])
    return domain


def disable(domain: SchoolDomain) -> SchoolDomain:
    # Decide from what is stored now, not from a copy that may be out of date.
    domain.refresh_from_db(fields=["is_primary", "status"])
    if domain.is_primary:
        raise DomainError("Make another domain primary before disabling this one.")
    domain.status = SchoolDomain.Status.DISABLED
    domain.save(update_fields=["status"])
    return domain


def resolve_host(host: str) -> SchoolDomain | None:
    """The verified, enabled domain for this Host header, or None.

    Only verified domains of active schools resolve. A pending or disabled domain
    behaves as if it did not exist.
    """
    try:
        host = normalize_host(host)
    except DomainError:
        return None
    return (
        SchoolDomain.objects.select_related("school")
        .filter(host=host, status=SchoolDomain.Status.VERIFIED, school__is_active=True)
        .first()
    )


def link_host(school: School) -> str | None:
    """The host to put in this school's emailed links: its primary domain."""
    primary = school.domains.filter(is_primary=True, status=SchoolDomain.Status.VERIFIED).first()
    return primary.host if primary else None
