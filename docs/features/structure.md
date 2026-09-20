# Feature: structure and appearance (backend)

Three record types the owner manages and the whole school relies on. All go through `sync/push/` and come back
through `sync/pull/`. Code: `apps/structure/`.

| Record | Written by | Read by |
| --- | --- | --- |
| `academic_section` (nursery, primary, secondary, ...) | owner | everyone in the school |
| `leadership_appointment` (head, deputy, HOD, coordinator) | owner | owner, principal, administrator, finance officer, teachers, other staff (not parents or students) |
| `school_appearance` (the colour scheme; one record, id `theme`) | owner | everyone in the school |

Nothing here can be deleted: appointments and jobs point at sections.

## Rules kept on the server

The app checks these on the device; the server keeps them so no device can break them.

- **Section**: `id` matches the record, name, stage and campus are required, `classes` is a whole number from 0 to 500.
- **Appointment**
  - The section must exist in this school.
  - A section has one Section Head. Replacing the head is an update that changes the person.
  - Everyone who is not a head reports to the head or a deputy **of the same section**.
  - Nobody reports in a circle.
  - A head of department must have a department; other levels have none.
  - A post's section and level are fixed once created. To change either, appoint someone new.
  - Two devices cannot both create a head at once: the section is locked while the check runs.
- **Appearance**: the scheme must be one the app offers (`forest`, `ocean`, `violet`, `rose`, `amber`).
- Who made the change and when are stamped by the server (`updatedByMembershipId`, `updatedAt`), never taken from
  the app. Fields the server does not know are dropped.

## What the app must change (no integration yet)

1. **Queue the default sections and appointments.** The app seeds its default sections and appointments on the
   device only (`_seed` in `proprietor_structure_repository.dart`), so the server never receives them. Until they are
   sent as `create` mutations, sections first, then appointments, editing one is refused ("record does not exist")
   and an appointment is refused ("section does not exist").
2. Replacing a head sends two updates (the appointment, then the section). They are independent on the server;
   send the appointment first.
3. Show the server's message when a change is refused.

## Not covered yet

- Job assignments still accept any `sectionId`. Once the app sends its sections, they should be checked against
  them (deferred so the app is not broken before it sends them).
- Campuses as their own record (a section only carries a campus name), needed by the campus comparison screen.
- The section's `leaderName` and the head appointment are not kept in step by the server.
