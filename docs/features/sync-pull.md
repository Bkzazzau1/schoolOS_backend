# Feature: sync pull (backend)

Until now a device could only send changes. Pull lets any device download the records it is allowed to see,
so a second phone, or a reinstalled app, gets the same school.

## Endpoint

`GET /api/v1/sync/pull/?school=<id>&since=<cursor>&limit=<n>` (add `&membership=<id>` if the person holds
several roles at the school)

```json
{ "records": [ { "entityType": "...", "entityId": "...", "version": 3, "deleted": false, "payload": { } } ],
  "cursor": 41, "hasMore": false }
```

1. First time: `since=0`. Apply the records, keep `cursor`.
2. While `hasMore` is true, ask again with the new cursor. Page size defaults to 200, at most 500.
3. Later: ask with the saved cursor to get only what changed since. A record changed several times comes once, at
   its latest version. Nothing new returns the same cursor.
4. `"deleted": true` means remove it locally (`payload` is empty).
5. The version on a record is what to send back as `baseVersion` when pushing an edit.

Errors: 401 not signed in, 403 not a member of that school, 400 bad parameters or `membership_required`.

## Why nothing is missed

Each record stores the school's **change number** at its last change. The number comes from one counter row per
school that is locked until the change commits, so changes in a school are numbered and committed strictly in order.
A device that has read up to N can never later see a change numbered below N. The database also refuses a repeated
number in a school. Timestamps are not used for this because concurrent changes can commit out of order.

## Who sees what

Each record type decides in its handler (`visible`). The default is the people who may change it.

| Record | Who receives it |
| --- | --- |
| Salary (`owner_payroll_profile`) and payroll batches | owner, finance officers, and anyone the owner authorised for payroll (payroll access in `features/payroll.md`) |
| Payroll authority, job assignments | owner, and the person the assignment belongs to |
| Staff list (`administrator_staff_directory`) | owner, principal, administrator, finance officer |
| Staff profile (`owner_staff_profile`) | owner and principal (all of it), the staff member (their own), administrator (without bank details; those come back blank) |
| Staff proposal | its proposer, the owner, and assigned approvers |
| Kinds with no rules yet | the owner, only while developing; never in production |

A record a person may not see is not sent, but the cursor moves past it.

## Not covered yet

- If someone **loses** access to a record (a proposer's proposal is fine, but for example a role change), their
  device keeps its old copy until the record next changes. There is no "you can no longer see this" message yet.
- Bank details are still not sent to finance officers; that is decided when payments are released for real.
- No push notification to wake other devices; they pull when the app opens or on a timer.
