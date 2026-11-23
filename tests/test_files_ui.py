"""Tests for permanent file-link retrieval and folder-picker UX; no credentials."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "bot.py": {
        "drive_file_url", "show_item", "_show_file_results", "_menu_keyboard",
        "_upload_folder_snapshot", "_browser_snapshot", "show_browser", "_show_upload_folders",
    },
}


def load_functions(file, names, namespace):
    source = ast.parse((ROOT / file).read_text(encoding="utf-8"))
    selected = [
        node for node in source.body if
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {n.name for n in selected} == names
    exec(compile(ast.Module(body=selected, type_ignores=[]), file, "exec"), namespace)


class Button:
    def __init__(self, text, **kwargs):
        self.text = text
        self.url = kwargs.get("url")
        self.callback_data = kwargs.get("callback_data")


class Markup:
    def __init__(self, rows, **kwargs):
        self.rows = rows


class FileExperienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = {
            "FOLDER_MIME": "application/vnd.google-apps.folder",
            "InlineKeyboardButton": Button,
            "InlineKeyboardMarkup": Markup,
            "ReplyKeyboardMarkup": Markup,
            "FILES_BTN": "📁 ملفاتي",
            "RECENT_BTN": "🕘 آخر ملفاتي",
            "SEARCH_BTN": "🔎 بحث عن ملف",
            "NEW_FOLDER_BTN": "➕ مجلد جديد",
            "UPLOAD_BTN": "📤 رفع ملف",
            "STATUS_BTN": "📊 الحالة",
            "HELP_BTN": "❓ طريقة الاستخدام",
        }
        load_functions("bot.py", MODULES["bot.py"], self.ns)

    async def test_telegram_keyboard_has_search_and_recent_without_admin(self):
        markup = self.ns["_menu_keyboard"](12345)
        words = " ".join(str(item) for row in markup.rows for item in row)
        self.assertIn("بحث عن ملف", words)
        self.assertIn("آخر ملفاتي", words)
        self.assertNotIn("أدمن", words)

    async def test_file_details_have_permanent_link_and_open_button(self):
        file_id = "file_id_ABC123"
        meta = {
            "id": file_id, "name": "Lecture 1.pdf", "size": "1048576",
            "mimeType": "application/pdf", "parents": ["parent_folder"],
        }
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(require_item=object())
        self.ns["_drive_call"] = AsyncMock(return_value=meta)
        self.ns["BadRequest"] = Exception
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            callback_query=None, effective_user=SimpleNamespace(id=123, full_name="Student"),
            effective_message=msg,
        )
        await self.ns["show_item"](update, None, file_id)
        args, kwargs = msg.reply_text.await_args
        url = "https://drive.google.com/file/d/file_id_ABC123/view"
        self.assertIn(url, args[0])
        self.assertEqual(kwargs["reply_markup"].rows[0][0].url, url)

    async def test_search_results_have_one_tap_drive_link(self):
        data = [
            {"id": "f1", "name": "محاضرة 4.pdf"},
            {"id": "f2", "name": "محاضرة 5.pdf"},
        ]
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["USER_DRIVE"] = SimpleNamespace(search_files=object())
        self.ns["_drive_call"] = AsyncMock(return_value=data)
        self.ns["_item_button"] = lambda name, action, id: Button(name, callback_data=f"{action}:{id}")
        msg = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123, full_name="Student"), effective_message=msg)
        await self.ns["_show_file_results"](update, None, "محاضرة")
        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.rows[0][1].url, "https://drive.google.com/file/d/f1/view")
        self.assertEqual(markup.rows[1][1].url, "https://drive.google.com/file/d/f2/view")

    async def test_folder_picker_creates_defaults_and_shows_folders_only(self):
        folder = {"id": "root", "name": "root"}
        items = [
            {"id": "a", "name": "ملفات عامة", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "b", "name": "محاضرات", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "c", "name": "فويسات", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "d", "name": "file.pdf", "mimeType": "application/pdf"},
        ]
        user_drive = SimpleNamespace(
            ensure_defaults=MagicMock(),
            folder_path=MagicMock(return_value=(folder, folder, ["ملفاتي"])),
            list_children=MagicMock(return_value=(items, False)),
        )
        self.ns["USER_DRIVE"] = user_drive
        root, selected, folders, files, more, breadcrumb = (
            self.ns["_upload_folder_snapshot"](123, "Student", "root")
        )
        user_drive.ensure_defaults.assert_called_once_with(123, "Student")
        self.assertEqual([x["name"] for x in folders], ["فويسات", "محاضرات", "ملفات عامة"])
        self.assertEqual([x["name"] for x in files], ["file.pdf"])
        self.assertEqual(breadcrumb, ["ملفاتي"])
        self.assertFalse(more)

    async def test_open_folder_replaces_message_with_contents_and_breadcrumb(self):
        root = {"id": "root", "name": "ملفاتي"}
        folder = {"id": "sub", "name": "المحاضرة الأولى", "parents": ["root"]}
        contents = [
            {"id": "nested", "name": "التمارين", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "pdf_1", "name": "الملزمة.pdf", "mimeType": "application/pdf"},
        ]
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["_drive_call"] = AsyncMock(return_value=(
            root, folder, {"id": "sub", "name": folder["name"]}, contents, False,
            ["ملفاتي", "محاضرات", "المحاضرة الأولى"],
        ))
        self.ns["_item_button"] = lambda text, action, ident: Button(
            text, callback_data=f"{action}:{ident}"
        )
        self.ns["BadRequest"] = Exception
        query = SimpleNamespace(edit_message_text=AsyncMock())
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=123, full_name="Student"),
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
        )
        await self.ns["show_browser"](update, None, "sub")
        args, kwargs = query.edit_message_text.await_args
        self.assertIn("ملفاتي / محاضرات / المحاضرة الأولى", args[0])
        self.assertIn("1 مجلدات", args[0])
        self.assertIn("1 ملفات", args[0])
        rows = kwargs["reply_markup"].rows
        self.assertEqual(rows[0][0].callback_data, "nav:root")
        self.assertEqual(rows[0][1].callback_data, "home")
        self.assertTrue(any(x.callback_data == "nav:nested" for row in rows for x in row))
        self.assertTrue(any(x.url == "https://drive.google.com/file/d/pdf_1/view"
                            for row in rows for x in row))
        update.effective_message.reply_text.assert_not_awaited()

    async def test_upload_picker_shows_only_folders_and_breadcrumb(self):
        root = {"id": "root", "name": "ملفاتي"}
        folder = {"id": "sub", "name": "محاضرات", "parents": ["root"]}
        folder_item = {"id": "subsub", "name": "نظم منطقية"}
        file_item = {"id": "existing_pdf", "name": "المحاضرة 5.pdf"}
        self.ns["_drive_call"] = AsyncMock(return_value=(
            root, folder, [folder_item], [file_item], False,
            ["ملفاتي", "محاضرات"],
        ))
        self.ns["BadRequest"] = Exception
        cq = SimpleNamespace(edit_message_text=AsyncMock())
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        item = {"token": "42", "label": "فويس.ogg", "uid": 123, "owner_name": "Student"}
        await self.ns["_show_upload_folders"](ctx, 77, item, folder_id="sub", cq=cq)
        text, kwargs = cq.edit_message_text.await_args
        self.assertIn("ملفاتي / محاضرات", text[0])
        self.assertIn("مجلدات: 1", text[0])
        self.assertIn("ملفات: 1", text[0])
        rows = kwargs["reply_markup"].rows
        self.assertTrue(any(x.callback_data == "upnav:42:subsub" for row in rows for x in row))
        self.assertFalse(any(x.callback_data == "upset:42:subsub" for row in rows for x in row))
        self.assertFalse(any(x.url for row in rows for x in row))
        self.assertTrue(any(x.callback_data == "upset:42:sub" for row in rows for x in row))
        ctx.bot.send_message.assert_not_awaited()


    async def test_upload_picker_starts_at_remembered_folder(self):
        root = {"id": "root", "name": "ملفاتي"}
        selected = {"id": "active", "name": "فويسات", "parents": ["root"]}
        self.ns["USER_DRIVE"] = SimpleNamespace(current=object())
        self.ns["_drive_call"] = AsyncMock(side_effect=[
            selected,
            (root, selected, [], [], False, ["ملفاتي", "فويسات"]),
        ])
        self.ns["BadRequest"] = Exception
        ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        item = {"token": "42", "label": "فويس.ogg", "uid": 123, "owner_name": "Student"}
        await self.ns["_show_upload_folders"](ctx, 77, item)
        self.assertEqual(item["recent_target"]["id"], "active")
        self.assertEqual(self.ns["_drive_call"].await_args_list[0].args[0],
                         self.ns["USER_DRIVE"].current)
        markup = ctx.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertTrue(any(x.callback_data == "upset:42:active"
                            for row in markup.rows for x in row))

    async def test_browser_can_jump_to_selected_folder_and_rename_current(self):
        root = {"id": "root", "name": "ملفاتي"}
        folder = {"id": "other", "name": "محاضرات", "parents": ["root"]}
        active = {"id": "active", "name": "فويسات"}
        self.ns["_private_ui"] = AsyncMock(return_value=True)
        self.ns["_drive_call"] = AsyncMock(return_value=(
            root, folder, active, [], False, ["ملفاتي", "محاضرات"],
        ))
        self.ns["BadRequest"] = Exception
        query = SimpleNamespace(edit_message_text=AsyncMock())
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=123, full_name="Student"),
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
        )
        await self.ns["show_browser"](update, None, "other")
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        actions = [x.callback_data for row in markup.rows for x in row]
        self.assertIn("nav:active", actions)
        self.assertIn("ren:other", actions)
        self.assertIn("trash:other", actions)

if __name__ == "__main__":
    unittest.main()
