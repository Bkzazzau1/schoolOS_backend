# Feature: school life (backend)

The 16 shared modules everyone in the school has by default: Community, Noticeboard, Activities & Clubs, Events &
Calendar, Houses & Teams, Media Gallery, Excursions, Transport, Meals, Boarding, Assembly, Visitors, Lost & Found,
Service, Awards and Teaching Models. Code: `apps/schoollife/`. The app sends their records through `sync/push/`; other
devices receive them through `sync/pull/`. Before this, the server refused all of them in production.

## How it is built

- **Fifteen modules are one small spec each** (`specs/`), run by one shared handler (`framework.py`). A spec says who
  manages, who may add their own, who reads, which fields are the leadership's to decide, and which are the server's.
  Changing who may do what in a module is a one-line change, and a test runs every guarantee below against every spec.
- **Community is its own package** (`community/`) because everyone writes to it: posts, comments, reactions and reports
  are four record types.

## What every module gets

- Only the roles named in the spec may write. **Managers** create and change anything; **contributors** add records and
  change only their own (a teacher cannot edit an event the administrator made).
- A record's id field must match its id, required fields must be present, no text field is huge (5,000 characters) and a
  record is at most 60 KB.
- **The server stamps** who created it, who last changed it and when (`createdByMembershipId`, `createdAt`,
  `updatedByMembershipId`, `updatedAt`). Anything the app sends for these is ignored. An edit by a manager does not make
  them the author.
- **Guarded fields** can only be changed by their roles (below), and a new record cannot arrive with one already set.
- **Server-owned counters** (a notice's `readCount`, `totalRecipients`) are set by the server whatever the app sends.
- Records are not deleted (managers cannot either), except community posts, comments and reactions.

## Who does what (defaults: change them in `specs/`)

| Module | Record | Manage | Add their own | Receive | Guarded |
| --- | --- | --- | --- | --- | --- |
| Noticeboard | `noticeboard_notice` | owner, principal, administrator | | everyone; staff-only notices to the staff side | `pinned`: owner, principal |
| Events | `school_event` | owner, principal, administrator | teacher | everyone | |
| Assembly | `assembly_session` | same | teacher | everyone | |
| Excursions | `school_excursion` | same | teacher | everyone | `readinessReviewed`: owner, principal |
| Activities | `school_activity` | same | teacher | everyone | |
| Houses | `school_house` | same | | everyone | |
| Service | `service_project` | same | teacher | everyone | `verified`: owner, principal |
| Awards | `award_recognition` | owner, principal | teacher (drafts) | everyone; internal-only awards to the staff side | setting `visibility` to `publicShowcase`: owner, principal |
| Teaching models | `teaching_model_config` | owner, principal | | staff side | |
| Transport | `school_transport_route` | owner, principal, administrator | | everyone | `reviewed`: owner, principal |
| Meals | `school_meal_day` | same | | everyone | |
| Boarding | `boarding_dorm` | same | | staff side only | `handoverReviewed`: owner, principal |
| Visitors | `visitor_record` | same | staff | staff side only | `frontDeskReviewed`: owner, principal, administrator |
| Lost & Found | `lost_found_item` | owner, principal, administrator, staff | teacher, parent, student, finance officer | everyone | changing `status` or `claimant`: the managers |
| Media Gallery | `gallery_media_album` | owner, principal, administrator | teacher, staff | everyone; internal albums to the staff side | setting `visibility` to `publicShowcase`: owner, principal |

"Staff side" means owner, principal, administrator, finance officer, teacher and other staff. Someone who added a record
always receives it.

## Community

Each thing a person does is its own record, so nobody ever edits someone else's record and two people commenting at once
cannot overwrite each other.

| Record | Who writes | Rules |
| --- | --- | --- |
| `community_post` | every adult member: owner, principal, administrator, finance officer, teacher, staff, **parent**. **Not students** | Edit or delete your own; a moderator (owner, principal, administrator) can edit or remove any. Staff-only posts can be made only by the staff side. Only owner or principal can put a post on the **public showcase**. The author's name and role are the server's. `comments` and `reactions` inside a post are dropped |
| `community_comment` | same | On a post you can see (`postId` fixed). Edit your own; delete your own, or a moderator |
| `community_reaction` | same | Id is `<postId>:<yourMembershipId>`, so one reaction per person per post, only as yourself. Removing it deletes it |
| `community_report` | same | Anyone who sees the post can report it; it starts as `awaitingReview`; only a moderator settles it (`actioned` or `dismissed`). Moderators are told. Only moderators and the reporter see it |

Audiences: a `staffOnly` post is received by the staff side; `parentsOnly` by parents and moderators; the rest
(`wholeSchool`, `earlyYears`, `primary`, `secondary`, `jss2A`) by everyone. Comments, reactions and reports follow their
post. If a post is deleted, its comments go only to moderators.

## Decisions I made that you may want to change

1. **Students cannot post** in Community (they can read it). Everyone else can. Change `WRITERS` in `community/common.py`.
2. **Teachers may add events, assembly, excursions, activities and service projects** but only edit their own; only
   managers change others'. The app today lets only the owner write in any module; this widens it, as you asked
   ("school life is for everyone").
3. **Section and class audiences are not enforced** (Primary, JSS 3, a class): the server does not know which children
   are in which section yet, so they are treated as whole school. Only staff-only and parents-only are enforced.
4. **Drivers** are an adult member: they read what everyone reads (transport, events, notices), post to Community and
   report found items, but not boarding, visitors or teaching models, and add nothing else.
5. **Parents receive everything except** boarding, visitors and teaching models (and internal-only pictures and awards,
   and staff-only posts and notices).

## What the app must change (no integration yet)

1. **Community**: stop keeping `comments` and a `reactions` count inside the post. Create a `community_comment` per
   comment, and a `community_reaction` (id `<postId>:<membershipId>`) per like; count them on the device. Reporting
   creates a `community_report`. The author name is shown from the record, not typed by the app (it hard-codes a name).
2. **Noticeboard**: read counts now belong to the server; the app must not increase `readCount` on the notice. Read
   receipts and acknowledgements will need their own record.
3. Guarded fields (`pinned`, review ticks, public showcase) are refused when someone else sets them: show the message.
4. Widen each module's screens for the roles that may now add records, and hide the controls for those who may not.
5. Meal days use the lower-case day as their id; a boarding record's id is made from the dorm name.

## Not covered yet

- Read receipts and acknowledgements for notices, attendance and participation for activities, house points history,
  photos and file uploads for the gallery (no file storage yet), consent forms, and signing up for a club or excursion.
- Section- and class-level audiences (needs the class and student records).
- Rate limits on posting, and profanity or spam screening.
- Field-by-field checks of each module's own values (a date is a date, an amount is a number). Today only the fields in a
  spec's `required` list are checked, because the app's models still change; add them to a spec when they settle.
