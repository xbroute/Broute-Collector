import copy
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import telegram_admin_policy as policy
import telegram_destinations as destinations
import telegram_multi_publisher as publisher


def timestamp(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def server(identifier="a", **kwargs):
    return {"id": identifier, "protocol": "vless", "country": "DE", "transport": "ws", "tls": True,
            "status": "online", "valid": True, "latency": 30, "should_remove": False,
            "raw": f"vless://uuid-{identifier}@example.com:443?security=tls&type=ws", **kwargs}


def destination(**kwargs):
    return destinations.normalize_destination({"chat_id": -1001, "bot_status": "administrator", "enabled": True, **kwargs})


class AdminPolicyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_legacy_managers_migrate_to_owners_without_changing_plain_legacy_store(self):
        store = {"manager_user_ids": [7], "destinations": []}
        self.assertEqual(policy.effective_roles(store), {"7": "owner"})
        self.assertEqual(destinations.normalize_store(store), store)

    def test_explicit_empty_roles_do_not_resurrect_revoked_legacy_managers(self):
        store = {"manager_user_ids": [7], "administration": {"roles": {}}}
        self.assertEqual(policy.effective_roles(store), {})

    def test_role_permission_matrix(self):
        for role, permissions in policy.ROLE_PERMISSIONS.items():
            store = {"administration": {"roles": {"7": role}}}
            for permission in {"view", "operate", "configure", "team", "backup"}:
                self.assertEqual(policy.allowed(store, 7, permission), permission in permissions, (role, permission))
            self.assertFalse(policy.allowed(store, 99, "view"))

    def test_explicit_recovery_owner_cannot_be_demoted_by_stored_role(self):
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "7, 8"}):
            self.assertEqual(policy.effective_roles({"administration": {"roles": {"7": "viewer"}}}), {"7": "owner", "8": "owner"})

    def test_invalid_owner_environment_fails_closed(self):
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "7 nope"}):
            with self.assertRaises(policy.PolicyError):
                policy.effective_roles({})

    def test_malformed_admin_schema_is_rejected(self):
        for value in [None, [], {"version": 2}, {"roles": []}, {"roles": {"7": "god"}}, {"roles": {"-1": "owner"}}, {"audit": None}]:
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.normalize_admin(value, [])

    def test_new_policy_roundtrip_is_encrypted(self):
        store = {"manager_user_ids": [7], "destinations": [destination(daily_limit=10, schedule=policy.parse_schedule("22:00-02:00"))],
                 "administration": {"roles": {"7": "owner", "8": "viewer"}}}
        encrypted = destinations.encrypt_store(store, "secret")
        self.assertNotIn("viewer", encrypted["ciphertext"])
        self.assertEqual(destinations.decrypt_store(encrypted, "secret"), destinations.normalize_store(store))

    def test_default_publishing_policy_keeps_every_eligible_record(self):
        self.assertTrue(policy.server_matches(server(), {}))
        self.assertTrue(policy.server_matches(server(tls=False, latency=None), {}))

    def test_filters_are_composed_and_cleared_explicitly(self):
        filters = policy.parse_filters("protocol=vless,trojan country=DE,NL tls=on latency=250 transport=ws", policy.normalize_policy({})["filters"])
        dest = {"filters": filters}
        self.assertTrue(policy.server_matches(server(), dest))
        for changes in [{"country": "US"}, {"protocol": "ss"}, {"tls": False}, {"latency": None}, {"latency": 251}, {"transport": "grpc"}]:
            self.assertFalse(policy.server_matches(server(**changes), dest), changes)
        cleared = policy.parse_filters("protocol=* country=* transport=* tls=off latency=0", filters)
        self.assertEqual(cleared, policy.normalize_policy({})["filters"])

    def test_latency_zero_is_valid_but_non_finite_and_negative_latency_are_rejected(self):
        dest = {"filters": {"max_latency_ms": 50}}
        self.assertTrue(policy.server_matches(server(latency=0), dest))
        for value in [float("nan"), float("inf"), -1, "no"]:
            self.assertFalse(policy.server_matches(server(latency=value), dest))

    def test_hy2_filter_alias_matches_hysteria2(self):
        self.assertTrue(policy.server_matches(server(protocol="hysteria2"), {"filters": {"protocols": ["hy2"]}}))

    def test_blocked_records_are_excluded_even_with_no_filters(self):
        self.assertFalse(policy.server_matches(server(), {}, {"a"}))

    def test_invalid_filter_and_policy_values_are_rejected(self):
        for value in ["url=https://private", "protocol=invalid", "tls=yes", "country=Germany", "latency=-1", "latency=nan", "transport=evil", ""]:
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.parse_filters(value, policy.normalize_policy({})["filters"])
        for value in [{"daily_limit": -1}, {"daily_limit": 1001}, {"filters": []}, {"filters": {"tls_only": "true"}}, {"pause_until": float("inf")}]:
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.normalize_policy(value)

    def test_daytime_window_boundary_is_exclusive_at_close(self):
        dest = destination(schedule=policy.parse_schedule("09:00-23:00 UTC+03:30"))
        now = timestamp("2026-09-30T05:29:59")
        self.assertEqual(policy.next_allowed_at(dest, {}, now), timestamp("2026-09-30T05:30:00"))
        now = timestamp("2026-09-30T19:30:00")
        self.assertEqual(policy.next_allowed_at(dest, {}, now), timestamp("2026-10-01T05:30:00"))

    def test_overnight_window_crosses_midnight_correctly(self):
        dest = destination(schedule=policy.parse_schedule("22:00-02:00 UTC+00:00"))
        for iso in ["2026-09-30T22:00:00", "2026-10-01T01:59:59"]:
            now = timestamp(iso)
            self.assertEqual(policy.next_allowed_at(dest, {}, now), now)
        now = timestamp("2026-10-01T02:00:00")
        self.assertEqual(policy.next_allowed_at(dest, {}, now), timestamp("2026-10-01T22:00:00"))

    def test_daily_limit_and_schedule_wait_for_next_local_opening(self):
        dest = destination(daily_limit=2, schedule=policy.parse_schedule("09:00-23:00 UTC+03:30"))
        now = timestamp("2026-09-30T08:00:00")
        target = {"daily_date": "2026-09-30", "daily_sent": 2}
        self.assertEqual(policy.next_allowed_at(dest, target, now), timestamp("2026-10-01T05:30:00"))

    def test_quota_resets_at_iran_local_midnight_and_only_successful_sends_count(self):
        dest, target = destination(daily_limit=1), {}
        before = timestamp("2026-09-30T20:29:59")
        policy.record_send(target, dest, before)
        self.assertEqual(target["daily_date"], "2026-09-30")
        self.assertEqual(policy.next_allowed_at(dest, target, before), timestamp("2026-09-30T20:30:00"))
        policy.record_send(target, dest, timestamp("2026-09-30T20:30:00"))
        self.assertEqual(target["daily_sent"], 1)
        self.assertEqual(target["total_sent"], 2)

    def test_future_pause_past_quota_day_uses_that_future_days_quota(self):
        now = timestamp("2026-09-30T08:00:00")
        dest = destination(daily_limit=1, pause_until=int(timestamp("2026-10-02T08:00:00")))
        self.assertEqual(policy.next_allowed_at(dest, {"daily_date": "2026-09-30", "daily_sent": 1}, now), dest["pause_until"])

    def test_pause_suspension_and_queue_pacing_are_all_honored(self):
        dest = destination(pause_until=200)
        self.assertEqual(policy.next_allowed_at(dest, {"suspended_until": 400, "next_send_after": 300}, 100, 500), 500)

    def test_invalid_schedules_and_durations_are_rejected(self):
        for text in ["24:00-25:00", "09:00-09:00", "09:00-10:00 UTC+15:00", "09:00-10:00 UTC+03:80", "junk"]:
            with self.subTest(text=text), self.assertRaises(policy.PolicyError):
                policy.parse_schedule(text)
        self.assertIsNone(policy.parse_schedule("off"))
        for text in ["0m", "1s", "31d", "999999999h", "inf"]:
            with self.subTest(text=text), self.assertRaises(policy.PolicyError):
                policy.duration(text)
        self.assertEqual(policy.duration("2h"), 7200)

    def test_confirmation_is_bound_to_actor_chat_and_exact_expiry(self):
        admin = policy.administration({})
        token = policy.issue_confirmation(admin, actor=7, chat=7, action="target_remove", payload={"key": "abc"}, resource_digest="d", now=100)
        self.assertEqual(policy.confirmation(admin, token, 7, 7, 100 + policy.CONFIRM_TTL_SECONDS - 1)["action"], "target_remove")
        for actor, chat, now in [(8, 7, 101), (7, 8, 101), (7, 7, 100 + policy.CONFIRM_TTL_SECONDS)]:
            with self.assertRaises(policy.PolicyError):
                policy.confirmation(admin, token, actor, chat, now)

    def test_audit_is_bounded_and_cannot_accept_private_url_arguments(self):
        admin = policy.administration({})
        for i in range(220):
            policy.audit(admin, 7, "target_filter", "abc", i)
        self.assertEqual(len(admin["audit"]), 200)
        with self.assertRaises(policy.PolicyError):
            policy.audit(admin, 7, "source_add", "https://secret.example/token", 300)

    def test_backup_encryption_roundtrip_wrong_key_size_and_tampering(self):
        value = {"version": 1, "url": "https://example.com/secret-token"}
        data = policy.encrypt_backup(value, "secret")
        self.assertNotIn(b"secret-token", data)
        self.assertEqual(policy.decrypt_backup(data, ["new-secret", "secret"]), value)
        for bad in [data[:-2] + b"XX", b"not a backup", b"x" * (policy.MAX_BACKUP_BYTES * 2 + 1)]:
            with self.assertRaises(policy.PolicyError):
                policy.decrypt_backup(bad, ["secret"])
        with self.assertRaises(policy.PolicyError):
            policy.decrypt_backup(data, ["wrong"])

    def test_ambiguous_destination_prefix_is_rejected(self):
        a, b = destination(key="aa11"), destination(chat_id=-1002, key="aa22")
        self.assertIsNone(destinations.find_destination([a, b], "aa")[1])


class PublisherPolicyTests(unittest.TestCase):
    def test_filter_changes_remove_already_queued_records_without_clearing_history(self):
        target = publisher._default_target_state()
        target.update({"sent": ["historic"], "sent_fingerprints": ["historic-fp"], "queue": ["us", "de"]})
        dest = destination(filters={"countries": ["DE"]})
        publisher.prepare_target_state(target, [server("us", country="US"), server("de")], dest)
        self.assertEqual(target["queue"], ["de"])
        self.assertEqual(target["sent"], ["historic"])
        self.assertEqual(target["sent_fingerprints"], ["historic-fp"])

    def test_queue_retry_and_rebuild_apply_once_and_preserve_sent_history(self):
        target = publisher._default_target_state()
        target.update({"suspended_until": 9999, "next_send_after": 9999, "last_error": "bad", "sent": ["historic"], "queue": ["b", "a"]})
        dest = destination(retry_generation=1, queue_revision=1)
        publisher.prepare_target_state(target, [server("a"), server("b")], dest)
        self.assertEqual(target["suspended_until"], 0)
        self.assertEqual(target["queue"], ["a", "b"])
        target["next_send_after"] = 123
        publisher.prepare_target_state(target, [server("a"), server("b")], dest)
        self.assertEqual(target["next_send_after"], 123)
        self.assertEqual(target["sent"], ["historic"])

    def test_long_future_schedule_does_not_request_busy_successor_runs(self):
        now = timestamp("2026-09-30T12:00:00")
        dest = destination(schedule=policy.parse_schedule("22:00-23:00 UTC+00:00"))
        state = {"destinations": {str(dest["chat_id"]): {"queue": ["a"]}}}
        with patch.object(publisher.time, "time", return_value=now):
            self.assertFalse(publisher._has_work(state, [dest]))

    def test_paused_store_has_no_active_destinations(self):
        with patch.object(publisher.time, "time", return_value=100):
            self.assertEqual(publisher._active_destinations({"destinations": [destination()], "administration": {"pause_until": 200}}), [])

    def test_block_and_quota_are_rechecked_at_the_send_boundary(self):
        dest, srv, target = destination(daily_limit=1), server(), publisher._default_target_state()
        now = timestamp("2026-09-30T12:00:00")
        blocked = {"destinations": [dest], "administration": {"blocked_ids": ["a"]}}
        with patch.object(publisher, "ensure_global_enabled"), patch.object(publisher, "load_destinations_remote", return_value=blocked), patch.object(publisher.time, "time", return_value=now):
            with self.assertRaises(publisher.DestinationChanged):
                publisher.ensure_destination_unchanged(dest, target, srv)
        policy.record_send(target, dest, now)
        with patch.object(publisher, "ensure_global_enabled"), patch.object(publisher, "load_destinations_remote", return_value={"destinations": [dest]}), patch.object(publisher.time, "time", return_value=now):
            with self.assertRaises(publisher.DestinationChanged):
                publisher.ensure_destination_unchanged(dest, target, srv)

    def test_operational_counters_survive_normalization(self):
        target = publisher._default_target_state()
        policy.record_send(target, destination(), 1_800_000_000)
        self.assertEqual(publisher._normalize_target_state(copy.deepcopy(target)), target)

    def test_emoji_templates_respect_telegram_utf16_limit(self):
        with self.assertRaises(ValueError):
            publisher.render_target_message(destination(template="😀" * 2100 + "{config}"), server())


if __name__ == "__main__":
    unittest.main()
