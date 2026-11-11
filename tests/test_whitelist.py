# -*- coding: utf-8 -*-
"""会话白名单回归测试（对应 issue #2 维护者 review 要求）。

运行：python -m unittest discover -s tests -v
不依赖宿主/网络：maibot_sdk 缺失时用内置桩（pydantic 提供配置模型能力）。
覆盖：旧配置兼容、空白名单、群聊/私聊命中与未命中、同号隔离、标识缺失、
日志脱敏，以及「被拒绝的消息不会进入图片处理」。
"""
from __future__ import annotations

import asyncio
import base64
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import AsyncMock, Mock

try:
    import maibot_sdk  # noqa: F401
except ImportError:
    from enum import Enum

    from pydantic import BaseModel, Field

    sdk = types.ModuleType("maibot_sdk")
    sdk.Field = Field
    sdk.PluginConfigBase = BaseModel

    class MaiBotPlugin:
        def __init__(self):
            self._ctx = None
            self._plugin_config_instance = None

        @property
        def config(self):
            return self._plugin_config_instance

        @property
        def ctx(self):
            return self._ctx

    def HookHandler(*args, **kwargs):
        def deco(fn):
            return fn
        return deco

    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.HookHandler = HookHandler
    sdk_types = types.ModuleType("maibot_sdk.types")
    sdk_types.ErrorPolicy = Enum("ErrorPolicy", {"ABORT": 0, "SKIP": 1, "LOG": 2})
    sdk_types.HookMode = Enum("HookMode", {"BLOCKING": 0, "OBSERVE": 1})
    sdk_types.HookOrder = Enum("HookOrder", {"EARLY": 0, "NORMAL": 1, "LATE": 2})
    sdk.types = sdk_types
    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = sdk_types

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location("photo_geo_plugin", ROOT / "plugin.py")
plugin = module_from_spec(spec)
sys.modules["photo_geo_plugin"] = plugin  # pydantic 前向引用需要模块可查
spec.loader.exec_module(plugin)

GID = "123456789"
UID = "987654321"
IMG = base64.b64encode(b"\xff\xd8\xff\xd9").decode()


def set_config(p, cfg):
    if hasattr(p, "set_plugin_config"):  # 真 SDK：走官方注入+校验链路
        p.set_plugin_config(cfg.model_dump())
    else:
        p._plugin_config_instance = cfg


def make_plugin(**overrides):
    p = plugin.PhotoGeoPlugin()
    set_config(p, plugin.GeoConfig(plugin=plugin.BasicSection(**overrides)))
    p._ctx = Mock()
    p._ctx.logger = Mock()
    p._handle_photo = AsyncMock()
    return p


def make_message(gid=GID, uid=UID, group=True, with_group_id=True, with_user_id=True,
                 platform="qq", napcat_type=None, group_info_present=None):
    # napcat_type 缺省随 group；group_info_present 缺省随 group。
    # 复现「缺群号的群消息」：napcat_type="group" 但 group_info_present=False。
    if napcat_type is None:
        napcat_type = "group" if group else "private"
    if group_info_present is None:
        group_info_present = group
    info = {
        "user_info": {"user_id": uid if with_user_id else "",
                      "user_nickname": "测试群友"},
        "additional_config": {"self_id": "10001", "napcat_message_type": napcat_type},
    }
    if group_info_present:
        info["group_info"] = {"group_id": gid if with_group_id else "",
                              "group_name": "测试群"}
    else:
        info["group_info"] = None
    return {
        "session_id": "stream-1",
        "platform": platform,
        "raw_message": [{"type": "image",
                         "binary_data_base64": IMG, "hash": "testhash"}],
        "message_info": info,
    }


def rendered_logs(logger_mock):
    out = []
    for c in logger_mock.info.call_args_list + logger_mock.warning.call_args_list:
        try:
            out.append(str(c.args[0]) % tuple(c.args[1:]))
        except TypeError:
            out.append(str(c.args))
    return " ".join(out)


class WhitelistTokensTest(unittest.TestCase):
    def test_valid_entries_normalized(self):
        self.assertEqual(
            plugin._whitelist_tokens(["group:123", " private:456 ", "group: 789"]),
            {"group:123", "private:456", "group:789"})

    def test_invalid_entries_ignored(self):
        self.assertEqual(plugin._whitelist_tokens(
            ["123456", "group:", "private:abc", "room:123", "", "  ", "group:12x"]), set())

    def test_empty_and_none(self):
        self.assertEqual(plugin._whitelist_tokens([]), set())
        self.assertEqual(plugin._whitelist_tokens(None), set())

    def test_duplicates_collapse(self):
        self.assertEqual(plugin._whitelist_tokens(["group:123", "group:123"]), {"group:123"})


class WhitelistHookTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = make_plugin()

    async def asyncTearDown(self):
        for t in list(self.p._tasks):
            t.cancel()
        if self.p._client is not None:
            await self.p._client.aclose()

    async def run_hook(self, msg):
        await self.p.handle(msg)
        await asyncio.sleep(0)  # 让已派生的后台任务落地，便于断言

    async def assert_rejected_before_processing(self, msg):
        self.p._handle_photo.reset_mock()
        self.p._candidates = Mock(wraps=self.p._candidates)
        await self.run_hook(msg)
        self.p._candidates.assert_not_called()
        self.p._handle_photo.assert_not_awaited()

    async def assert_processed(self, msg):
        await self.run_hook(msg)
        self.p._handle_photo.assert_awaited_once()
        self.assertEqual(self.p._handle_photo.await_args.args[0], b"\xff\xd8\xff\xd9")

    # ---- 旧配置兼容 ----
    async def test_legacy_config_without_whitelist_fields(self):
        legacy = plugin.GeoConfig(plugin=plugin.BasicSection())
        self.assertFalse(legacy.plugin.whitelist_enabled)
        self.assertEqual(legacy.plugin.whitelist_chats, [])
        set_config(self.p, legacy)
        await self.assert_processed(make_message())

    async def test_disabled_whitelist_ignores_list(self):
        self.p = make_plugin(whitelist_enabled=False, whitelist_chats=["group:999"])
        await self.assert_processed(make_message())

    async def test_group_only_still_gates_private_when_disabled(self):
        self.p = make_plugin(group_only=True)
        await self.assert_rejected_before_processing(make_message(group=False))

    # ---- 空白名单 / 无效条目 ----
    async def test_enabled_empty_list_skips_all(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=[])
        await self.assert_rejected_before_processing(make_message())
        await self.assert_rejected_before_processing(make_message(group=False))

    async def test_enabled_all_invalid_entries_skips_all(self):
        self.p = make_plugin(whitelist_enabled=True,
                             whitelist_chats=[GID, "group:", "private:abc", "room:1"])
        await self.assert_rejected_before_processing(make_message())

    # ---- 群聊命中 / 未命中 ----
    async def test_group_hit(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=["group:" + GID])
        await self.assert_processed(make_message())

    async def test_group_miss(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=["group:999"])
        await self.assert_rejected_before_processing(make_message())

    async def test_group_message_never_matches_private_entry(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=["private:" + GID])
        await self.assert_rejected_before_processing(make_message())

    # ---- 私聊命中 / 未命中 ----
    async def test_private_hit_requires_group_only_false(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:" + UID])
        await self.assert_processed(make_message(group=False))

    async def test_private_blocked_by_group_only_even_if_listed(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=True,
                             whitelist_chats=["private:" + UID])
        await self.assert_rejected_before_processing(make_message(group=False))

    async def test_private_miss(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:999"])
        await self.assert_rejected_before_processing(make_message(group=False))

    async def test_private_never_matches_group_entry(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["group:" + UID])
        await self.assert_rejected_before_processing(make_message(group=False))

    # ---- 同号隔离 ----
    async def test_same_number_isolation(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["group:123"])
        await self.assert_processed(make_message(gid="123", uid="456"))
        await self.assert_rejected_before_processing(make_message(gid="456", uid="123"))
        await self.assert_rejected_before_processing(
            make_message(group=False, gid=None, uid="123"))
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:123"])
        await self.assert_processed(make_message(group=False, gid=None, uid="123"))
        await self.assert_rejected_before_processing(make_message(gid="123", uid="456"))

    # ---- 标识缺失 ----
    async def test_group_missing_gid_skips(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["group:" + GID, "private:" + UID])
        await self.assert_rejected_before_processing(make_message(with_group_id=False))

    async def test_private_missing_uid_skips(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:" + UID])
        await self.assert_rejected_before_processing(
            make_message(group=False, with_user_id=False))

    # ---- review① 缺群号的群消息（group_info 被省略、napcat_message_type=group）----
    async def test_group_marked_but_gid_missing_not_treated_as_private(self):
        # 维护者复现场景：group_only=false、发图人在私聊白名单里，消息明确标记为群
        # 但省略了 group_info —— 必须跳过，不能落入私聊分支误放行
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:" + UID])
        await self.assert_rejected_before_processing(
            make_message(napcat_type="group", group_info_present=False))

    async def test_group_marked_gid_missing_skips_even_if_group_listed(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["group:" + GID])
        await self.assert_rejected_before_processing(
            make_message(napcat_type="group", group_info_present=False))

    # ---- review② 跨平台同号（白名单仅对 QQ 平台生效）----
    async def test_telegram_private_same_number_not_matched(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:" + UID])
        await self.assert_rejected_before_processing(
            make_message(group=False, platform="telegram"))

    async def test_telegram_group_same_number_not_matched(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["group:" + GID])
        await self.assert_rejected_before_processing(make_message(platform="telegram"))

    async def test_qq_platform_still_matches(self):
        self.p = make_plugin(whitelist_enabled=True, group_only=False,
                             whitelist_chats=["private:" + UID])
        await self.assert_processed(make_message(group=False, platform="qq"))

    # ---- review③ 配置校验脱敏（未加引号的整数条目不得泄露原值）----
    async def test_integer_entries_do_not_leak_value(self):
        self.p = make_plugin(whitelist_enabled=True)
        if hasattr(self.p, "set_plugin_config"):
            # 真 SDK：直接注入未加引号的整数，复现热更新校验链路
            self.p.set_plugin_config({"plugin": {"config_version": "1.0.0",
                                                 "whitelist_enabled": True,
                                                 "whitelist_chats": [123456789]}})
        else:
            # 桩：构造模型同样经过 before 校验器
            set_config(self.p, plugin.GeoConfig(plugin=plugin.BasicSection(
                whitelist_enabled=True, whitelist_chats=[123456789])))
        entries = self.p.config.plugin.whitelist_chats
        self.assertEqual(entries, ["123456789"])
        self.assertEqual(plugin._whitelist_tokens(entries), set())
        self.p._log_whitelist_status()
        self.assertNotIn("123456789", rendered_logs(self.p.ctx.logger))

    # ---- 日志脱敏 ----
    async def test_log_contains_no_ids(self):
        self.p = make_plugin(whitelist_enabled=True,
                             whitelist_chats=["group:" + GID, "private:" + UID])
        self.p._log_whitelist_status()
        rendered = rendered_logs(self.p.ctx.logger)
        self.assertIn("有效条目=2", rendered)
        self.assertNotIn(GID, rendered)
        self.assertNotIn(UID, rendered)

    async def test_log_warns_once_when_no_valid_entries(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=[])
        self.p._log_whitelist_status()
        self.p.ctx.logger.warning.assert_called_once()
        self.p.ctx.logger.info.assert_not_called()

    async def test_config_update_logs_status(self):
        self.p = make_plugin(whitelist_enabled=True, whitelist_chats=["group:" + GID])
        await self.p.on_config_update("plugin", {}, "1.0.0")
        rendered = rendered_logs(self.p.ctx.logger)
        self.assertIn("有效条目=1", rendered)
        self.assertNotIn(GID, rendered)


if __name__ == "__main__":
    unittest.main()