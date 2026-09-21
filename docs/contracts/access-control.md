# Contract: activities and access

**Status:** backend **built** (`apps/access`, `apps/notifications`), not yet
connected to the app. This is the contract for the Flutter side and for every
backend feature that must respect it.

## 1. The idea

An **activity** is one screen of the app a person can be allowed to see. There are
115. Every role has a **default set**. The owner can then, for their school:

- **change a role's defaults** ("teachers here also get the Receipts screen");
- **give someone an activity** their role does not have (a **grant**);
- **take one away from someone** (a **block**);
- **move one from a person to another** in one step (**reassign**);
- put an **end date** on a grant or block (cover for someone on leave).

The person is **told** about every change that affects them. Everything is
recorded in an audit trail. The owner cannot be blocked.

## 2. Activities

A key is `<workspace>.<screen>`, and the screen part is exactly the key the app
already uses in that workspace's navigation, so the app filters its menu with no
lookup table.

| Workspace | Count | Default for | Examples |
| --- | --- | --- | --- |
| `owner` | 15 | owner | `owner.payroll`, `owner.staff-profiles`, `owner.access` |
| `principal` | 15 | principal | `principal.teachers`, `principal.approvals` |
| `administrator` | 13 | administrator | `administrator.records`, `administrator.staff` |
| `finance` | 15 | accountant | `finance.payroll`, `finance.receipts` |
| `teacher` | 16 | teacher | `teacher.cbt`, `teacher.attendance` |
| `parent` | 11 | parent | `parent.finance`, `parent.documents` |
| `schoollife` | 16 | **everyone** | `schoollife.transport`, `schoollife.events` |
| `general` | 5 | staff, student | `general.dashboard`, `general.messages` |

The catalog is **code** (`apps/access/catalog/`), so the app and server share one
list. A test pins it to the app's real navigation keys; if a screen is added or
renamed in the app, that test fails until the catalog is updated.

Flags: **essential** (a landing screen, never blockable), **grantable** (`false`
only for `owner.access`, which only the owner can ever have), **sensitive**
(informational: the owner's screen warns before granting it).

**School life is for everyone by default** (owner, principal, administrator,
teacher, finance, parent, staff, student). That decides who can *see* a module.
Who may *post* in it (for example a parent posting in the community) is a separate
rule that belongs to each module when it is built on the server.

**Payroll and every other owner screen can be given to anyone** except
`owner.access`. Seeing the payroll screen is not money authority: preparing,
approving and releasing payment stay as the payroll authorities.

## 3. How a person's access is worked out

In order, later layers win:

1. the built-in defaults for their role;
2. **the school's changes to that role** (`RoleActivity`, stored as differences);
3. **the owner's grants and blocks for that person** (`MembershipActivity`),
   ignoring any that have expired or are still **waiting** (section 4);
4. essential activities are always added back.

The owner's role is fixed. A person's own grant or block **survives** later
changes to their role. Clearing it puts them back on the role's default.

## 4. Blocking is two steps

The owner blocks; the person's app **first fetches the latest and submits its
pending work**, then the block takes effect. Nothing the person has not yet
handed in is lost.

1. **The owner blocks** (`mode: "after_sync"`, the default). The person **keeps**
   the activity for now and is told it is going.
2. **`access/me/`** lists it under `blocking` with a `finalizeAt` deadline.
3. **The app** fetches the latest data for that activity, pushes its queued
   changes for it, then calls **`access/acknowledge/`**.
4. **The block takes effect** at that moment. The app then removes the activity
   from its menus and clears its local data for it.
5. If the app never reports back, the block takes effect anyway at `finalizeAt`
   (`ACCESS_BLOCK_GRACE_HOURS`, default 48).

The owner can also block **`immediate`**, for example for a security reason. A
**grant** is always immediate. Blocking something a person does not currently have
also takes effect at once (there is nothing to submit), and pressing block again
on an already blocked person never brings the activity back.

Role-wide removals (`PUT roles/{role}/`) take effect immediately: they are a policy
change, not about one person's unsent work.

## 5. Endpoints (all under `/api/v1/`)

**A person with two roles at one school** (a teacher who is also a parent) must
say which one, with `?membership=<id>`. Without it the server answers
`400 {"code": "membership_required", "memberships": [...]}` and never guesses.
A person can only name their own memberships (403 otherwise).

**Any member**

| Method and path | What |
| --- | --- |
| `GET  schools/{school}/access/me/` | `{membershipId, role, activities: [keys], blocking: [{activity, finalizeAt}]}` |
| `POST schools/{school}/access/acknowledge/` | `{"activities": [...]}`: fetched and submitted, so those blocks take effect now. Returns the same as `me`. Own blocks only; repeating is harmless |
| `GET  schools/{school}/notifications/?unread=1&limit=50` | The person's own messages, newest first, with an `unread` count |
| `POST schools/{school}/notifications/{id}/read/` | Mark one read (404 if it is not theirs) |
| `POST schools/{school}/notifications/read-all/` | Mark all read |

**Owner only** (403 for everyone else, including other schools' owners)

| Method and path | What |
| --- | --- |
| `GET  owner/schools/{school}/access/catalog/` | Every activity, grouped, with flags, `defaultRoles`, `rolesInThisSchool` |
| `GET  owner/schools/{school}/access/roles/` | Each role's activities here and whether it was `customized` |
| `PUT  owner/schools/{school}/access/roles/{role}/` | `{"activities": [...]}` sets a role's activities |
| `DELETE owner/schools/{school}/access/roles/{role}/` | Back to the built-in defaults |
| `GET  owner/schools/{school}/access/people/` | Everyone: effective activities and their grants and blocks, each with a `state` (`in_force`, `waiting_for_sync` with `takesEffectBy`, or `expired`) |
| `PUT  owner/schools/{school}/access/people/{membership}/activities/{key}/` | `{"effect": "grant\|block", "mode": "after_sync\|immediate", "expiresAt": null, "note": ""}` |
| `DELETE …/people/{membership}/activities/{key}/` | Restore the role's default (also cancels a waiting block); 404 if nothing was set |
| `POST owner/schools/{school}/access/reassign/` | `{"activity", "fromMembershipId", "toMembershipId", "note", "mode"}`. The receiver starts at once; the giver finishes up first. All or nothing |
| `GET  owner/schools/{school}/access/audit/?limit=50` | Who changed what, newest first (max 200) |

A refused change is `400 {"code": "access_error", "message": "..."}`, written for
the owner and safe to show. **Refused:** blocking a landing screen; granting
`owner.access`; changing the owner; an unknown activity or role; a person from
another school or an inactive one; an end date in the past; removing a landing
screen from a role; reassigning something the giver lacks or the receiver has.

## 6. Telling the person

Every change that really changes something for someone puts a message in their
inbox (`kind: "access_changed"`, with `data.activities` so the app can re-read
access). No message is sent when nothing changes for them, and none for a refused
change. Examples:

- *"You can now use Payroll Handoff. Note from the owner: Covering Mrs Musa."*
- *"CBT Practice will be removed from your account after your next sync, and no
  later than 23 Sep 2026. Your app will send any work you have not yet submitted first."*
- *"You no longer have CBT Practice."*
- A role change tells everyone in that role: *"You can now use: Receipts. No longer
  available: CBT Practice."*

**In-app only for now.** Email is added in `apps/notifications/services.py` when
the school's mail backend exists, so every feature gets it at once.

## 7. Enforcement (the rule every feature must follow)

Hiding a menu item is a convenience. **The server must refuse the data too.**

```python
from apps.access.permissions import require_activity

def get(self, request, school_id):
    require_activity(request.user, school_id, "finance.payroll", request.query_params.get("membership"))
```

- Every endpoint that returns or accepts data belonging to an activity calls it.
- A block that is still *waiting* does not stop the server: the person keeps
  access until they acknowledge or the deadline passes, so their app can finish.
- Sync handlers that write records belonging to an activity should check
  `has_activity(membership, key)` in `authorize`.

## 8. What the Flutter app needs

1. After sign-in and on **every sync**, call `access/me/` (with `?membership=` when
   the person has more than one role) and **cache the list** for offline use.
2. **Filter each workspace's navigation** by `"<workspace>.<screen key>"`. If the
   open screen is no longer allowed, fall back to the workspace's landing screen.
3. **For each item in `blocking`:** fetch the latest for that activity, push queued
   changes for it, then call `access/acknowledge/`. Only after that succeeds, hide
   the screen and **delete its local data**. If the push fails, do not
   acknowledge: try again on the next sync (the deadline still protects the owner).
4. Show the **notifications inbox** and an unread badge; open it on
   `access_changed` and re-read access.
5. Treat a `403` from any endpoint as "no longer allowed": refresh access and leave
   the screen without an error dialog.
6. The owner's **Access & Activities** screen (`owner.access`) on the endpoints in
   section 5: role defaults, a per-person page (grant, block with "after sync" or
   "now", end date, note), reassign, waiting blocks, and the audit list. Warn
   before granting a `sensitive` activity.

## 9. Decisions

**Settled**

1. **School life** is for everyone by default.
2. **Payroll** (and every owner screen except `owner.access`) can be assigned.
3. **A block waits for the app to fetch and submit, then takes effect** (section 4).
4. **The person is notified** of every change that affects them (section 6).
5. **One place for power.** Screens, payroll authorities and job duties will
   eventually be managed together as one access system (plan below).

**Plan for decision 5.** Today there are three places to give power: activities
(this contract), payroll authorities (`owner_payroll_authorizer`: prepare, approve,
pay, approve staff, view) and job duties (`owner_job_assignment`). The plan is to
give each activity optional **actions** (`finance.payroll` -> prepare, approve,
release; `owner.staff-profiles` -> approve new staff) and let the owner grant them
in the same screen, with the same notifications, blocks and audit. The existing
authorizer records would be migrated into it, and the server-owned link to the
person's account carries over unchanged. This needs its own design pass before
building; nothing here blocks it.

**Still open**

- **Posting rights in school life** (who may post, per module): decided when each
  module is built. For example, may a parent post in Community but not Noticeboard?
- **Email** for notifications, once a mail backend exists.
- **Role-wide removals** are immediate today. Should they also wait for each
  person's app, like a personal block? (More work; say if wanted.)
- **The wait** is 48 hours by default. Is that right for schools with poor
  connectivity?
- **Parents and students** can be configured like anyone else. Confirm that is
  wanted (for example blocking `parent.finance` for one family).
