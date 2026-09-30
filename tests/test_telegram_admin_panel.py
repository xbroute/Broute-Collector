import base64
import copy
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import telegram_admin_policy as policy
import telegram_bot_admin_extension as extension
import telegram_bot_destination_extension as destinations_ext
import telegram_destinations as destinations
import telegram_managed_sources as sources
import telegram_bot_control as control_base


class Retryable(RuntimeError):
    pass


class FakeControl(SimpleNamespace):
    def __init__(self, roles=None, legacy=False):
        super().__init__()
        self.records, self.messages, self.calls = {}, [], []
        self.enabled = False
        self.fail_write = False
        self.active = 0
        self.group_admins = {7}
        self.member = {"status": "administrator", "can_post_messages": True}
        self.BOT_TOKEN = "test-token"
        self.TARGET_CHAT_ID = -100900
        self.CONTROL_BRANCH = "main"
        self.PUBLISHER_STATE_PATH = "telegram_multi_state.json"
        self.PUBLISHER_STATE_BRANCH = "telegram-state"
        self.RetryableCommandError = Retryable
        self.update_to_action = lambda update: (None,) * 5
        self.process_action = lambda *args, **kwargs: None
        self.register_commands = lambda: None
        self.menu_keyboard = lambda: {"inline_keyboard": []}
        self.is_authorized_admin = lambda uid: False
        self.current_admin_ids = lambda: set()
        self.message_thread_id = lambda message: None
        self.normalize_command = lambda value: str(value or "").split(" ", 1)[0].lower()
        self.answer_callback = Mock()
        self.ensure_publisher_run = Mock()
        self.publisher_run_count = lambda: self.active
        self._json_request = Mock(return_value=(200, {"ok": True, "result": {}}))
        dest = destinations.normalize_destination({"chat_id": -1001, "title": "Private destination", "bot_status": "administrator", "chat_type": "channel", "enabled": True})
        store = {"manager_user_ids": list((roles or {}).keys()) if legacy else [], "destinations": [dest]}
        if not legacy:
            store["administration"] = {"roles": {str(uid): role for uid, role in (roles or {}).items()}}
        self.put_store(store)
        self.records[(sources.SOURCE_STATE_PATH, sources.SOURCE_STATE_BRANCH)] = sources.encrypt_sources([
            {"id": "aa1122334455", "url": "https://example.com/private-secret", "enabled": True, "last_validated_configs": 3}])

    def put_store(self, store):
        self.records[(destinations.DESTINATION_STATE_PATH, destinations.DESTINATION_STATE_BRANCH)] = destinations.encrypt_store(store)

    def get_store(self):
        return destinations.decrypt_store(self.records[(destinations.DESTINATION_STATE_PATH, destinations.DESTINATION_STATE_BRANCH)])

    def get_sources(self):
        return sources.decrypt_sources(self.records[(sources.SOURCE_STATE_PATH, sources.SOURCE_STATE_BRANCH)])

    def read_repo_json(self, path, branch, default):
        return copy.deepcopy(self.records.get((path, branch), default))

    def write_repo_json(self, path, branch, payload, message):
        if self.fail_write:
            raise Retryable("storage unavailable")
        self.records[(path, branch)] = copy.deepcopy(payload)
        self.calls.append(("write", path, branch))

    def current_enabled(self):
        return self.enabled

    def set_enabled(self, value):
        changed = self.enabled != value
        self.enabled = value
        return changed

    def send_text(self, chat, text, **kwargs):
        self.messages.append((chat, text, kwargs))

    safe_send_text = send_text

    def telegram_api(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == "getChatAdministrators":
            return [{"user": {"id": uid, "is_bot": False}} for uid in self.group_admins]
        if method == "getMe":
            return {"id": 999, "is_bot": True}
        if method == "getChat":
            return {"type": "channel", "title": "Private destination"}
        if method == "getChatMember":
            return copy.deepcopy(self.member)
        if method in {"setMyCommands", "sendMessage"}:
            return {}
        raise AssertionError(method)

    def github_api(self, path, **kwargs):
        self.calls.append(("github", path, kwargs))
        if path.startswith("/actions/"):
            return 200, {"workflow_runs": []}
        if path.startswith("/contents/data/servers.json"):
            server = {"id": "a", "protocol": "vless", "country": "DE", "country_name": "Germany", "tls": True,
                      "latency": 10, "transport": "ws", "status": "online", "valid": True, "should_remove": False,
                      "raw": "vless://uuid@example.com:443?security=tls&type=ws"}
            return 200, {"content": base64.b64encode(json.dumps([server]).encode()).decode()}
        raise AssertionError(path)


class AdminPanelTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_SOURCE_ENCRYPTION_KEY": "",
                                      "TELEGRAM_DESTINATION_ENCRYPTION_KEY": "", "TELEGRAM_OWNER_USER_IDS": ""})
        env.start()
        self.addCleanup(env.stop)
        self.fake = FakeControl({7: "owner", 8: "admin", 9: "operator", 10: "viewer"})
        self.install(self.fake)

    def install(self, fake):
        restore_targets = destinations_ext.install(fake)
        restore_admin = extension.install(fake)
        self.addCleanup(restore_targets)
        self.addCleanup(restore_admin)

    def send(self, text="", actor=7, chat=None, chat_type="private", document=None, callback=None, fake=None):
        fake = fake or self.fake
        message = {"from": {"id": actor}, "chat": {"id": actor if chat is None else chat, "type": chat_type}, "text": text}
        if document is not None:
            message["document"] = document
        update = {"message": message, "update_id": 100}
        if callback is not None:
            update = {"callback_query": {"id": "cb", "from": {"id": actor}, "message": message, "data": callback}}
        action, user_id, chat_id, thread_id, cb_id = fake.update_to_action(update)
        if action:
            fake.process_action(action, user_id=user_id, chat_id=chat_id, thread_id=thread_id, callback_id=cb_id)
        return fake.messages[-1][1] if fake.messages else ""

    def token(self):
        return next(iter(self.fake.get_store()["administration"]["confirmations"]))

    def confirm(self, token=None, **kwargs):
        return self.send(callback="adm:confirm:" + (token or self.token()), **kwargs)

    def test_all_role_dashboards_and_callbacks_fit_telegram_limits(self):
        for actor in (7, 8, 9, 10):
            self.send("/admin", actor=actor)
            self.send("/targets", actor=actor)
            self.send("/target_show 1", actor=actor)
        for _, text, kwargs in self.fake.messages:
            self.assertLessEqual(len(text.encode("utf-16-le"))//2, 3500)
            for row in (kwargs.get("keyboard") or {}).get("inline_keyboard", []):
                for button in row:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_viewer_cannot_mutate_using_command_old_callback_or_new_prompt(self):
        before = copy.deepcopy(self.fake.records)
        for kwargs in [{"text": "/publisher_on"}, {"text": "/target_interval 1 60"}, {"callback": "target:on:" + self.fake.get_store()["destinations"][0]["key"]},
                       {"callback": "adm:prompt:source_add"}, {"text": "/source_remove 1"}, {"text": "/buy_text changed"}, {"text": "/backup"}]:
            self.send(actor=10, **kwargs)
        self.assertEqual(self.fake.records, before)
        self.assertFalse(self.fake.enabled)

    def test_operator_can_stop_and_retry_but_cannot_change_filters_or_team(self):
        self.fake.enabled = True
        self.send("/publisher_off", actor=9)
        self.assertFalse(self.fake.enabled)
        self.send("/queue_retry 1", actor=9)
        self.assertEqual(self.fake.get_store()["destinations"][0]["retry_generation"], 1)
        self.send("/target_filter 1 protocol=trojan", actor=9)
        self.assertEqual(self.fake.get_store()["destinations"][0]["filters"]["protocols"], [])
        self.send("/admin_add 20 viewer", actor=9)
        self.assertNotIn("20", self.fake.get_store()["administration"]["roles"])

    def test_admin_cannot_manage_owners_or_backup_restore(self):
        for command in ["/admin_add 20 owner", "/admin_remove 7", "/backup", "/restore"]:
            text = self.send(command, actor=8)
            self.assertIn("اجازه", text)

    def test_unknown_user_only_receives_own_identity_and_no_configuration(self):
        self.assertIn("11", self.send("/whoami", actor=11))
        text = self.send("/targets", actor=11)
        self.assertNotIn("Private destination", text)
        self.assertIn("دسترسی", text)
        self.assertNotIn("11", self.fake.get_store()["administration"]["roles"])

    def test_commands_in_group_cannot_change_or_expose_management(self):
        before = copy.deepcopy(self.fake.records)
        count = len(self.fake.messages)
        self.send("/publisher_on", chat=-1001, chat_type="supergroup")
        self.send("/sources", chat=-1001, chat_type="supergroup")
        self.assertFalse(self.fake.enabled)
        self.assertEqual(self.fake.records, before)
        self.assertEqual(len(self.fake.messages), count)

    def test_target_remove_requires_single_use_confirmation(self):
        self.send("/target_remove 1")
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)
        token = self.token()
        self.confirm(token)
        self.assertEqual(self.fake.get_store()["destinations"], [])
        text = self.confirm(token)
        self.assertIn("معتبر نیست", text)
        self.assertEqual(self.fake.get_store()["administration"]["audit"][-1]["action"], "target_remove")

    def test_confirmation_cannot_be_used_by_other_user_or_chat(self):
        self.send("/target_remove 1")
        token = self.token()
        self.confirm(token, actor=8)
        self.confirm(token, actor=7, chat=123)
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_expired_confirmation_is_rejected(self):
        with patch.object(extension.time, "time", return_value=1000):
            self.send("/target_remove 1")
        with patch.object(extension.time, "time", return_value=1000 + policy.CONFIRM_TTL_SECONDS):
            self.confirm()
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_stale_target_confirmation_cannot_remove_changed_settings(self):
        self.send("/target_remove 1")
        token = self.token()
        self.send("/target_interval 1 60")
        self.assertIn("تغییر", self.confirm(token))
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_revoked_permission_is_rechecked_at_confirmation(self):
        self.send("/target_remove 1", actor=8)
        token = self.token()
        store = self.fake.get_store()
        store["administration"]["roles"]["8"] = "viewer"
        self.fake.put_store(store)
        self.confirm(token, actor=8)
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_last_owner_cannot_be_removed_or_demoted(self):
        self.send("/admin_remove 7")
        self.assertIn("آخرین مالک", self.confirm())
        self.send("/admin_add 7 viewer")
        token = list(self.fake.get_store()["administration"]["confirmations"])[-1]
        self.assertIn("آخرین مالک", self.confirm(token))
        self.assertEqual(self.fake.get_store()["administration"]["roles"]["7"], "owner")

    def test_owner_can_add_and_remove_other_roles_with_audit(self):
        self.send("/admin_add 20 operator")
        self.confirm()
        self.assertEqual(self.fake.get_store()["administration"]["roles"]["20"], "operator")
        self.send("/admin_remove 20")
        self.confirm()
        self.assertNotIn("20", self.fake.get_store()["administration"]["roles"])

    def test_explicit_recovery_owner_cannot_be_removed(self):
        with patch.dict(os.environ, {"TELEGRAM_OWNER_USER_IDS": "7"}):
            self.send("/admin_remove 7")
            self.assertIn("Secret", self.confirm())

    def test_sessions_are_durable_and_use_stable_target_keys(self):
        key = self.fake.get_store()["destinations"][0]["key"]
        self.send(callback="adm:prompt:target_interval " + key)
        self.assertIn("7", self.fake.get_store()["administration"]["sessions"])
        self.send("2m-4m")
        item = self.fake.get_store()["destinations"][0]
        self.assertEqual((item["min_delay_seconds"], item["max_delay_seconds"]), (120, 240))
        self.assertNotIn("7", self.fake.get_store()["administration"]["sessions"])

    def test_invalid_prompt_value_retains_session_for_correction(self):
        key = self.fake.get_store()["destinations"][0]["key"]
        self.send(callback="adm:prompt:target_quota " + key)
        self.send("oops")
        self.assertIn("7", self.fake.get_store()["administration"]["sessions"])
        self.send("100")
        self.assertEqual(self.fake.get_store()["destinations"][0]["daily_limit"], 100)

    def test_expired_session_does_not_apply_input(self):
        with patch.object(extension.time, "time", return_value=1000):
            self.send(callback="adm:prompt:target_quota " + self.fake.get_store()["destinations"][0]["key"])
        with patch.object(extension.time, "time", return_value=1001 + policy.SESSION_TTL_SECONDS):
            self.send("100")
        self.assertEqual(self.fake.get_store()["destinations"][0]["daily_limit"], 0)

    def test_cancel_clears_only_callers_sessions_and_approvals(self):
        self.send("/target_remove 1", actor=8)
        self.send("/target_remove 1", actor=7)
        self.send("/cancel", actor=7)
        requests = self.fake.get_store()["administration"]["confirmations"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(next(iter(requests.values()))["actor"], 8)

    def test_multiline_template_filter_schedule_and_quota_are_persisted(self):
        self.send("/target_template 1\nسلام {country}\n{config}")
        self.send("/target_filter 1 protocol=vless country=DE tls=on latency=50")
        self.send("/target_schedule 1 22:00-02:00 UTC+03:30")
        self.send("/target_quota 1 10")
        item = self.fake.get_store()["destinations"][0]
        self.assertEqual(item["template"], "سلام {country}\n{config}")
        self.assertEqual(item["filters"]["countries"], ["DE"])
        self.assertEqual(item["schedule"]["start"], 1320)
        self.assertEqual(item["daily_limit"], 10)

    def test_topic_cannot_be_set_for_a_channel(self):
        text = self.send("/target_topic 1 123")
        self.assertIn("Forum", text)
        self.assertIsNone(self.fake.get_store()["destinations"][0]["message_thread_id"])

    def test_topic_is_accepted_only_after_actual_forum_check(self):
        original_api = self.fake.telegram_api
        with patch.object(self.fake, "telegram_api", side_effect=lambda method, payload: {"is_forum": True} if method == "getChat" else original_api(method, payload)):
            self.send("/target_topic 1 123")
        self.assertEqual(self.fake.get_store()["destinations"][0]["message_thread_id"], 123)

    def test_rollback_restores_previous_settings_and_forces_destination_off(self):
        self.send("/target_interval 1 2m-4m")
        self.send("/target_rollback 1")
        self.confirm()
        item = self.fake.get_store()["destinations"][0]
        self.assertEqual((item["min_delay_seconds"], item["max_delay_seconds"]), (30, 90))
        self.assertFalse(item["enabled"])

    def test_actual_channel_permission_is_verified_before_enabling(self):
        self.fake.member["can_post_messages"] = False
        self.send("/target_on 1", actor=9)
        item = self.fake.get_store()["destinations"][0]
        self.assertFalse(item["enabled"])
        self.assertEqual(item["bot_status"], "cannot_post")

    def test_existing_admin_bot_can_be_registered_without_new_membership_event(self):
        original = self.fake.telegram_api
        with patch.object(self.fake, "telegram_api", side_effect=lambda method, payload: {"id": -2002, "title": "Existing channel", "type": "channel"} if method == "getChat" else original(method, payload)):
            self.send("/target_register @existing_channel")
        items = self.fake.get_store()["destinations"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[-1]["chat_id"], -2002)
        self.assertFalse(items[-1]["enabled"])
        self.assertEqual(items[-1]["bot_status"], "administrator")

    def test_destination_registration_requires_actor_to_be_chat_admin(self):
        original = self.fake.telegram_api
        self.fake.group_admins = {99}
        with patch.object(self.fake, "telegram_api", side_effect=lambda method, payload: {"id": -2002, "title": "Existing", "type": "channel"} if method == "getChat" else original(method, payload)):
            self.assertIn("ادمین همان", self.send("/target_register -2002"))
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_registration_refuses_destination_without_bot_send_permission(self):
        original = self.fake.telegram_api
        self.fake.member["can_post_messages"] = False
        with patch.object(self.fake, "telegram_api", side_effect=lambda method, payload: {"id": -2002, "title": "Existing", "type": "channel"} if method == "getChat" else original(method, payload)):
            self.send("/target_register -2002")
        self.assertEqual(len(self.fake.get_store()["destinations"]), 1)

    def test_bulk_off_is_immediate_and_bulk_on_requires_confirmation_and_permissions(self):
        self.send("/targets_off", actor=9)
        self.assertFalse(self.fake.get_store()["destinations"][0]["enabled"])
        self.send("/targets_on", actor=8)
        self.assertFalse(self.fake.get_store()["destinations"][0]["enabled"])
        self.confirm(actor=8)
        self.assertTrue(self.fake.get_store()["destinations"][0]["enabled"])

    def test_source_pause_rename_and_test_never_echo_secret_url(self):
        self.send("/source_off 1")
        self.send("/source_name 1 Example source")
        with patch.object(extension, "validate_subscription", return_value=("url", 12)):
            self.send("/source_check 1")
        self.send("/source_show 1", actor=10)
        item = self.fake.get_sources()[0]
        self.assertFalse(item["enabled"])
        self.assertEqual(item["name"], "Example source")
        self.assertEqual(item["last_validated_configs"], 12)
        self.assertNotIn("private-secret", repr(self.fake.messages))
        self.assertNotIn("private-secret", repr(self.fake.get_store()["administration"]["audit"]))

    def test_failed_source_test_is_reported_without_exposing_exception_url(self):
        with patch.object(extension, "validate_subscription", side_effect=sources.SourceValidationError("https://secret-token")):
            self.send("/source_check 1")
        self.assertEqual(self.fake.get_sources()[0]["health"], "failed")
        self.assertNotIn("secret-token", repr(self.fake.messages))

    def test_source_add_uses_hardened_validation_and_dispatches_collector(self):
        with patch.object(extension, "validate_subscription", return_value=("https://example.com/new", 5)) as validate:
            self.send("https://example.com/new")
        validate.assert_called_once()
        self.assertEqual(len(self.fake.get_sources()), 2)
        self.assertTrue(any("dispatches" in str(call) for call in self.fake.calls))

    def test_malformed_source_url_is_rejected_before_network_or_storage(self):
        before = copy.deepcopy(self.fake.records)
        with patch.object(extension, "validate_subscription") as network:
            for url in ["https://example.com/sub\x00secret", "https://example.com/\x7f", "https://" + "x"*70 + ".com/sub", "https://example.com/" + "x"*8200]:
                self.send("/source_add " + url)
            network.assert_not_called()
        self.assertEqual(self.fake.records, before)

    def test_source_remove_is_confirmed_and_history_file_is_never_written(self):
        self.send("/source_remove 1")
        self.assertEqual(len(self.fake.get_sources()), 1)
        self.confirm()
        self.assertEqual(self.fake.get_sources(), [])
        self.assertFalse(any(call[:2] == ("write", self.fake.PUBLISHER_STATE_PATH) for call in self.fake.calls))

    def test_source_removal_retry_finishes_audit_without_deleting_next_source(self):
        self.send("/source_remove 1")
        token = self.token()
        self.fake.records[(sources.SOURCE_STATE_PATH, sources.SOURCE_STATE_BRANCH)] = sources.encrypt_sources([
            {"id": "bb1122334455", "url": "https://example.com/second", "enabled": True}])
        self.confirm(token)
        self.assertEqual(self.fake.get_sources()[0]["id"], "bb1122334455")
        self.assertNotIn(token, self.fake.get_store()["administration"]["confirmations"])

    def test_uncertain_delivery_resolution_is_confirmed_and_never_writes_publisher_state(self):
        held = {"server_id": "a", "fingerprint": "fingerprint", "at": 1_800_000_000}
        state = {"destinations": {"-1001": {"uncertain_deliveries": {"a": held}}}}
        self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)] = copy.deepcopy(state)
        self.send("/queue_resolve 1 a retry", actor=9)
        self.assertEqual(self.fake.get_store()["destinations"][0]["delivery_resolutions"], {})
        self.confirm(actor=9)
        self.assertEqual(self.fake.get_store()["destinations"][0]["delivery_resolutions"]["a"]["decision"], "retry")
        self.assertEqual(self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)], state)

    def test_resolution_rejects_attempt_changed_since_approval(self):
        state = {"destinations": {"-1001": {"uncertain_deliveries": {"a": {"server_id": "a", "fingerprint": "one", "at": 100}}}}}
        self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)] = state
        self.send("/queue_resolve 1 a sent")
        state["destinations"]["-1001"]["uncertain_deliveries"]["a"]["at"] = 101
        self.confirm()
        self.assertEqual(self.fake.get_store()["destinations"][0]["delivery_resolutions"], {})

    def test_queue_controls_never_write_publisher_history(self):
        self.send("/queue_retry 1")
        self.send("/queue_rebuild 1")
        self.send("/config_block a")
        self.assertFalse(any(call[:2] == ("write", self.fake.PUBLISHER_STATE_PATH) for call in self.fake.calls))

    def test_preview_only_sends_to_requesting_private_chat_without_state_change(self):
        before = copy.deepcopy(self.fake.records)
        self.send("/target_preview 1", actor=10)
        sends = [payload for name, *rest in self.fake.calls if name == "sendMessage" for payload in rest]
        self.assertEqual(sends[-1]["chat_id"], 10)
        self.assertEqual(self.fake.records, before)

    def test_backup_is_encrypted_and_excludes_tokens_approvals_and_operational_history(self):
        self.send("/target_remove 1")
        self.send("/backup")
        body = self.fake._json_request.call_args.kwargs["raw_body"]
        self.assertNotIn(b"private-secret", body)
        self.assertIn(b"BROUTE-BACKUP-1", body)
        payload = extension.AdminPanel(self.fake).backup_payload(self.fake.get_store())
        self.assertEqual(payload["destinations"]["administration"]["confirmations"], {})
        self.assertNotIn("sent", payload)
        self.assertNotIn("test-token", json.dumps(payload))

    def test_restore_requires_master_off_and_no_running_publisher(self):
        self.fake.enabled = True
        self.assertIn("خاموش", self.send("/restore"))
        self.fake.enabled, self.fake.active = False, 1
        self.assertIn("اجرای ناشر", self.send("/restore"))

    def test_unsolicited_document_is_not_downloaded(self):
        with patch.object(extension, "download_backup") as download:
            self.send(document={"file_id": "document", "file_size": 100})
            download.assert_not_called()

    def test_restore_validates_then_confirms_and_preserves_operational_history(self):
        self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)] = {"destinations": {"-1001": {"sent": ["historic"]}}}
        snapshot = copy.deepcopy(self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)])
        panel = extension.AdminPanel(self.fake)
        backup = policy.encrypt_backup(panel.backup_payload(self.fake.get_store()), "test-token")
        self.send("/restore")
        with patch.object(extension, "download_backup", return_value=backup):
            self.send(document={"file_id": "document", "file_size": len(backup)})
        self.assertTrue(self.fake.get_store()["destinations"][0]["enabled"])
        self.confirm()
        self.assertFalse(self.fake.get_store()["destinations"][0]["enabled"])
        self.assertEqual(self.fake.records[(self.fake.PUBLISHER_STATE_PATH, self.fake.PUBLISHER_STATE_BRANCH)], snapshot)

    def test_restore_rechecks_master_after_confirmation_request(self):
        panel = extension.AdminPanel(self.fake)
        value = extension.validate_backup(panel.backup_payload(self.fake.get_store()))
        panel.ask(self.fake.get_store(), 7, 7, "restore", {"backup": value,
                  "sources_digest": policy.digest(self.fake.get_sources()),
                  "promo_digest": policy.digest(extension.promo._current_promo(self.fake))}, panel.restore_digest(self.fake.get_store()), "restore?")
        self.fake.enabled = True
        self.assertIn("خاموش", self.confirm())
        self.assertTrue(self.fake.get_store()["destinations"][0]["enabled"])

    def test_restore_rejects_unrelated_source_edit_after_approval(self):
        panel = extension.AdminPanel(self.fake)
        backup = policy.encrypt_backup(panel.backup_payload(self.fake.get_store()), "test-token")
        self.send("/restore")
        with patch.object(extension, "download_backup", return_value=backup):
            self.send(document={"file_id": "document", "file_size": len(backup)})
        self.send("/source_name 1 Changed")
        self.assertIn("تغییر", self.confirm())
        self.assertTrue(self.fake.get_store()["destinations"][0]["enabled"])

    def test_backup_restore_does_not_replay_old_delivery_approvals(self):
        panel = extension.AdminPanel(self.fake)
        value = panel.backup_payload(self.fake.get_store())
        value["destinations"]["destinations"][0]["delivery_resolutions"] = {"a": {"nonce": "a"*16, "decision": "retry"}}
        restored = extension.validate_backup(value)
        self.assertEqual(restored["destinations"]["destinations"][0]["delivery_resolutions"], {})

    def test_invalid_backup_rejected_before_any_write(self):
        panel = extension.AdminPanel(self.fake)
        value = panel.backup_payload(self.fake.get_store())
        value["destinations"]["destinations"][0]["template"] = "missing config"
        before = copy.deepcopy(self.fake.records)
        with self.assertRaises(ValueError):
            panel.apply_restore(self.fake.get_store(), 7, 7, "token", value)
        self.assertEqual(self.fake.records, before)

    def test_persistence_error_is_retryable_and_never_acknowledged_as_success(self):
        self.fake.fail_write = True
        with self.assertRaises(Retryable):
            self.send("/target_interval 1 2m")
        self.assertEqual(self.fake.get_store()["destinations"][0]["min_delay_seconds"], 30)

    def test_reply_failure_does_not_silently_discard_command(self):
        with patch.object(self.fake, "send_text", side_effect=RuntimeError("offline")):
            with self.assertRaises(Retryable):
                self.send("/admin")

    def test_registration_scopes_commands_to_private_chats(self):
        self.fake.register_commands()
        calls = [call for call in self.fake.calls if call[0] == "setMyCommands"]
        self.assertEqual(calls[-1][1]["scope"], {"type": "all_private_chats"})

    def test_arbitrary_group_promoter_cannot_bootstrap_owner(self):
        fake = FakeControl({})
        fake.group_admins = {7}
        self.install(fake)
        event = {"my_chat_member": {"from": {"id": 99}, "chat": {"id": -10099, "type": "supergroup", "title": "Untrusted"},
                                   "new_chat_member": {"status": "administrator"}}}
        action, uid, chat, thread, cb = fake.update_to_action(event)
        fake.process_action(action, user_id=uid, chat_id=chat, thread_id=thread, callback_id=cb)
        self.assertEqual(fake.get_store()["administration"]["roles"], {})
        self.assertEqual(len(fake.get_store()["destinations"]), 1)

    def test_verified_control_group_admin_can_bootstrap_private_panel(self):
        fake = FakeControl({})
        self.install(fake)
        self.send("/start", fake=fake)
        self.assertEqual(fake.get_store()["administration"]["roles"], {"7": "owner"})

    def test_legacy_owner_migration_preserves_destinations(self):
        fake = FakeControl({7: "owner"}, legacy=True)
        original = fake.get_store()["destinations"]
        self.install(fake)
        self.send("/publisher_off", fake=fake)
        self.assertEqual(fake.get_store()["administration"]["roles"], {"7": "owner"})
        self.assertEqual(fake.get_store()["destinations"], original)

    def test_non_admin_membership_event_disables_known_destination_even_for_unknown_actor(self):
        event = {"my_chat_member": {"from": {"id": 99}, "chat": {"id": -1001, "type": "channel"},
                                   "new_chat_member": {"status": "left"}}}
        action, uid, chat, thread, cb = self.fake.update_to_action(event)
        self.fake.process_action(action, user_id=uid, chat_id=chat, thread_id=thread, callback_id=cb)
        self.assertFalse(self.fake.get_store()["destinations"][0]["enabled"])

    def test_raw_request_supports_multipart_and_rejects_two_bodies(self):
        with self.assertRaises(ValueError):
            control_base._json_request("https://api.telegram.org", payload={}, raw_body=b"data")

    def test_malformed_admin_command_does_not_block_later_emergency_off_in_real_consumer(self):
        updates = [{"update_id": 1, "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/target_quota 1 invalid"}},
                   {"update_id": 2, "message": {"from": {"id": 7}, "chat": {"id": 7, "type": "private"}, "text": "/publisher_off"}}]
        with patch.object(control_base, "BOT_TOKEN", "test"), patch.object(control_base, "GH_TOKEN", "test"), \
             patch.object(control_base, "register_commands"), patch.object(control_base, "load_bot_state", return_value={"last_update_id": 0}), \
             patch.object(control_base, "save_bot_state") as offset, patch.object(control_base, "telegram_api", return_value=updates), \
             patch.object(control_base, "update_to_action", self.fake.update_to_action), patch.object(control_base, "process_action", self.fake.process_action):
            self.fake.enabled = True
            self.assertEqual(control_base.main(), 0)
        self.assertFalse(self.fake.enabled)
        self.assertEqual(offset.call_args.args[0]["last_update_id"], 2)

    def test_poll_budget_defers_updates_without_advancing_their_offset(self):
        updates = [{"update_id": 5}, {"update_id": 6}]
        with patch.object(control_base, "BOT_TOKEN", "test"), patch.object(control_base, "GH_TOKEN", "test"), \
             patch.object(control_base, "register_commands"), patch.object(control_base, "load_bot_state", return_value={"last_update_id": 0}), \
             patch.object(control_base, "save_bot_state") as offset, patch.object(control_base, "telegram_api", return_value=updates), \
             patch.object(control_base, "update_to_action", return_value=(None,)*5), \
             patch.object(control_base.time, "monotonic", side_effect=[100, 100, 320]):
            self.assertEqual(control_base.main(), 0)
        self.assertEqual(offset.call_count, 1)
        self.assertEqual(offset.call_args.args[0]["last_update_id"], 5)


if __name__ == "__main__":
    unittest.main()
