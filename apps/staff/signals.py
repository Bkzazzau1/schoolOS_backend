from django.dispatch import Signal

#: Sent when a proposal is approved and the person is now a staff member.
#: Arguments: school, staff_id, email, system_role, name, proposal_id.
#:
#: The invitations feature listens to this to send the person their registration
#: link, so `staff` does not depend on it and either can be built and tested alone.
staff_approved = Signal()

#: Sent when someone is asked to complete their registration: right after approval,
#: and whenever a registration request is sent or resent. The invitations feature
#: listens to this to email the person their link.
#: Arguments: school, staff_id, email, system_role, name, requested_by (a
#: Membership, or None when the server did it).
registration_requested = Signal()
