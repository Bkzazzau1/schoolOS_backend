# The owner's features, and how each becomes backend

Read from the Flutter app (`proprietor_workspace_page.dart` and the pages it
opens). This is the owner's whole menu, what each screen stores today, and what
the backend needs. It is the plan for building the owner side feature by feature.

**Legend.** *Persisted*: the app saves real records on the device that need
syncing. *Demo*: the screen shows fixed sample data today, so there is nothing to
store yet; the backend has to compute it from other features.

## The owner's screens

| Activity key | What the owner does | Data today | Backend |
| --- | --- | --- | --- |
| `owner.overview` | School-wide snapshot: enrollment, finance, staff, risks | Demo | **Read model**, after the data features exist |
| `owner.finance` | Fee income, collections, arrears at a glance; jumps into the finance office | Demo | **Read model** |
| `owner.finance-approvals` | Approves or declines scholarships and discounts | Persisted: `concession_request` | `finance` feature: owner-only decision, others create |
| `owner.enrollment` | Admissions and enrollment summary | Demo | **Read model** from admissions and registration |
| `owner.staff` | HR overview: leadership, attendance, workload, contract risk | Demo | **Read model** |
| `owner.jobs` | Gives jobs and duties to anyone, registered or not | Persisted: `owner_job_assignment` | **Built** (`apps/owner/jobs`) |
| `owner.staff-profiles` | Full staff record; proposals and approvals; onboarding; duplicates | Persisted: `owner_staff_profile`, `staff_proposal`, staff directory | **Built** (`apps/staff`), see `features/staff.md` |
| `owner.payroll` | Salaries, invoice, who may approve staff and pay | Persisted: `owner_payroll_profile`, `owner_payroll_authorizer`, `payroll_batch` | Salaries and authority **built**; batches in `payroll` feature |
| `owner.reports` | Executive report pack | Demo (export is on the device) | **Read model** |
| `owner.campuses` | Compare campuses | Demo | Needs a campus model, then a read model |
| `owner.ai` | Owner's assistant | Demo | Later; depends on what data it may read |
| `owner.structure` | Sections, leaders, appointments | Persisted: `academic_section`, `leadership_appointment` | `structure` feature: small, owner-only |
| `owner.appearance` | School colours and look | Persisted: `school_appearance` | `appearance` feature: owner writes, everyone reads |
| `owner.school-life` | Opens 16 shared modules (community, events, transport, meals, ...), which everyone has by default | Persisted, one record type per module | **Built** (`apps/schoollife`), see `features/school-life.md` |
| `owner.access` | **New.** Decide who sees which activity | n/a | **Built** (`apps/access`), see the access contract |

## What is already built

- **Sync door** with per-type rules (`apps/sync`, `apps/owner/handlers.py`).
- **Salaries, payroll authority, job assignments**: owner-only, server-owned
  status and account link, append-only salary history.
- **Access control**: the catalog of 115 activities, role defaults, per-person
  grants and blocks, reassign, audit. See `contracts/access-control.md`.
- **School web addresses** and the app-link file (`apps/domains`).

## Suggested order for the rest of the owner side

1. **`structure` and `appearance`. Done** (`features/structure.md`). Small, owner-only, already persisted in the
   app, no dependencies. Quick wins that also prove the handler recipe again.
   `structure` matters because section heads in job assignments point at it.
2. **`staff`. Done.** Proposals, approval (owner or assigned approver, done on
   the server in one step), profiles, phone and NIN uniqueness, the registration
   rules. See `features/staff.md`. Unblocks invitations.
3. **`invitations`.** Contract written. Needs staff, email, and the domains work
   already built.
4. **`payroll` batches. Done** (`features/payroll.md`). Prepare, approve, release, with the approver never the
   preparer.
5. **`finance` concessions. Done** (`features/concessions.md`). Owner decides; finance creates.
6. ~~Sync pull~~ (built: `features/sync-pull.md`).
7. **Read models (owner and finance built: `features/dashboards.md`; the rest wait for their data)** for overview, finance, enrollment, HR overview, reports,
   campuses. These only make sense once the data features above exist, because
   they are computed from them.
8. **`schoollife`** modules, one handler each, following the same recipe.

## Decided

- **School life is for everyone** (parents, teachers, staff and students see the
  modules; who may post in each is decided per module when it is built).
- **Payroll can be assigned** to anyone; only `owner.access` is owner-only.
- **One place for power:** screens, payroll authority and job duties will fold
  into one access system. See the plan in `contracts/access-control.md`.

## Two things to settle early

- **Campuses.** "Campus Comparison" implies more than one campus per school. The
  backend has one `School` per tenant and no campus. Decide whether a campus is a
  section of a school or a separate level between school and section.
- **Where roles meet authority.** Payroll has its own authorities (prepare,
  approve, pay, approve staff) and jobs have duties, alongside the new activities.
  They answer different questions (see the access contract), but the owner will
  see three places to give someone power. A single "Access & Activities" screen
  should show all three.
