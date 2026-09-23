# SchoolOS SaaS account layer

## Purpose

SchoolOS keeps **school tenancy** and **commercial/account ownership** separate.

- `organizations.Organization` is the customer/account above one or more schools.
- `organizations.OrganizationMembership` says who can administer that account.
- `schools.School` remains the strict operational tenant boundary.
- `schools.Membership` remains the authority for acting inside a school.

An organization owner is therefore not automatically a teacher, finance officer,
principal or other operational role in every school.

## Hierarchy

```text
User
 ├─ OrganizationMembership
 │    └─ Organization
 │         ├─ School A
 │         ├─ School B
 │         └─ School C
 │
 └─ School Membership(s)
      └─ operational role inside one tenant
```

Existing schools created before this layer may have `organization = NULL`. This is
intentional for a safe migration path. New schools created through the SaaS
provisioning endpoint always belong to an organization.

## Organization roles

- `owner` — full account ownership; can create schools.
- `administrator` — account administration; can create schools.
- `billing_administrator` — reserved for billing/account finance; cannot create
  schools in this phase.

These values are distinct from `schools.Role`.

## Authenticated profile

`GET /api/v1/me/` returns the existing `memberships` plus account-level
`organizations`.

Example:

```json
{
  "id": "USER_UUID",
  "email": "owner@example.com",
  "name": "School Owner",
  "memberships": [
    {
      "id": "SCHOOL_MEMBERSHIP_UUID",
      "schoolId": "SCHOOL_UUID",
      "schoolName": "Bright Future Academy",
      "role": "proprietor",
      "organizationId": "ORG_UUID"
    }
  ],
  "organizations": [
    {
      "id": "ORG_MEMBERSHIP_UUID",
      "organizationId": "ORG_UUID",
      "organizationName": "Bright Future Schools",
      "role": "owner"
    }
  ]
}
```

A legitimate account owner may have an organization membership before the first
school exists.

## API

### List/create organizations

`GET /api/v1/organizations/`

Returns the signed-in person's active organization memberships.

`POST /api/v1/organizations/`

```json
{
  "name": "Bright Future Schools"
}
```

Creation is transactional and creates both the organization and the caller's
`owner` membership, plus an audit event.

### List/create schools under an organization

`GET /api/v1/organizations/{organization_id}/schools/`

Any active member of that organization may list its active schools.

`POST /api/v1/organizations/{organization_id}/schools/`

Allowed only to active organization `owner` or `administrator` memberships.

```json
{
  "name": "Bright Future Academy",
  "schoolType": "nursery_primary_secondary",
  "location": "Kaduna, Kaduna State"
}
```

Accepted school types:

- `nursery`
- `primary`
- `secondary`
- `nursery_primary`
- `primary_secondary`
- `nursery_primary_secondary`
- `college`
- `other`

Successful response:

```json
{
  "school": {
    "id": "SCHOOL_UUID",
    "organizationId": "ORG_UUID",
    "name": "Bright Future Academy",
    "schoolType": "nursery_primary_secondary",
    "location": "Kaduna, Kaduna State",
    "isActive": true
  },
  "membership": {
    "id": "MEMBERSHIP_UUID",
    "schoolId": "SCHOOL_UUID",
    "schoolName": "Bright Future Academy",
    "role": "proprietor",
    "organizationId": "ORG_UUID"
  }
}
```

## Atomic provisioning

School creation is one database transaction:

1. lock/re-read the active organization;
2. re-check the caller has an active owner/admin organization membership;
3. create the `School` tenant;
4. create the caller's `proprietor` `SchoolMembership`;
5. record an append-only organization audit event;
6. commit everything together.

If any step fails, no partial school is left behind.

The access subsystem stores only overrides from built-in role defaults, so a new
school requires no fabricated access rows. Its proprietor membership immediately
receives the normal proprietor defaults.

## Tenant isolation rule

Organization ownership never weakens existing school authorization. Operational
endpoints continue to authorize through `schools.Membership` and a school id.
Organization membership is used only for account-level actions such as school
provisioning.

## Next SaaS phases

1. organization onboarding/sign-up UI;
2. account-home navigation from an open proprietor workspace;
3. plans, subscriptions and entitlements;
4. billable-student snapshots and invoices;
5. payment provider integration;
6. organization administrators/invitations;
7. platform operator console.
