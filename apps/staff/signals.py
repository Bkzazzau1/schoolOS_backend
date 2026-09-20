from django.dispatch import Signal

#: Sent when a proposal is approved and the person is now a staff member.
#: Arguments: school, staff_id, email, system_role, name, proposal_id.
#:
#: The invitations feature listens to this to send the person their registration
#: link, so `staff` does not depend on it and either can be built and tested alone.
staff_approved = Signal()
