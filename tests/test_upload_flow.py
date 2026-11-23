"""Fast, no-secret regression tests for the Telegram conversation flow.

Load selected functions via AST: importing bot.py would start Telegram polling
and require credentials. This checks the real function bodies without doing so.
"""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


class BadRequest(Exception):
    pass


class TimedOut(Exception):
    pass


class NetworkError(Exception):
    pass


SOURCE = Path(__file__).resolve().parents[1] / "bot.py"
WANTED = {
    "ask_next", "_finish_queued_file", "_safe_callback_answer",
    "on_upload_folder_button", "_welcome_text", "cancel_command",
}


def get_code():
    parsed = ast.parse(SOURCE.read_text(encoding="utf-8"))
    selected = [node for node in parsed.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in WANTED]
    assert {node.name for node in selected} == WANTED
    return compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec")


FUNCTION_CODE = get_code()


class UploadFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = {
            "BadRequest": BadRequest,
            "TimedOut": TimedOut,
            "NetworkError": NetworkError,
            "Update": object,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
        }
        exec(FUNCTION_CODE, self.ns)

    def _item(self, folder=None):
        return {
            "file_id": "telegram-id", "token": "42", "uid": 123,
            "owner_name": "Student", "label": "مراجعة.ogg",
            "target_folder": folder, "target_name": "محاضرات",
        }

    async def test_welcome_explains_voice_folders_and_identity(self):
        welcome = self.ns["_welcome_text"]()
        for phrase in ("Ctrl+Tea", "فويس", "المجلد", "الاسم الأصلي"):
            self.assertIn(phrase, welcome)

    async def test_first_prompt_is_folder_picker(self):
        picker = AsyncMock()
        self.ns["_show_upload_folders"] = picker
        ud = {"queue": [self._item()]}
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await self.ns["ask_next"](ctx, 77, ud)
        picker.assert_awaited_once_with(ctx, 77, ud["queue"][0])
        ctx.bot.send_message.assert_not_awaited()

    async def test_name_is_requested_only_after_folder_selection(self):
        self.ns["_show_upload_folders"] = AsyncMock()
        self.ns["kb_status"] = lambda token=None: "buttons"
        item = self._item(folder="folder-1")
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await self.ns["ask_next"](ctx, 77, {"queue": [item]})
        self.ns["_show_upload_folders"].assert_not_awaited()
        args, kwargs = ctx.bot.send_message.await_args
        self.assertIn("اكتب اسم", args[1])
        self.assertEqual(kwargs["reply_markup"], "buttons")

    async def test_failed_upload_stays_in_queue(self):
        self.ns["upload_item"] = AsyncMock(return_value=False)
        item = self._item(folder="folder-1")
        ud = {"queue": [item]}
        self.assertFalse(await self.ns["_finish_queued_file"](None, 77, ud, None))
        self.assertEqual(ud["queue"], [item])

    async def test_successful_upload_removes_one_queued_item(self):
        self.ns["upload_item"] = AsyncMock(return_value=True)
        a, b = self._item(folder="folder-1"), self._item()
        ud = {"queue": [a, b]}
        self.assertTrue(await self.ns["_finish_queued_file"](None, 77, ud, "Lecture"))
        self.assertEqual(ud["queue"], [b])

    async def test_expired_telegram_button_is_ignored(self):
        cq = SimpleNamespace(
            answer=AsyncMock(side_effect=BadRequest("Query is too old and response timeout expired"))
        )
        self.assertFalse(await self.ns["_safe_callback_answer"](cq))

    async def test_folder_selection_checks_current_queue(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(select=object())
        self.ns["_drive_call"] = AsyncMock(return_value={"id": "folder-1", "name": "محاضرات"})
        self.ns["ask_next"] = AsyncMock()
        cq = SimpleNamespace(
            data="upset:42:folder-1",
            message=SimpleNamespace(chat_id=77, reply_text=AsyncMock()),
            edit_message_text=AsyncMock(),
        )
        item = self._item()
        ctx = SimpleNamespace(user_data={"queue": [item]})
        update = SimpleNamespace(callback_query=cq, effective_user=SimpleNamespace(id=123))
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertEqual(item["target_folder"], "folder-1")
        self.assertEqual(item["target_name"], "محاضرات")
        self.ns["ask_next"].assert_awaited_once()
        self.ns["_drive_call"].assert_awaited_once()
        self.assertEqual(self.ns["_drive_call"].await_args.args[0],
                         self.ns["USER_DRIVE"].select)

    async def test_stale_folder_button_does_not_change_queue(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["_drive_call"] = AsyncMock()
        cq = SimpleNamespace(
            data="upset:wrong-token:folder-1",
            message=SimpleNamespace(chat_id=77, reply_text=AsyncMock()),
        )
        item = self._item()
        ctx = SimpleNamespace(user_data={"queue": [item]})
        update = SimpleNamespace(callback_query=cq, effective_user=SimpleNamespace(id=123))
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertIsNone(item["target_folder"])
        self.ns["_drive_call"].assert_not_awaited()



    def _callback(self, action, item=None, items=None):
        item = item or self._item()
        cq = SimpleNamespace(
            data=action,
            message=SimpleNamespace(chat_id=77, reply_text=AsyncMock()),
            edit_message_text=AsyncMock(),
        )
        queue = items if items is not None else [item]
        ud = {"queue": queue}
        ctx = SimpleNamespace(user_data=ud)
        update = SimpleNamespace(callback_query=cq, effective_user=SimpleNamespace(id=123))
        return update, ctx, cq, ud

    async def test_fast_save_chooses_folder_and_skips_naming(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(select=object())
        self.ns["_drive_call"] = AsyncMock(return_value={"id":"active","name":"فويسات"})
        self.ns["_finish_queued_file"] = AsyncMock(return_value=True)
        self.ns["ask_next"] = AsyncMock()
        update, ctx, _, _ = self._callback("upfast:42:active")
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertEqual(ctx.user_data["queue"][0]["target_folder"], "active")
        self.ns["_finish_queued_file"].assert_awaited_once_with(ctx, 77, ctx.user_data, None)
        self.ns["ask_next"].assert_awaited_once()

    async def test_batch_upload_uses_one_selected_folder_for_all_files(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(select=object())
        self.ns["_drive_call"] = AsyncMock(return_value={"id":"active","name":"فويسات"})
        self.ns["ask_next"] = AsyncMock()
        a = self._item()
        b = dict(self._item(), token="43", target_folder=None)
        c = dict(self._item(), token="44", target_folder=None)
        update, ctx, cq, ud = self._callback("upbatch:42:active", items=[a,b,c])
        async def successful_upload(*args):
            self.assertEqual(ud["queue"][0]["target_folder"], "active")
            ud["queue"].pop(0)
            return True
        self.ns["_finish_queued_file"] = AsyncMock(side_effect=successful_upload)
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertEqual(ud["queue"], [])
        self.assertEqual(self.ns["_finish_queued_file"].await_count, 3)
        self.ns["_drive_call"].assert_awaited_once()
        self.assertIn("3", cq.message.reply_text.await_args.args[0])

    async def test_cancel_button_never_cancels_wrong_file(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["ask_next"] = AsyncMock()
        update, ctx, cq, ud = self._callback("upcancel:wrong")
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertEqual(len(ud["queue"]), 1)
        self.ns["ask_next"].assert_not_awaited()
        cq.message.reply_text.assert_awaited_once()
        cq.data = "upcancel:42"
        await self.ns["on_upload_folder_button"](update, ctx)
        self.assertEqual(ud["queue"], [])
        self.ns["ask_next"].assert_awaited_once()

    async def test_one_tap_original_name_from_naming_prompt_uses_token(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["_finish_queued_file"] = AsyncMock(return_value=True)
        self.ns["ask_next"] = AsyncMock()
        item = self._item(folder="active")
        item["retry_name"] = "Old custom name"
        update, ctx, _, _ = self._callback("upnamefast:42", item=item)
        await self.ns["on_upload_folder_button"](update, ctx)
        self.ns["_finish_queued_file"].assert_awaited_once_with(
            ctx, 77, ctx.user_data, None)

    async def test_retry_keeps_chosen_name_and_does_not_reask_folder(self):
        self.ns["_safe_callback_answer"] = AsyncMock(return_value=True)
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["_finish_queued_file"] = AsyncMock(return_value=True)
        self.ns["ask_next"] = AsyncMock()
        item = self._item(folder="active")
        item["retry_name"] = "Lecture 01"
        update, ctx, _, _ = self._callback("upretry:42", item=item)
        await self.ns["on_upload_folder_button"](update, ctx)
        self.ns["_finish_queued_file"].assert_awaited_once_with(
            ctx, 77, ctx.user_data, "Lecture 01")

    async def test_cancel_command_removes_only_one_pending_file(self):
        self.ns["_menu_keyboard"] = lambda: "menu"
        self.ns["ask_next"] = AsyncMock()
        a,b = self._item(),dict(self._item(),token="43",target_folder=None)
        ctx = SimpleNamespace(user_data={"queue":[a,b]})
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=msg,
                                 effective_chat=SimpleNamespace(id=77))
        await self.ns["cancel_command"](update,ctx)
        self.assertEqual(ctx.user_data["queue"],[b])
        self.ns["ask_next"].assert_awaited_once()

    async def test_failed_queued_upload_remembers_custom_name_for_retry(self):
        self.ns["upload_item"] = AsyncMock(return_value=False)
        item = self._item(folder="active")
        ud = {"queue":[item]}
        self.assertFalse(await self.ns["_finish_queued_file"](None,77,ud,"Lecture 1"))
        self.assertEqual(item["retry_name"],"Lecture 1")
        self.assertNotIn("_uploading",item)

if __name__ == "__main__":
    unittest.main()
