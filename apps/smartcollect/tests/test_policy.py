"""The collection policy: the school's defaults, the layers over them (family > batch > term > session > school), what each layer inherits,
where each value came from, reasons, expiry, and that nothing is ever edited or deleted."""

from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .. import policy
from ..constants import ExpiryKind
from ..errors import CollectionRefused
from ..models import CollectionAuditEvent, CollectionPolicyOverride
from .base import POLICY, CollectTestCase


class SchoolDefaultTests(CollectTestCase):
    def test_a_school_starts_with_the_documented_defaults(self):
        row = policy.school_policy(self.school)
        self.assertEqual(
            policy.school_values(row),
            {
                "account_mode": "static", "reuse_scope": "until_replaced", "reuse_count": None, "reuse_until": None,
                "settlement_action": "dormant_immediately", "grace_period_hours": None, "arrears_policy": "carry_forward",
                "eligibility_policy": "needs_override",
            },
        )
        self.assertEqual((row.default_provider_switch_policy, row.override_reason_policy), ("retire_when_settled", "required_sensitive"))

    def test_only_the_owner_or_a_policy_manager_can_change_it(self):
        for who in (self.maker, self.checker, self.members["teacher"], self.members["parent"]):
            with self.assertRaises(CollectionRefused) as caught:
                policy.update_school_policy(who, {"account_mode": "dynamic"})
            self.assertEqual(caught.exception.code, "not_policy_manager")
        self.give_duties(self.maker, POLICY)
        self.assertEqual(policy.update_school_policy(self.maker, {"account_mode": "dynamic"}).default_account_mode, "dynamic")

    def test_a_change_is_audited_with_what_each_field_was_and_became_and_an_unchanged_field_is_not(self):
        policy.update_school_policy(self.owner, {"account_mode": "dynamic", "arrears_policy": "carry_forward"})
        event = CollectionAuditEvent.objects.get(kind="school_policy_changed")
        self.assertEqual((event.detail["before"], event.detail["after"]), ({"account_mode": "static"}, {"account_mode": "dynamic"}))
        self.assertEqual(event.actor, self.owner)

    def test_a_wait_needs_a_waiting_period_the_school_chooses_and_none_is_assumed(self):
        with self.assertRaises(CollectionRefused) as caught:
            policy.update_school_policy(self.owner, {"settlement_action": "grace_then_close"})
        self.assertEqual(caught.exception.code, "policy_incomplete")
        row = policy.update_school_policy(self.owner, {"settlement_action": "grace_then_close", "grace_period_hours": 72})
        self.assertEqual(row.default_grace_period_hours, 72)

    def test_counts_and_dates_are_needed_where_the_scope_counts_or_dates(self):
        for values in ({"reuse_scope": "selected_terms"}, {"reuse_scope": "multiple_sessions"}, {"reuse_scope": "until_date"}):
            with self.assertRaises(CollectionRefused):
                policy.update_school_policy(self.owner, values)
        policy.update_school_policy(self.owner, {"reuse_scope": "selected_terms", "reuse_count": 2})
        policy.update_school_policy(self.owner, {"reuse_scope": "until_date", "reuse_until": "2027-07-31"})

    def test_nonsense_is_refused_with_the_fields_own_name(self):
        for values in (
            {"account_mode": "instant"}, {"grace_period_hours": "many"}, {"grace_period_hours": 0}, {"reuse_count": 99}, {"reuse_until": "soon"},
            {"nonsense": 1}, {"provider": "paystack"},
        ):
            with self.assertRaises(CollectionRefused):
                policy.update_school_policy(self.owner, values)

    def test_no_grace_period_is_hard_coded_anywhere(self):
        self.assertIsNone(policy.school_policy(self.school).default_grace_period_hours)


class LayerTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")
        self.other = self.make_family("Kabir")

    def resolve(self, **kw):
        return policy.resolve(self.school, session=self.session, term=self.term1, family=kw.pop("family", None), **kw)

    def test_with_no_override_every_field_is_the_schools_default(self):
        resolved = self.resolve(family=self.family)
        self.assertEqual({s["scope"] for s in resolved.sources.values()}, {"school"})
        self.assertEqual(resolved["account_mode"], "static")

    def test_each_layer_changes_only_what_it_says_and_the_rest_keeps_inheriting(self):
        policy.set_override(self.owner, scope="session", session=self.session, values={"arrears_policy": "current_term_only"}, reason="Year rule")
        policy.set_override(self.owner, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="")
        resolved = self.resolve(family=self.family)
        self.assertEqual((resolved["arrears_policy"], resolved["account_mode"], resolved["settlement_action"]), ("current_term_only", "dynamic", "dormant_immediately"))
        self.assertEqual(
            (resolved.sources["arrears_policy"]["scope"], resolved.sources["account_mode"]["scope"], resolved.sources["settlement_action"]["scope"]),
            ("session", "term", "school"),
        )
        self.assertEqual(set(resolved.overridden()), {"arrears_policy", "account_mode"})

    def test_the_nearest_layer_wins_family_over_batch_over_term_over_session_over_the_school(self):
        batch = self.new_batch()
        policy.update_school_policy(self.owner, {"settlement_action": "manual"})
        policy.set_override(self.owner, scope="session", session=self.session, values={"settlement_action": "close_immediately"}, reason="s")
        self.assertEqual(self.resolve(family=self.family)["settlement_action"], "close_immediately")
        policy.set_override(self.owner, scope="term", term=self.term1, values={"settlement_action": "dormant_immediately"}, reason="t")
        self.assertEqual(self.resolve(family=self.family)["settlement_action"], "dormant_immediately")
        policy.set_override(self.maker, scope="batch", batch=batch, values={"settlement_action": "provider_native"}, reason="b", allow_prepare=True)
        self.assertEqual(self.resolve(family=self.family, batch=batch)["settlement_action"], "provider_native")
        policy.set_override(self.owner, scope="family", family=self.family, values={"settlement_action": "manual"}, reason="f")
        resolved = self.resolve(family=self.family, batch=batch)
        self.assertEqual((resolved["settlement_action"], resolved.sources["settlement_action"]["scope"]), ("manual", "family"))
        # another family is untouched by the first family's override
        self.assertEqual(self.resolve(family=self.other, batch=batch)["settlement_action"], "provider_native")
        # and a term override for a term other than the one asked about does not apply
        self.assertEqual(policy.resolve(self.school, session=self.session, term=self.term2, family=self.other)["settlement_action"], "close_immediately")

    def test_a_family_or_term_or_batch_can_never_choose_a_different_provider(self):
        for field in ("provider", "connection", "provider_connection", "providerConnectionId"):
            with self.assertRaises(CollectionRefused) as caught:
                policy.set_override(self.owner, scope="family", family=self.family, values={field: "paystack"}, reason="x")
            self.assertEqual(caught.exception.code, "provider_not_overridable")
        for school_only in ("provider_switch_policy", "override_reason_policy"):
            with self.assertRaises(CollectionRefused):
                policy.set_override(self.owner, scope="term", term=self.term1, values={school_only: "manual"}, reason="x")

    def test_an_override_that_would_leave_a_policy_unfinished_is_refused(self):
        with self.assertRaises(CollectionRefused) as caught:
            policy.set_override(self.owner, scope="family", family=self.family, values={"settlement_action": "grace_then_dormant"}, reason="x")
        self.assertEqual(caught.exception.code, "policy_incomplete")
        self.assertFalse(CollectionPolicyOverride.objects.exists())
        policy.set_override(
            self.owner, scope="family", family=self.family, values={"settlement_action": "grace_then_dormant", "grace_period_hours": 48}, reason="x"
        )

    def test_an_override_needs_a_target(self):
        with self.assertRaises(CollectionRefused) as caught:
            policy.set_override(self.owner, scope="family", values={"account_mode": "dynamic"}, reason="x")
        self.assertEqual(caught.exception.code, "target_required")


class ReasonTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")

    def test_optional_never_asks(self):
        policy.update_school_policy(self.owner, {"override_reason_policy": "optional"})
        policy.set_override(self.owner, scope="family", family=self.family, values={"eligibility_policy": "include"}, reason="")

    def test_always_asks_even_for_a_harmless_override(self):
        policy.update_school_policy(self.owner, {"override_reason_policy": "required_always"})
        with self.assertRaises(CollectionRefused) as caught:
            policy.set_override(self.owner, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="")
        self.assertEqual(caught.exception.code, "reason_required")
        policy.set_override(self.owner, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="New arrangement")

    def test_sensitive_asks_for_family_overrides_and_for_eligibility_or_arrears_but_not_the_rest(self):
        policy.set_override(self.owner, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="")
        for kw in (
            dict(scope="family", family=self.family, values={"account_mode": "static"}),
            dict(scope="term", term=self.term2, values={"eligibility_policy": "include"}),
            dict(scope="session", session=self.session, values={"arrears_policy": "current_term_only"}),
        ):
            with self.assertRaises(CollectionRefused) as caught:
                policy.set_override(self.owner, reason="", **kw)
            self.assertEqual(caught.exception.code, "reason_required", kw)
            policy.set_override(self.owner, reason="A good reason", **kw)

    def test_a_reason_is_kept_with_the_override_and_a_very_long_one_is_refused(self):
        created = policy.set_override(
            self.owner, scope="family", family=self.family, values={"eligibility_policy": "include"}, reason="  Pays late but always pays  "
        )
        self.assertEqual(created.reason, "Pays late but always pays")
        with self.assertRaises(CollectionRefused):
            policy.set_override(self.owner, scope="family", family=self.family, values={"eligibility_policy": "exclude"}, reason="x" * 301)


class ExpiryTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")

    def value(self):
        return policy.resolve(self.school, session=self.session, term=self.term1, family=self.family)["account_mode"]

    def override(self, **kw):
        return policy.set_override(self.owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="r", **kw)

    def test_until_removed_lasts_until_someone_removes_it(self):
        created = self.override(expiry_kind=ExpiryKind.UNTIL_REMOVED)
        self.set_today(date(2030, 1, 1))
        self.assertEqual(self.value(), "dynamic")
        policy.remove_override(self.owner, created.id, reason="No longer needed")
        self.assertEqual(self.value(), "static")

    def test_a_date_lasts_through_that_day_and_no_longer(self):
        end = self.today + timedelta(days=3)
        self.override(expiry_kind=ExpiryKind.AT_DATE, expires_on=end)
        self.assertEqual(self.value(), "dynamic")
        self.set_today(end)
        self.assertEqual(self.value(), "dynamic")
        self.set_today(end + timedelta(days=1))
        self.assertEqual(self.value(), "static")

    def test_a_date_already_past_is_refused_and_a_date_is_required(self):
        for kw in ({"expires_on": self.today - timedelta(days=1)}, {"expires_on": None}, {"expires_on": "not a date"}):
            with self.assertRaises(CollectionRefused):
                self.override(expiry_kind=ExpiryKind.AT_DATE, **kw)

    def test_end_of_term_lasts_through_the_terms_last_day(self):
        self.override(expiry_kind=ExpiryKind.END_OF_TERM, expiry_term=self.term1)
        self.set_today(self.term1.ends_on)
        self.assertEqual(self.value(), "dynamic")
        self.set_today(self.term1.ends_on + timedelta(days=1))
        self.assertEqual(self.value(), "static")

    def test_end_of_term_with_no_term_named_uses_the_current_term(self):
        created = self.override(expiry_kind=ExpiryKind.END_OF_TERM)
        self.assertEqual(created.expiry_term, self.term1)

    def test_end_of_session_lasts_through_the_sessions_last_day(self):
        self.override(expiry_kind=ExpiryKind.END_OF_SESSION)
        self.set_today(self.session.ends_on)
        self.assertEqual(self.value(), "dynamic")
        self.set_today(self.session.ends_on + timedelta(days=1))
        self.assertEqual(self.value(), "static")

    def test_a_one_time_override_is_used_up_by_the_first_account_it_is_used_for(self):
        self.override(expiry_kind=ExpiryKind.ONE_TIME)
        self.assertEqual(self.value(), "dynamic")
        policy.consume_one_time(self.family)
        self.assertEqual(self.value(), "static")
        override = CollectionPolicyOverride.objects.get()
        self.assertIsNotNone(override.consumed_at)  # used up, and still on record

    def test_an_expired_override_stays_on_record(self):
        self.override(expiry_kind=ExpiryKind.AT_DATE, expires_on=self.today + timedelta(days=1))
        self.set_today(self.today + timedelta(days=5))
        self.assertEqual(self.value(), "static")
        self.assertEqual(CollectionPolicyOverride.objects.count(), 1)


class HistoryTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")

    def test_replacing_an_override_keeps_the_old_one_as_history(self):
        first = policy.set_override(self.owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="one")
        second = policy.set_override(self.owner, scope="family", family=self.family, values={"settlement_action": "manual"}, reason="two")
        first.refresh_from_db()
        self.assertEqual((first.removed_at is not None, first.removal_reason, second.removed_at), (True, "Replaced by a newer override.", None))
        self.assertEqual(CollectionPolicyOverride.objects.count(), 2)
        resolved = policy.resolve(self.school, session=self.session, term=self.term1, family=self.family)
        self.assertEqual((resolved["account_mode"], resolved["settlement_action"]), ("static", "manual"))  # only the live one applies

    def test_only_one_override_can_be_live_for_a_target_by_database_rule(self):
        policy.set_override(self.owner, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionPolicyOverride.objects.create(school=self.school, scope="term", term=self.term1, values={"account_mode": "static"})

    def test_an_override_has_exactly_the_target_its_scope_names(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionPolicyOverride.objects.create(school=self.school, scope="family", family=self.family, term=self.term1, values={"account_mode": "static"})
        with self.assertRaises(IntegrityError), transaction.atomic():
            CollectionPolicyOverride.objects.create(school=self.school, scope="term", values={"account_mode": "static"})

    def test_an_override_is_never_edited_or_deleted(self):
        created = policy.set_override(self.owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="one")
        created.values = {"account_mode": "static"}
        with self.assertRaises(ValidationError):
            created.save()
        with self.assertRaises(ValidationError):
            created.delete()

    def test_removal_is_audited_and_cannot_be_repeated(self):
        created = policy.set_override(self.owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="one")
        policy.remove_override(self.owner, created.id, reason="done with it")
        self.assertEqual(CollectionAuditEvent.objects.filter(kind="policy_override_removed").count(), 1)
        with self.assertRaises(CollectionRefused) as caught:
            policy.remove_override(self.owner, created.id)
        self.assertEqual(caught.exception.code, "already_removed")

    def test_setting_is_audited_with_the_values_the_reason_and_who(self):
        policy.set_override(self.owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="one")
        event = CollectionAuditEvent.objects.get(kind="policy_override_set")
        self.assertEqual((event.detail["scope"], event.detail["values"], event.detail["reason"], event.actor), ("family", {"account_mode": "dynamic"}, "one", self.owner))

    def test_the_audit_trail_is_append_only(self):
        policy.update_school_policy(self.owner, {"account_mode": "dynamic"})
        event = CollectionAuditEvent.objects.get()
        event.kind = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()


class AuthorityTests(CollectTestCase):
    def setUp(self):
        super().setUp()
        self.family = self.make_family("Bello")

    def test_the_maker_can_override_a_batch_but_not_the_rest(self):
        batch = self.new_batch()
        policy.set_override(self.maker, scope="batch", batch=batch, values={"account_mode": "dynamic"}, reason="", allow_prepare=True)
        for kw in (dict(scope="term", term=self.term1), dict(scope="session", session=self.session), dict(scope="family", family=self.family)):
            with self.assertRaises(CollectionRefused) as caught:
                policy.set_override(self.maker, values={"account_mode": "dynamic"}, reason="r", allow_prepare=True, **kw)
            self.assertEqual(caught.exception.code, "not_policy_manager", kw)

    def test_a_checker_cannot_override_anything(self):
        with self.assertRaises(CollectionRefused):
            policy.set_override(self.checker, scope="term", term=self.term1, values={"account_mode": "dynamic"}, reason="r")

    def test_another_schools_session_or_family_is_not_found(self):
        with self.assertRaises(CollectionRefused) as caught:
            policy.set_override(self.other_owner, scope="session", session=self.session, values={"account_mode": "dynamic"}, reason="r")
        self.assertEqual(caught.exception.code, "target_not_found")
        with self.assertRaises(CollectionRefused):
            policy.set_override(self.other_owner, scope="family", family=self.family, values={"account_mode": "dynamic"}, reason="r")
