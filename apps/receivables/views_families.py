"""Families, their statements and credit, and the collection accounts they pay into. For the owner and the
finance office (and the billing authorities, who are among them); every object is looked up inside the
acting person's own school, so another school's family is simply not found."""

from rest_framework.response import Response

from apps.bankconnect.models import BankTransaction, TransactionAllocation
from apps.students.models import GuardianLink, Student

from . import bridge, collection_accounts, credit, families, issuers, ledger, serializers, statements
from .errors import Refused
from .http import ReceivablesView, body, found, paging, uuid_arg
from .models import Family, FamilyCollectionAccount, FamilyStatement, StudentReceivable
from .permissions import acting_membership


def _family(membership, family_id) -> Family:
    return found(Family.objects.filter(school=membership.school, id=family_id), "family")


def _student(membership, student_id) -> Student:
    student = Student.objects.filter(school=membership.school, id=uuid_arg(student_id, "student")).first()
    if student is None:
        raise Refused("That student is not at this school.", "student_not_found")
    return student


def _connection(membership, data):
    """The school's own bank connection a request names, or a refusal."""
    from apps.bankconnect.models import BankConnection

    if not data.get("connectionId"):
        raise Refused("Choose which of the school's bank accounts this is for.", "connection_required")
    return found(BankConnection.objects.filter(school=membership.school, id=uuid_arg(data["connectionId"], "connection")), "bank connection")


class FamiliesView(ReceivablesView):
    """GET families (?q= search, ?accounts=with|without filters by having a payment account, ?withAccounts=1 lists each
    family's accounts and students, paged by ?limit= and ?offset=)."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        limit, offset = paging(request, default=30)
        params = request.query_params
        rows = families.search(membership.school, params.get("q", ""), limit=limit + 1, offset=offset, accounts=params.get("accounts") or None)
        more = len(rows) > limit
        show = params.get("withAccounts") in ("1", "true", "yes")
        return Response({"families": [serializers.family(f, accounts=show) for f in rows[:limit]], "hasMore": more})

    def post(self, request, school_id):
        """Make a family, optionally with its students."""
        membership = acting_membership(request, school_id)
        data = body(request)
        ids = data.get("studentIds") or []
        if not isinstance(ids, list):
            raise Refused("studentIds must be a list.", "invalid_students")
        students = [_student(membership, i) for i in ids]
        family = families.create_family(membership.school, display_name=data.get("displayName"), actor=membership, students=students)
        for student in students:
            families.link_guardians_of(family, student, actor=membership)
        return Response({"family": serializers.family(family, detail=True)}, status=201)


class FamilyDetailView(ReceivablesView):
    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        return Response({"family": serializers.family(_family(membership, family_id), detail=True)})


class FamilyActionView(ReceivablesView):
    """POST families/<id>/<action>/ - change a family."""

    action = None

    def post(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family, data = _family(membership, family_id), body(request)
        if self.action == "rename":
            families.rename(family, data.get("displayName"), actor=membership)
        elif self.action == "add-student":
            student = _student(membership, data.get("studentId"))
            families.add_student(family, student, actor=membership)
            families.link_guardians_of(family, student, actor=membership)
        elif self.action == "remove-student":
            families.remove_student(family, _student(membership, data.get("studentId")), actor=membership)
        elif self.action == "link-guardian":
            link = GuardianLink.objects.select_related("student").filter(id=uuid_arg(data.get("guardianId"), "guardian"), student__school=membership.school).first()
            if link is None:
                raise Refused("That guardian is not at this school.", "guardian_not_found")
            families.link_guardian(family, link, primary=bool(data.get("primary")), actor=membership)
        elif self.action == "set-status":
            families.set_status(family, data.get("status"), actor=membership)
        family.refresh_from_db()
        return Response({"family": serializers.family(family, detail=True)})


class StudentsWithoutFamilyView(ReceivablesView):
    """Students who could be charged but belong to no family: someone must place them before they can be billed."""

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response({"students": [serializers.student_brief(s) for s in families.students_without_family(membership.school)[:500]]})


class BridgeView(ReceivablesView):
    """GET reports what the bridge from the older free-text family references would do; POST does it."""

    @staticmethod
    def _report(report) -> dict:
        return {
            "alreadyInFamily": report.already_in_family, "familiesCreated": report.families_created, "studentsLinked": report.students_linked,
            "groups": [{"reference": g.reference, "students": [serializers.student_brief(s) for s in g.students], "familyExists": g.existing is not None} for g in report.groups],
            "conflicts": [{"student": serializers.student_brief(s), "references": refs} for s, refs in report.conflicts],
            "withoutReference": [serializers.student_brief(s) for s in report.without_reference],
            "siblingHints": [serializers.student_brief(s) for s in report.sibling_hints],
        }

    def get(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response({"report": self._report(bridge.analyse(membership.school))})

    def post(self, request, school_id):
        membership = acting_membership(request, school_id)
        report = bridge.apply(membership.school, singletons=bool(body(request).get("singletons")), actor=membership)
        return Response({"report": self._report(report)})


class FamilyReceivablesView(ReceivablesView):
    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        rows = list(StudentReceivable.objects.filter(family=family).select_related("student", "session", "term").order_by("due_date", "created_at", "id"))
        figures = ledger.positions(rows)
        return Response({"receivables": [serializers.receivable(r, figures[r.id]) for r in rows], "position": serializers.position(ledger.family_position(family))})


class FamilyStatementView(ReceivablesView):
    """GET the statement, worked out now from the ledger. Optional ?session=<id>&term=<id>."""

    def get(self, request, school_id, family_id):
        from apps.academics.models import AcademicSession, AcademicTerm

        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        session = term = None
        if request.query_params.get("session"):
            session = found(AcademicSession.objects.filter(school=membership.school, id=uuid_arg(request.query_params["session"], "session")), "session")
        if request.query_params.get("term"):
            term = found(AcademicTerm.objects.filter(session__school=membership.school, id=uuid_arg(request.query_params["term"], "term")), "term")
        return Response({"statement": statements.build(family, session=session, term=term)})


class FamilyStatementsView(ReceivablesView):
    """GET the statements issued to a family; POST to issue one for a session (and optionally a term)."""

    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        return Response({"statements": [serializers.statement_record(s) for s in FamilyStatement.objects.filter(family=family)[:100]]})

    def post(self, request, school_id, family_id):
        from apps.academics.models import AcademicSession, AcademicTerm

        membership = acting_membership(request, school_id)
        family, data = _family(membership, family_id), body(request)
        session = found(AcademicSession.objects.filter(school=membership.school, id=uuid_arg(data.get("sessionId"), "session")), "session")
        term = found(AcademicTerm.objects.filter(session=session, id=uuid_arg(data.get("termId"), "term")), "term") if data.get("termId") else None
        return Response({"statement": serializers.statement_record(statements.issue(family, session=session, term=term, actor=membership))}, status=201)


class FamilyCreditView(ReceivablesView):
    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        limit, offset = paging(request)
        entries = credit.entries(family).order_by("-created_at", "-id")
        return Response({
            "creditMinor": ledger.credit_balance(family), "currency": "NGN",
            "entries": [serializers.credit_entry(e) for e in entries[offset: offset + limit]], "total": entries.count(),
        })


class FamilyRefundView(ReceivablesView):
    def post(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family, data = _family(membership, family_id), body(request)
        entry = credit.refund(family, data.get("amountMinor"), actor=membership, reason=data.get("reason"))
        return Response({"entry": serializers.credit_entry(entry), "creditMinor": ledger.credit_balance(family)}, status=201)


class FamilyPaymentsView(ReceivablesView):
    """The bank payments that have gone towards this family, with where each went."""

    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        ids = set(TransactionAllocation.objects.filter(family=family).values_list("transaction_id", flat=True))
        ids |= set(family.credit_entries.exclude(transaction__isnull=True).values_list("transaction_id", flat=True))
        rows = BankTransaction.objects.filter(school=membership.school, id__in=ids).order_by("-transaction_date", "-created_at")[:100]
        payments = []
        for tx in rows:
            allocations = TransactionAllocation.objects.filter(transaction=tx, family=family).order_by("created_at", "id")
            payments.append({
                "id": str(tx.id), "amountMinor": tx.amount_minor, "currency": tx.currency, "senderName": tx.sender_name,
                "transactionDate": tx.transaction_date.isoformat(), "isSandbox": tx.is_sandbox,
                "allocations": [serializers.allocation_row(a) for a in allocations], "creditMinor": credit.total_credit_from(tx, family),
            })
        return Response({"payments": payments})


class FamilyAccountsView(ReceivablesView):
    """GET a family's collection accounts; POST to record the account a provider has given it."""

    def get(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family = _family(membership, family_id)
        return Response({"accounts": [serializers.account(a) for a in family.collection_accounts.all()]})

    def post(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family, data = _family(membership, family_id), body(request)
        connection = _connection(membership, data) if data.get("connectionId") else None
        account = collection_accounts.register(
            family, provider=data.get("provider"), actor=membership, account_number=data.get("accountNumber", ""),
            external_account_ref=data.get("externalAccountRef", ""), account_name=data.get("accountName", ""),
            bank_name=data.get("bankName", ""), connection=connection, public_details=data.get("details"),
            provisioned=data.get("provisioned", True) is not False,
        )
        return Response({"account": serializers.account(account)}, status=201)


class FamilyAccountIssueView(ReceivablesView):
    """POST to have the school's provider issue this family its account, under one of the school's connections."""

    def post(self, request, school_id, family_id):
        membership = acting_membership(request, school_id)
        family, data = _family(membership, family_id), body(request)
        account = collection_accounts.issue(family, connection=_connection(membership, data), actor=membership)
        return Response({"account": serializers.account(account)}, status=201)


class IssueMissingAccountsView(ReceivablesView):
    """POST to give every active family without one an account under a connection."""

    def post(self, request, school_id):
        membership = acting_membership(request, school_id)
        return Response(collection_accounts.issue_missing(_connection(membership, body(request)), actor=membership))


class AccountProvidersView(ReceivablesView):
    """GET the providers a family account can come from, with what each one's account looks like."""

    def get(self, request, school_id):
        acting_membership(request, school_id)
        return Response({"providers": issuers.describe()})


class StatementVoidView(ReceivablesView):
    def post(self, request, school_id, statement_id):
        membership = acting_membership(request, school_id)
        statement = found(FamilyStatement.objects.filter(school=membership.school, id=statement_id), "statement")
        return Response({"statement": serializers.statement_record(statements.void(statement, actor=membership, reason=body(request).get("reason")))})


class CollectionAccountActionView(ReceivablesView):
    action = None

    def post(self, request, school_id, account_id):
        membership = acting_membership(request, school_id)
        account = found(FamilyCollectionAccount.objects.filter(school=membership.school, id=account_id), "collection account")
        data = body(request)
        if self.action == "suspend":
            account = collection_accounts.suspend(account, actor=membership, reason=data.get("reason"))
        elif self.action == "reinstate":
            account = collection_accounts.reinstate(account, actor=membership)
        elif self.action == "close":
            account = collection_accounts.close(account, actor=membership, reason=data.get("reason"))
        elif self.action == "mark-provisioned":
            account = collection_accounts.mark_provisioned(account, actor=membership)
        return Response({"account": serializers.account(account)})
