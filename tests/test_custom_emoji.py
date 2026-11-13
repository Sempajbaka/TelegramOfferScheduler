"""Synthetic MTProto/Qt tests; never connect to Telegram."""
import json
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from telethon import types, functions, errors
from formatting import imported_offer, parse_offer, encode
from engine import Engine, error_description
from application_settings import remember_versions
from profiles import load_settings
from test_v2 import Server


def sample(multiple=True):
    return NS(id=7, date=datetime.now(timezone.utc), media=None, message='😀 Текст 🔥', entities=[
        types.MessageEntityCustomEmoji(0, 2, 1234567890123456789),
        types.MessageEntityBold(3, 5), types.MessageEntityItalic(3, 5),
        types.MessageEntityStrike(3, 5), types.MessageEntityTextUrl(3, 5, 'https://example.org'),
        *([types.MessageEntityCustomEmoji(9, 2, 2234567890123456789)] if multiple else [])])


class EmojiTests(unittest.TestCase):
    def test_installed_telethon_serializes_custom_entities_and_repeat(self):
        from telethon.extensions import BinaryReader
        source = sample()
        request = functions.messages.SendMessageRequest(peer=types.InputPeerSelf(), message=source.message,
            random_id=123, entities=source.entities, schedule_date=source.date, schedule_repeat_period=86400)
        with BinaryReader(bytes(request)) as reader:
            restored = reader.tgread_object()
        self.assertEqual(encode(restored.entities), encode(source.entities))
        self.assertEqual(restored.schedule_repeat_period, 86400)

    def test_original_entities_round_trip_and_history(self):
        for multiple in (False, True):
            source = sample(multiple)
            offer = imported_offer(source)
            text, entities = parse_offer(json.loads(json.dumps(offer)))
            self.assertEqual(text, source.message)
            self.assertEqual(encode(entities), encode(source.entities))
            config = {'templates': {'A': text}, 'template_formats': {'A': 'telegram'},
                      'template_entities': {'A': offer['entities']}}
            remember_versions(config)
            self.assertEqual(config['template_versions']['A'][0]['entities'], offer['entities'])

    def test_utf16_invalid_half_surrogate_rejected(self):
        offer = imported_offer(sample())
        offer['entities'] = encode([types.MessageEntityCustomEmoji(1, 1, 42)])
        with self.assertRaises(ValueError):
            parse_offer(offer)

    def test_premium_and_entity_errors_explained(self):
        for cls in (errors.PremiumAccountRequiredError, errors.EmojiInvalidError, errors.EntityBoundsInvalidError):
            self.assertNotIn('Проверьте соединение', error_description(cls(request=None)))
        self.assertIn('кастомный', error_description(errors.BadRequestError(request=None, message='CUSTOM_EMOJI_INVALID')))


class EmojiAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_stripping_entities_blocks_remaining_slots(self):
        class StrippingServer(Server):
            async def __call__(self, request):
                result = await super().__call__(request)
                if isinstance(request, functions.messages.SendMessageRequest):
                    self.queues[-1][-1].entities = []
                return result
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.client = StrippingServer()
            engine.sleep = AsyncMock()
            engine.entities = {-1: types.InputPeerChat(1)}
            chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
            with closing(engine.store.db):
                await engine.prepare(chats, {'A': imported_offer(sample())}, int(time.time())+600, 86400, 2, 1)
                with self.assertRaisesRegex(ValueError, 'эмодзи'):
                    await engine.execute()
                self.assertEqual(len(engine.client.queues[-1]), 1)
                self.assertEqual(engine.store.jobs(-1)[0]['state'], 'unverified')

    async def test_saved_messages_read_only_and_media_excluded(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.account = AsyncMock()
            media = sample()
            media.media = types.MessageMediaPhoto()
            engine.rpc = AsyncMock(return_value=NS(messages=[sample(), media, NS(message=None)]))
            with closing(engine.store.db):
                result = await engine.saved_messages()
                self.assertEqual(len(result['saved_messages']), 1)
                self.assertEqual(result['saved_messages'][0]['entities'], encode(sample().entities))
                request = engine.rpc.call_args.args[0]
                self.assertIsInstance(request, functions.messages.GetHistoryRequest)
                self.assertIsInstance(request.peer, types.InputPeerSelf)
                self.assertEqual(request.limit, 100)

    async def test_create_repeat_rollover_edit_two_accounts(self):
        offer = imported_offer(sample())
        for account in (100, 200):
            with TemporaryDirectory() as folder:
                engine = Engine(Path(folder), lambda _: None, threading.Event())
                engine.client = Server()
                engine.client.user_id = account
                engine.sleep = AsyncMock()
                engine.entities = {-1: types.InputPeerChat(1)}
                chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
                with closing(engine.store.db):
                    await engine.prepare(chats, {'A': offer}, int(time.time())+600, 86400, 2, 1)
                    await engine.execute()
                    self.assertEqual(len(engine.client.queues[-1]), 2)
                    for msg in engine.client.queues[-1]:
                        self.assertEqual(encode(msg.entities), offer['entities'])
                        self.assertEqual(msg.schedule_repeat_period, 86400)
                        msg.date += timedelta(days=1)
                        msg.id += 50
                    changed = sample()
                    changed.entities[0].document_id += 1
                    update = imported_offer(changed)
                    result = await engine.prepare_updates(chats, {'A': update})
                    self.assertEqual(len(result['changes']), 2)
                    await engine.apply_prepared_changes()
                    for msg in engine.client.queues[-1]:
                        self.assertEqual(encode(msg.entities), update['entities'])
                        self.assertEqual(msg.schedule_repeat_period, 86400)

    async def test_premium_rejection_stops_without_plain_fallback(self):
        with TemporaryDirectory() as folder:
            engine = Engine(Path(folder), lambda _: None, threading.Event())
            engine.client = Server()
            engine.client.wait_once = errors.PremiumAccountRequiredError(request=None)
            engine.sleep = AsyncMock()
            engine.entities = {-1: types.InputPeerChat(1)}
            chats = [{'id': -1, 'title': 'Test', 'allowed': True, 'manual': 15, 'rules': 'yes', 'template_id': 'A'}]
            with closing(engine.store.db):
                offer = imported_offer(sample())
                await engine.prepare(chats, {'A': offer}, int(time.time())+600, 86400, 2, 1)
                with self.assertRaises(errors.PremiumAccountRequiredError):
                    await engine.execute()
                sends = [r for r in engine.client.requests if isinstance(r, functions.messages.SendMessageRequest)]
                self.assertEqual(len(sends), 1)
                self.assertEqual(encode(sends[0].entities), offer['entities'])


class EmojiGuiTests(unittest.TestCase):
    def test_import_copy_restart_and_readonly(self):
        from PySide6.QtWidgets import QApplication
        from app import Window
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            window = Window(Path(folder))
            try:
                offer = imported_offer(sample())
                offer['text'] += '\r\nВторая строка\u2028Третья строка'
                window.install_template('Original', offer)
                window.save_template()
                self.assertEqual(window.offers()['Original']['text'], offer['text'])
                self.assertTrue(window.text.isReadOnly())
                self.assertIn('2', window.template_info.text())
                second = window.profiles.add('Second')
                label = f'Second ({second})'
                with patch('app.QInputDialog.getItem', return_value=(label, True)):
                    window.copy_template()
                config = load_settings(window.profiles.path(second) / 'settings.json')
                self.assertEqual(config['template_entities']['Original'], offer['entities'])
                saved = load_settings(window.config_path)
                self.assertEqual(saved['template_entities']['Original'], offer['entities'])
            finally:
                window.close()
                app.processEvents()
            window = Window(Path(folder))
            try:
                window.template.setCurrentText('Original')
                self.assertEqual(window.offers()['Original'], offer)
                self.assertTrue(window.text.isReadOnly())
            finally:
                window.close()
                app.processEvents()
