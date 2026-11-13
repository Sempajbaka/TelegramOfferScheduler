import asyncio
import inspect
import json
import time
from datetime import datetime, timezone

import telethon
from telethon import TelegramClient, functions, types, utils, errors, events
from telethon.sessions import StringSession
from planner import DAY, interval_seconds, make_slots, distance, validate_text
from storage import Store
from profiles import Vault, Profiles
from synchronization import reconcile, compatible
from application_settings import ApplicationSettings
from formatting import parse_offer, encode, decode, same_entities, imported_offer
from diagnostics import diagnose
from campaigns import within_window, template_names
from storage import save_json


class Stopped(Exception):
    pass


def error_description(exc):
    name = type(exc).__name__
    if getattr(exc, 'message', '') == 'CUSTOM_EMOJI_INVALID':
        name = 'CustomEmojiInvalidError'
    messages = {
        'TimeoutError': 'Telegram не ответил за отведённое время. Операция остановлена, прогресс сохранён. Результат начатой отправки может быть неизвестен: перед продолжением обновите очередь.',
        'ScheduleTooMuchError': 'Достигнут лимит серверной очереди. Подтверждённые сообщения сохранены.',
        'ChatWriteForbiddenError': 'Нет права публиковать в этой группе.',
        'ChatAdminRequiredError': 'Для операции не хватает прав в группе.',
        'ChannelPrivateError': 'Группа недоступна выбранному аккаунту.',
        'UserBannedInChannelError': 'Публикация в группе запрещена этому аккаунту.',
        'AuthKeyUnregisteredError': 'Сессия недействительна. Войдите в аккаунт заново.',
        'SessionRevokedError': 'Сессия отозвана. Войдите в аккаунт заново.',
        'SessionExpiredError': 'Сессия истекла. Войдите в аккаунт заново.',
        'PhoneCodeInvalidError': 'Неверный код Telegram. Повторите ввод кода.',
        'PhoneCodeExpiredError': 'Код истёк. Повторите вход с номером телефона.',
        'PhoneNumberInvalidError': 'Номер телефона неверен. Используйте международный формат, например +79991234567.',
        'PhoneNumberBannedError': 'Telegram заблокировал вход для этого номера. Обратитесь в поддержку Telegram.',
        'PhoneNumberFloodError': 'Telegram временно ограничил запросы кода для этого номера. Не повторяйте попытки и попробуйте позже или войдите по QR-коду.',
        'PasswordHashInvalidError': 'Неверный пароль 2FA. Повторите ввод.',
        'ApiIdInvalidError': 'Telegram отклонил общие API ID/API Hash. Проверьте вкладку настроек.',
        'PremiumAccountRequiredError': 'Для этой операции нужен Telegram Premium.',
        'CustomEmojiInvalidError': 'Telegram отклонил кастомный эмодзи. Проверьте доступность эмодзи для этого аккаунта и импортируйте сообщение заново.',
        'EmojiInvalidError': 'Telegram отклонил эмодзи. Проверьте его доступность для выбранного аккаунта и импортируйте сообщение заново.',
        'EntitiesTooLongError': 'Telegram отклонил количество или размер элементов форматирования.',
        'EntityBoundsInvalidError': 'Telegram отклонил границы форматирования. Импортируйте исходное сообщение заново.',
    }
    return messages.get(name, f'{name}: операция остановлена. Проверьте соединение, права и очередь Telegram. Прогресс сохранён.')


def capability():
    classes = (functions.messages.SendMessageRequest,
               functions.messages.EditMessageRequest, types.Message)
    supported = all('schedule_repeat_period' in inspect.signature(c).parameters for c in classes)
    return {'version': telethon.__version__, 'repeat': supported}


def snapshot(messages):
    return sorted((m.id, int(m.date.timestamp()), m.message or '',
                   getattr(m, 'schedule_repeat_period', None) or 0) for m in messages)


class Engine:
    def __init__(self, directory, log, cancel, registry_root=None):
        self.directory, self.log, self.cancel = directory, log, cancel
        self.store = Store(directory / 'progress.sqlite3')
        self.client = None
        self.pause = 2.0
        self.next_request = 0
        self.plan = []
        self.entities = {}
        self.dialog_entities = {}
        self.queue_cache = []
        self.premium = False
        self.identity_verified = False
        self.next_allowed = {}
        self.create_pause = 2.0
        self.test_mode = False
        self.change_plan = []
        self.vault = Vault(directory)
        self.registry_root = registry_root
        self.margin = 60
        self.request_timeout = 40
        self.network_stalled = False
        self.progress = lambda value: None
        self.completed = 0
        self.total = 0
        self.progress_unit = 'подтверждено'
        self.qr_login_request = None

    def report_progress(self, **fields):
        self.progress({'completed': self.completed, 'total': self.total, 'unit': self.progress_unit, **fields})

    async def network(self, factory, label='Запрос Telegram', cancellable=True, timeout=None):
        if self.network_stalled and cancellable:
            raise Stopped('Предыдущий запрос не завершил отмену. Перезапустите приложение перед продолжением.')
        if cancellable:
            self.check_cancel()
        self.log(f'{label}: ожидание ответа (не более {timeout or self.request_timeout:g} с)')
        self.report_progress(phase='Ответ Telegram', wait_until=0)
        task = asyncio.ensure_future(factory())
        deadline = time.monotonic() + (timeout or self.request_timeout)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                done, _ = await asyncio.wait({task}, timeout=min(.2, remaining))
                if task in done:
                    return task.result()
                if cancellable:
                    self.check_cancel()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=.5)
                if not task.done():
                    self.network_stalled = True
            # Consume late failures without waiting indefinitely for cancellation.
            def consume(future):
                if not future.cancelled():
                    future.exception()
            task.add_done_callback(consume)

    def configure(self, pause=2, create_pause=2, test_mode=False, margin=60):
        self.pause = max(1, min(60, float(pause)))
        self.create_pause = max(1, min(60, float(create_pause)))
        self.test_mode = bool(test_mode)
        self.margin = max(60, int(margin))

    def mutation_limit(self, chats, count):
        if self.test_mode and (len(set(chats)) != 1 or count > 1):
            raise ValueError('Тестовый режим разрешает одно изменение в одной группе за подтверждение')

    def save_session(self):
        if self.client and hasattr(self.client, 'session'):
            self.vault.write('session', StringSession.save(self.client.session))

    async def sleep(self, seconds):
        end = time.monotonic() + max(0, seconds)
        limited = float(self.store.meta('wait_until') or 0) > time.time()
        self.report_progress(phase='Ограничение Telegram' if limited else 'Пауза между запросами',
                             wait_until=time.time() + max(0, seconds))
        next_notice = time.monotonic() if seconds >= 5 else end
        while time.monotonic() < end:
            self.check_cancel()
            if time.monotonic() >= next_notice:
                self.log(f'Пауза по ограничению Telegram: осталось {max(1, int(end - time.monotonic()))} с. Ожидание можно отменить.')
                next_notice = time.monotonic() + 30
            await asyncio.sleep(min(.25, max(0, end - time.monotonic())))

    def check_cancel(self):
        if self.cancel.is_set():
            raise Stopped('Остановлено. Подтверждённые сообщения остаются в Telegram.')

    async def rpc(self, request, guard=None, on_attempt=None, on_wait=None):
        waits = 0
        while True:
            self.check_cancel()
            until = float(self.store.meta('wait_until') or 0)
            await self.sleep(max(until - time.time(), self.next_request - time.monotonic(), 0))
            if guard:
                guard()
            if on_attempt:
                on_attempt()
            try:
                result = await self.network(lambda: self.client(request), type(request).__name__)
                self.next_request = time.monotonic() + self.pause
                return result
            except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
                if on_wait:
                    on_wait()
                seconds = exc.seconds + 1
                self.store.meta('wait_until', time.time() + seconds)
                self.log(f'{type(exc).__name__}: ожидание {seconds} с, сохранено на диск')
                waits += 1
                if waits >= 3:
                    raise Stopped('Telegram повторно ограничивает запрос. Операция остановлена; время ожидания сохранено. Продолжите позже.')
            finally:
                self.next_request = time.monotonic() + self.pause

    async def connect(self, api_id, api_hash, phone, pause, restore_only=False):
        self.pause = max(1, min(60, float(pause)))
        self.identity_verified = False
        if self.client:
            await self.network(lambda: self.client.disconnect(), 'Отключение', cancellable=False, timeout=5)
        if self.registry_root:
            settings = ApplicationSettings(self.registry_root)
            settings.migrate()
            if api_id and api_hash:
                settings.save_credentials(api_id, api_hash)
            credentials = settings.credentials()
            api_id, api_hash = credentials.get('api_id'), credentials.get('api_hash', '')
        else:
            api_hash = api_hash.strip() or self.vault.read('api_hash')
        if len(api_hash) != 32:
            raise ValueError('Введите API ID и API Hash один раз в общих настройках')
        encrypted_session = self.vault.read('session')
        legacy = self.directory / 'account.session'
        session = StringSession(encrypted_session) if encrypted_session or not legacy.exists() else str(self.directory / 'account')
        self.client = TelegramClient(session, int(api_id), api_hash,
                                     flood_sleep_threshold=0, request_retries=0,
                                     connection_retries=1, raise_last_call_error=True)
        if hasattr(self.client, 'add_event_handler'):
            self.client.add_event_handler(self.schedule_update, events.Raw())
        if not self.registry_root:
            self.vault.write('api_hash', api_hash)
        await self.network(lambda: self.client.connect(), 'Подключение')
        if await self.network(lambda: self.client.is_user_authorized(), 'Проверка сессии'):
            return await self.account()
        if restore_only:
            return {'session_expired': True}
        await self.sleep(max(0, float(self.store.meta('wait_until') or 0) - time.time()))
        self.phone = phone
        try:
            sent = await self.network(lambda: self.client.send_code_request(phone), 'Запрос кода')
        except errors.FloodWaitError as exc:
            self.store.meta('wait_until', time.time() + exc.seconds + 1)
            raise
        self.phone_hash = sent.phone_code_hash
        return {'auth': 'code'}

    async def connect_qr(self, api_id, api_hash, pause):
        """Connect an existing session or start Telegram's code-free QR login."""
        result = await self.connect(api_id, api_hash, '', pause, restore_only=True)
        if not result.get('session_expired'):
            return result
        self.qr_login_request = await self.network(
            lambda: self.client.qr_login(), 'Создание QR-кода для входа')
        expires = getattr(self.qr_login_request, 'expires', None)
        return {
            'auth': 'qr',
            'qr_url': self.qr_login_request.url,
            'qr_expires': int(expires.timestamp()) if expires else 0,
        }

    async def wait_qr_login(self):
        if not self.qr_login_request:
            raise ValueError('QR-код не создан. Нажмите «Войти по QR-коду» ещё раз.')
        request = self.qr_login_request
        expires = getattr(request, 'expires', None)
        timeout = max(5, min(120, (expires.timestamp() - time.time()) + 2)) if expires else 60
        try:
            await self.network(lambda: request.wait(timeout=timeout),
                               'Ожидание сканирования QR-кода', timeout=timeout + 2)
        except errors.SessionPasswordNeededError:
            self.qr_login_request = None
            return {'auth': 'password'}
        except TimeoutError:
            self.qr_login_request = None
            raise ValueError('Срок действия QR-кода истёк. Создайте новый QR-код и отсканируйте его сразу.')
        self.qr_login_request = None
        return await self.account()

    async def login(self, code=None, password=None):
        await self.sleep(max(0, float(self.store.meta('wait_until') or 0) - time.time()))
        try:
            if password is not None:
                await self.network(lambda: self.client.sign_in(password=password), 'Проверка 2FA')
            else:
                await self.network(lambda: self.client.sign_in(self.phone, code, phone_code_hash=self.phone_hash), 'Проверка кода')
        except errors.SessionPasswordNeededError:
            return {'auth': 'password'}
        except errors.FloodWaitError as exc:
            self.store.meta('wait_until', time.time() + exc.seconds + 1)
            raise
        return await self.account()

    async def account(self):
        me = (await self.rpc(functions.users.GetUsersRequest([types.InputUserSelf()])))[0]
        previous = self.store.meta('account')
        if previous and int(previous) != me.id:
            raise ValueError('Локальный прогресс принадлежит другому аккаунту')
        if me.bot:
            raise ValueError('Нужен пользовательский аккаунт, не бот')
        if self.registry_root:
            Profiles(self.registry_root).claim(self.directory, me.id)
        self.identity_verified = True
        self.store.meta('account', me.id)
        self.premium = bool(me.premium)
        self.save_session()
        return {'auth': 'ready', 'premium': self.premium, 'account_id': me.id, **capability()}

    async def saved_messages(self):
        await self.account()
        page = await self.rpc(functions.messages.GetHistoryRequest(
            peer=types.InputPeerSelf(), offset_id=0, offset_date=None, add_offset=0,
            limit=100, max_id=0, min_id=0, hash=0))
        messages = []
        for message in page.messages:
            # Web previews are generated from the text; other media cannot be reproduced.
            if not getattr(message, 'message', None):
                continue
            if getattr(message, 'media', None) and not isinstance(message.media, types.MessageMediaWebPage):
                continue
            offer = imported_offer(message)
            messages.append({'id': message.id, 'date': int(message.date.timestamp()), **offer})
        return {'saved_messages': messages}

    async def dialogs(self):
        await self.account()
        # Explicit paginated requests keep ALL network reads under the same rate limiter.
        result, offset_date, offset_id, offset_peer = [], None, 0, types.InputPeerEmpty()
        seen_pages = set()
        while True:
            page = await self.rpc(functions.messages.GetDialogsRequest(
                offset_date=offset_date, offset_id=offset_id, offset_peer=offset_peer,
                limit=100, hash=0, exclude_pinned=offset_id != 0))
            entities = {utils.get_peer_id(e): e for e in [*page.users, *page.chats]}
            page_key = tuple((utils.get_peer_id(d.peer), d.top_message) for d in page.dialogs)
            if page_key and page_key in seen_pages:
                raise ValueError('Telegram повторяет страницу списка чатов. Загрузка остановлена; повторите позже.')
            seen_pages.add(page_key)
            for dialog in page.dialogs:
                ident = utils.get_peer_id(dialog.peer)
                entity = entities.get(ident)
                if isinstance(entity, (types.Chat, types.Channel)) and not getattr(entity, 'left', False):
                    if getattr(entity, 'broadcast', False) or getattr(entity, 'forum', False):
                        continue
                    self.entities[ident] = utils.get_input_peer(entity)
                    count = await self.participant_count(entity)
                    result.append({'id': ident, 'title': entity.title, 'participants_count': count})
                    self.log(f'Загрузка групп: обработано {len(result)}')
            if len(page.dialogs) < 100:
                break
            last = page.dialogs[-1]
            msg = next((m for m in page.messages if m.id == last.top_message and
                        utils.get_peer_id(m.peer_id) == utils.get_peer_id(last.peer)), None)
            if msg is None:
                break
            offset_date, offset_id = msg.date, msg.id
            offset_peer = utils.get_input_peer(entities[utils.get_peer_id(last.peer)])
        return {'dialogs': list({r['id']: r for r in result}.values())}

    async def account_dialogs(self):
        """Read every removable dialog without changing Telegram state."""
        await self.account()
        filters = []
        try:
            response = await self.rpc(functions.messages.GetDialogFiltersRequest())
            filters = list(getattr(response, 'filters', response) or [])
        except (AttributeError, errors.RPCError) as exc:
            self.log(f'Папки Telegram недоступны: {type(exc).__name__}')

        result, offset_date, offset_id, offset_peer = [], None, 0, types.InputPeerEmpty()
        seen_pages = set()
        self.dialog_entities = {}
        self.completed, self.total, self.progress_unit = 0, 0, 'диалогов загружено'
        while True:
            page = await self.rpc(functions.messages.GetDialogsRequest(
                offset_date=offset_date, offset_id=offset_id, offset_peer=offset_peer,
                limit=100, hash=0, exclude_pinned=offset_id != 0))
            if not self.total:
                self.total = int(getattr(page, 'count', 0) or len(page.dialogs))
            entities = {utils.get_peer_id(e): e for e in [*page.users, *page.chats]}
            page_key = tuple((utils.get_peer_id(d.peer), d.top_message) for d in page.dialogs)
            if page_key and page_key in seen_pages:
                raise ValueError('Telegram повторяет страницу списка чатов. Загрузка остановлена; повторите позже.')
            seen_pages.add(page_key)
            messages = {(utils.get_peer_id(m.peer_id), m.id): m for m in page.messages
                        if getattr(m, 'peer_id', None) is not None}
            for dialog in page.dialogs:
                ident = utils.get_peer_id(dialog.peer)
                entity = entities.get(ident)
                title = utils.get_display_name(entity) if entity is not None else str(ident)
                self.report_progress(title=title, phase='Чтение сведений о диалоге')
                if entity is None or getattr(entity, 'left', False) or getattr(entity, 'self', False):
                    self.completed += 1
                    self.report_progress(title=title, phase='Диалог пропущен')
                    continue
                if isinstance(entity, types.Channel):
                    kind = 'channel' if getattr(entity, 'broadcast', False) else 'group'
                elif isinstance(entity, types.Chat):
                    kind = 'group'
                elif isinstance(entity, types.User):
                    kind = 'bot' if getattr(entity, 'bot', False) else 'private'
                else:
                    self.completed += 1
                    self.report_progress(title=title, phase='Неподдерживаемый тип диалога пропущен')
                    continue
                self.dialog_entities[ident] = entity
                count = await self.participant_count(entity) if isinstance(entity, (types.Chat, types.Channel)) else None
                message = messages.get((ident, dialog.top_message))
                stamp = int(message.date.timestamp()) if message is not None and getattr(message, 'date', None) else 0
                folder_ids, folder_names = self._dialog_folders(dialog, entity, filters)
                result.append({'id': ident, 'title': title or str(ident),
                               'kind': kind, 'last_activity': stamp,
                               'participants_count': count, 'folders': folder_ids,
                               'folder_names': folder_names})
                self.completed += 1
                self.report_progress(title=title, phase='Диалог добавлен в список')
                self.log(f'Загрузка чатов аккаунта: обработано {len(result)}')
            if len(page.dialogs) < 100:
                break
            last = page.dialogs[-1]
            message = messages.get((utils.get_peer_id(last.peer), last.top_message))
            if message is None:
                break
            offset_date, offset_id = message.date, message.id
            offset_peer = utils.get_input_peer(entities[utils.get_peer_id(last.peer)])

        unique = list({row['id']: row for row in result}.values())
        self.total = self.completed
        self.report_progress(phase='Список загружен')
        folders = {}
        for row in unique:
            for key, name in zip(row['folders'], row['folder_names']):
                folders[key] = name
        return {'account_dialogs': unique,
                'dialog_folders': [{'id': key, 'title': value} for key, value in folders.items()]}

    @staticmethod
    def _dialog_folders(dialog, entity, filters):
        archived = getattr(dialog, 'folder_id', None) == 1
        folder_ids = ['archive' if archived else 'main']
        folder_names = ['Архив' if archived else 'Основная']
        ident = utils.get_peer_id(dialog.peer)
        unread = bool(getattr(dialog, 'unread_count', 0) or getattr(dialog, 'unread_mark', False))
        notify = getattr(dialog, 'notify_settings', None)
        muted_until = getattr(notify, 'mute_until', None)
        if hasattr(muted_until, 'timestamp'):
            muted_until = muted_until.timestamp()
        muted = bool(muted_until and float(muted_until) > time.time())

        for item in filters:
            filter_id = getattr(item, 'id', None)
            if not filter_id:
                continue
            included = {utils.get_peer_id(peer) for peer in
                        [*(getattr(item, 'pinned_peers', None) or []),
                         *(getattr(item, 'include_peers', None) or [])]}
            excluded = {utils.get_peer_id(peer) for peer in (getattr(item, 'exclude_peers', None) or [])}
            explicit = ident in included
            category = False
            if isinstance(entity, types.User):
                category = ((getattr(entity, 'bot', False) and getattr(item, 'bots', False)) or
                            (getattr(entity, 'contact', False) and getattr(item, 'contacts', False)) or
                            (not getattr(entity, 'contact', False) and not getattr(entity, 'bot', False)
                             and getattr(item, 'non_contacts', False)))
            elif isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and not getattr(entity, 'broadcast', False)):
                category = bool(getattr(item, 'groups', False))
            elif isinstance(entity, types.Channel):
                category = bool(getattr(item, 'broadcasts', False))
            excluded_by_rule = (ident in excluded or
                                (archived and getattr(item, 'exclude_archived', False)) or
                                (not unread and getattr(item, 'exclude_read', False)) or
                                (muted and getattr(item, 'exclude_muted', False)))
            if (explicit or category) and not excluded_by_rule:
                title = getattr(item, 'title', '')
                title = getattr(title, 'text', title) or f'Папка {filter_id}'
                folder_ids.append(f'custom:{filter_id}')
                folder_names.append(str(title))
        return folder_ids, folder_names

    async def cleanup_dialogs(self, rows):
        """Leave groups/channels or remove private histories, one confirmed row at a time."""
        await self.account()
        unique = list({int(row['id']): row for row in rows}.values())
        if not unique:
            raise ValueError('Отметьте чаты для очистки')
        self.total, self.completed, self.progress_unit = len(unique), 0, 'чатов обработано'
        reports = []
        for row in unique:
            ident, title = int(row['id']), row.get('title') or str(row['id'])
            entity = self.dialog_entities.get(ident)
            try:
                self.check_cancel()
                self.report_progress(title=title, phase='Очистка диалога')
                if entity is None:
                    status = 'Пропущено: список устарел, обновите его'
                else:
                    if isinstance(entity, types.Channel):
                        await self.rpc(functions.channels.LeaveChannelRequest(utils.get_input_channel(entity)))
                        status = 'Выход из группы/канала подтверждён Telegram'
                    elif isinstance(entity, types.Chat):
                        await self.rpc(functions.messages.DeleteChatUserRequest(
                            chat_id=entity.id, user_id=types.InputUserSelf(), revoke_history=False))
                        status = 'Выход из группы подтверждён Telegram'
                    else:
                        rounds = 0
                        while True:
                            affected = await self.rpc(functions.messages.DeleteHistoryRequest(
                                peer=utils.get_input_peer(entity), max_id=0, just_clear=False, revoke=False))
                            rounds += 1
                            if not getattr(affected, 'offset', 0):
                                break
                            if rounds >= 100:
                                raise Stopped('История чата слишком велика. Операция остановлена; обновите список.')
                        status = 'Диалог и локальная история удалены из аккаунта'
                    self.dialog_entities.pop(ident, None)
            except Stopped as exc:
                return {'cleanup_reports': reports,
                        'done': f'{exc} До остановки подтверждено {sum(report["ok"] for report in reports)} диалогов. Обновите список перед повтором.'}
            except errors.RPCError as exc:
                if getattr(exc, 'code', None) == 401:
                    raise
                status = f'Ошибка Telegram: {type(exc).__name__}'
            self.completed += 1
            ok = status.startswith(('Выход', 'Диалог'))
            reports.append({'id': ident, 'title': title, 'ok': ok, 'status': status})
            self.log(f'{title}: {status}')
            self.report_progress(title=title, phase=status)
        succeeded = sum(report['ok'] for report in reports)
        return {'cleanup_reports': reports,
                'done': f'Очистка завершена: {succeeded} из {len(reports)} диалогов удалены/покинуты.'}

    async def participant_count(self, entity):
        count = getattr(entity, 'participants_count', None)
        if count is not None:
            return count
        try:
            if isinstance(entity, types.Channel):
                response = await self.rpc(functions.channels.GetFullChannelRequest(utils.get_input_channel(entity)))
                return getattr(response.full_chat, 'participants_count', None)
            response = await self.rpc(functions.messages.GetFullChatRequest(entity.id))
            participants = getattr(response.full_chat, 'participants', None)
            if isinstance(participants, types.ChatParticipants):
                return len(participants.participants)
        except errors.RPCError as exc:
            if getattr(exc, 'code', None) == 401:
                raise
            self.log(f'Количество участников группы {entity.id} недоступно: {type(exc).__name__}')
        return None

    async def schedule_update(self, update):
        # No RPC or automatic mutations here; only evidence supplied by Telegram.
        if not self.identity_verified:
            return
        if isinstance(update, types.UpdateDeleteScheduledMessages):
            chat = utils.get_peer_id(update.peer)
            sent = getattr(update, 'sent_messages', None)
            if sent and len(sent) == len(update.messages):
                for scheduled, actual in zip(update.messages, sent):
                    self.store.meta(f'delivery:{chat}:{actual}', json.dumps({'scheduled_id': scheduled, 'message_id': actual, 'observed': int(time.time())}))
            self.log(f'Чат {chat}: сервер изменил очередь; выполните синхронизацию. Отправок в полученном событии: {len(sent or [])}.')
        elif isinstance(update, types.UpdateNewScheduledMessage):
            chat = utils.get_peer_id(update.message.peer_id)
            self.log(f'Чат {chat}: новая или изменённая отложка ID {update.message.id}; требуется сверка очереди.')

    def peer(self, chat):
        if chat not in self.entities:
            raise ValueError('Сначала загрузите группы Telegram')
        return self.entities[chat]

    async def slowmode(self, chat):
        peer = self.peer(chat)
        if isinstance(peer, types.InputPeerChannel):
            full = await self.rpc(functions.channels.GetFullChannelRequest(peer))
            next_date = getattr(full.full_chat, 'slowmode_next_send_date', None)
            self.next_allowed[chat] = (int(next_date.timestamp()) if hasattr(next_date, 'timestamp') else int(next_date or 0)) + 60
            return getattr(full.full_chat, 'slowmode_seconds', None) or 0
        return 0

    async def earliest(self, chat, interval):
        recent = await self.rpc(functions.messages.SearchRequest(
            peer=self.peer(chat), q='', from_id=types.InputPeerSelf(),
            filter=types.InputMessagesFilterEmpty(), min_date=None, max_date=None,
            offset_id=0, add_offset=0, limit=1, max_id=0, min_id=0, hash=0))
        last = max((int(m.date.timestamp()) for m in recent.messages if getattr(m, 'date', None)), default=0)
        return max(last + interval, self.next_allowed.get(chat, 0))

    async def history(self, chat):
        result = await self.rpc(functions.messages.GetScheduledHistoryRequest(self.peer(chat), hash=0))
        return list(result.messages)

    def repeat_guard(self, repeat):
        if repeat and not capability()['repeat']:
            raise ValueError('В установленном Telethon нет repeat API. Доступен только явный разовый режим.')
        if repeat and not self.premium:
            raise ValueError('Ежедневный повтор требует Telegram Premium. Выберите разовый режим явно.')

    async def prepare(self, chats, text, start, repeat, cap, pause):
        self.plan = []
        self.pause = max(1, min(60, float(pause)))
        await self.account()
        self.repeat_guard(repeat)
        if isinstance(text, str):
            validate_text(text)
        now = int(time.time())
        output = []
        for chat in chats:
            template_id = chat.get('template_id')
            names = template_names(chat) if isinstance(text, dict) else [template_id]
            contents = []
            for name in names:
                value, formatting = parse_offer(text.get(name, '') if isinstance(text, dict) else text)
                contents.append({'template_id': name, 'text': value, 'entities': encode(formatting)})
            offer, entities = contents[0]['text'], decode(contents[0]['entities'])
            if not chat['allowed'] or not chat['rules'].strip():
                raise ValueError(f"{chat['title']}: подтвердите разрешение администратора и укажите правила")
            slow = await self.slowmode(chat['id'])
            interval = interval_seconds(slow, chat['manual'], self.margin)
            remote = await self.history(chat['id'])
            old = self.store.jobs(chat['id'])
            if old:
                active = [j for j in old if j['state'] not in ('deleted', 'cancelled')]
                if self.store.pending_creation(chat['id']) or any(j['repeat'] != repeat or not any(j['text'] == c['text'] and (j.get('entities') or '[]') == c['entities'] for c in contents) for j in active):
                    raise ValueError(f"{chat['title']}: уже есть локальный план с другим текстом/режимом или незавершёнными слотами. Используйте обновление шаблона либо «Продолжить».")
                _, issues = self.sync(chat['id'], remote)
                if issues:
                    raise ValueError(f"{chat['title']}: часть локальных слотов не сопоставлена с сервером. Сначала проверьте очередь и связи.")
            occupied = [int(m.date.timestamp()) for m in remote]
            first = max(start, await self.earliest(chat['id'], interval))
            slots = make_slots(first, now, interval, occupied, max(0, cap - len(remote)), allowed=lambda stamp: within_window(stamp, chat))
            if self.test_mode:
                slots = slots[:1]
            output.append({**chat, 'chat': chat['id'], 'slots': slots, 'text': offer,
                           'repeat': repeat, 'test_mode': self.test_mode, 'entities': encode(entities), 'interval': interval, 'snapshot': snapshot(remote),
                           'slow': slow, 'existing': len(remote)})
            offset = len([j for j in old if j['state'] not in ('deleted', 'cancelled')])
            output[-1]['slot_contents'] = {str(stamp): contents[(offset+i) % len(contents)] for i,stamp in enumerate(slots)}
        if not any(p['slots'] for p in output):
            raise ValueError('Нет свободных слотов с учётом интервалов и выбранного предела очереди')
        self.plan = output
        self.mutation_limit([p['chat'] for p in output if p['slots']], sum(len(p['slots']) for p in output))
        return {'plan': output}

    @staticmethod
    def future(stamp):
        if stamp <= time.time() + 60:
            raise ValueError('Слот слишком близко или уже прошёл. Отправка остановлена: немедленной публикации не будет.')

    async def execute(self):
        if not self.plan:
            raise ValueError('Сначала подготовьте предварительный просмотр')
        await self.account()
        self.mutation_limit([p['chat'] for p in self.plan if p['slots']], sum(len(p['slots']) for p in self.plan))
        for p in self.plan:
            self.repeat_guard(p['repeat'])
            if snapshot(await self.history(p['chat'])) != p['snapshot']:
                raise ValueError('Очередь изменилась после просмотра. Подготовьте план заново.')
            if interval_seconds(await self.slowmode(p['chat']), p['manual'], self.margin) != p['interval']:
                raise ValueError('Slow mode изменился. Подготовьте план заново.')
            if p['slots']:
                self.future(p['slots'][0])
        self.store.add_plan(self.plan)
        result = await self.resume(self.plan)
        self.plan = []
        return result

    async def resume(self, chats):
        await self.account()
        work = []
        for config in chats:
            for job in self.store.jobs(config.get('chat', config['id'])):
                if job['state'] not in ('verified', 'deleted', 'cancelled'):
                    work.append((job['stamp'], config))
        if work:
            self.mutation_limit([c['id'] for _, c in work], len(work))
        self.completed, self.total = 0, len(work)
        self.progress_unit = 'подтверждено'
        reports = {c['id']: {'chat': c['id'], 'status': 'создание завершено'} for c in chats}
        blocked, context = set(), {}
        # Populate earliest slots across all selected chats first. Finishing all
        # 90 slots of chat A before starting B could make B's first dates expire.
        for stamp, config in sorted(work, key=lambda pair: (pair[0], pair[1]['id'])):
            if config['id'] in blocked:
                continue
            try:
                self.report_progress(chat=config['id'], title=config.get('title', str(config['id'])), phase='Создание')
                await self._resume_impl([config], only_stamp=stamp, context=context)
                self.completed += 1
                jobs = self.store.jobs(config['id'])
                active = [j for j in jobs if j['state'] not in ('deleted', 'cancelled')]
                status = f"Подтверждено {sum(j['state'] == 'verified' for j in active)} из {len(active)}"
                reports[config['id']]['status'] = status
                self.report_progress(chat=config['id'], status=status)
            except Stopped:
                raise
            except (ValueError, errors.RPCError) as exc:
                self.report_progress(chat=config['id'], status=str(exc) if isinstance(exc, ValueError) else error_description(exc))
                if len(chats) == 1:
                    raise
                message = str(exc) if isinstance(exc, ValueError) else error_description(exc)
                blocked.add(config['id'])
                reports[config['id']] = {'chat': config['id'], 'status': message, 'error': True}
                self.log(f"Группа {config['id']}: {message}")
        reports = list(reports.values())
        failures = sum(bool(r.get('error')) for r in reports)
        return {'done': f'Обработано групп: {len(reports)}. Ошибок: {failures}. Подтверждённые слоты не дублируются.',
                'chat_reports': reports}

    async def _resume_impl(self, chats, only_stamp=None, context=None):
        context = {} if context is None else context
        for config in chats:
            chat = config.get('chat', config['id'])
            if not config['allowed'] or not config['rules'].strip():
                raise ValueError('Нет подтверждения разрешения администратора / правил')
            if chat not in context or time.monotonic() - context[chat][2] > 60:
                interval = interval_seconds(await self.slowmode(chat), config['manual'], self.margin)
                earliest = await self.earliest(chat, interval)
                context[chat] = (interval, earliest, time.monotonic())
            interval, earliest, _ = context[chat]
            jobs = self.store.jobs(chat)
            remote = await self.history(chat)
            for j in jobs:
                if only_stamp is not None and j['stamp'] != only_stamp:
                    continue
                self.check_cancel()
                if j['state'] in ('verified', 'deleted', 'cancelled'):
                    continue  # Never refill a previously completed daily campaign.
                self.repeat_guard(j['repeat'])
                matches = [m for m in remote if (m.id == j['message_id'] if j['message_id'] else
                           int(m.date.timestamp()) == j['stamp'] and m.message == j['text'])]
                if len(matches) == 1:
                    self.verify(chat, j, matches[0])
                    continue
                if j['state'] == 'unverified':
                    raise ValueError('Есть неподтверждённое сообщение. Проверьте его в очереди и удалите перед новым планом.')
                self.future(j['stamp'])
                if not within_window(j['stamp'], config):
                    raise ValueError('Сохранённый слот вне разрешённых часов. Проверьте расписание; отправка остановлена.')
                if j['stamp'] < earliest:
                    raise ValueError('Слот нарушает интервал после последнего сообщения аккаунта / текущий slow mode. Остановлено.')
                if j['state'] == 'sending':
                    # Ambiguous network outcome: no blind resend or fresh random_id.
                    raise ValueError('Неопределённый результат прежнего запроса. Очередь не подтверждает его: автоматический повтор заблокирован. Проверьте Telegram.')
                if any(distance(j['stamp'], int(m.date.timestamp())) < interval for m in remote):
                    raise ValueError('Конфликт с текущей очередью или новыми правилами. Остановлено.')
                if j['repeat'] and interval > DAY:
                    raise ValueError('Ежедневный повтор конфликтует с интервалом больше суток')
                args = dict(peer=self.peer(chat), message=j['text'], random_id=j['random_id'],
                            schedule_date=datetime.fromtimestamp(j['stamp'], timezone.utc), no_webpage=True, entities=decode(j.get('entities')))
                if j['repeat']:
                    args['schedule_repeat_period'] = j['repeat']
                self.log(f"Чат {chat}: создание слота {datetime.fromtimestamp(j['stamp']):%d.%m %H:%M}, повтор {j['repeat']}")
                try:
                    async def send():
                        return await self.rpc(functions.messages.SendMessageRequest(**args), lambda: self.future(j['stamp']),
                                              on_attempt=lambda: self.store.update(chat, j['stamp'], state='sending'),
                                              on_wait=lambda: self.store.update(chat, j['stamp'], state='pending'))
                    previous_pause = self.pause
                    self.pause = self.create_pause
                    try:
                        await send()
                    finally:
                        self.pause = previous_pause
                except errors.RPCError as exc:
                    # An explicit server rejection is not an ambiguous network result.
                    definitive = getattr(exc, 'code', 500) < 500
                    self.store.update(chat, j['stamp'], state='pending' if definitive else 'sending', error=type(exc).__name__)
                    raise
                remote = await self.history(chat)
                matches = [m for m in remote if int(m.date.timestamp()) == j['stamp'] and m.message == j['text']]
                if len(matches) != 1:
                    raise ValueError('Серверная очередь не подтвердила единственное сообщение. Остановлено без повторной отправки.')
                self.verify(chat, j, matches[0])
        return {'done': 'Продолжение завершено; уже подтверждённые слоты повторно не создавались.'}

    def verify(self, chat, job, msg):
        actual = getattr(msg, 'schedule_repeat_period', None) or 0
        ok = actual == job['repeat'] and msg.message == job['text'] and same_entities(msg, job.get('entities'))
        self.store.update(chat, job['stamp'], message_id=msg.id, server_stamp=int(msg.date.timestamp()),
                          sync_state='связано' if ok else 'конфликт', state='verified' if ok else 'unverified')
        if not ok:
            raise ValueError('Повтор, текст или форматирование (включая кастомные эмодзи) НЕ подтверждены сервером. Проверьте сообщение в очереди Telegram. Работа остановлена без повторной отправки.')
        self.log(f'Подтверждено сервером: чат {chat}, ID {msg.id}, повтор {actual} с')

    async def queues(self, chats):
        # One finite pass per explicitly requested chat; never fan out to saved profiles.
        chats = list({chat['id']: chat for chat in chats}.values())
        self.completed, self.total = 0, len(chats)
        self.progress_unit = 'групп обработано'
        self.report_progress(title='', chat=None, phase='Проверка очереди')
        if not chats:
            return {'queue': [], 'chat_reports': [], 'diagnostics': []}
        await self.account()
        rows = []
        reports = []
        diagnostic_lines = []
        operation_issues = {}
        for operation in self.store.operations():
            payload = json.loads(operation['payload'])
            row = payload.get('row', {})
            operation_issues[(row.get('chat'), row.get('id'))] = operation.get('error') or 'Есть незавершённое изменение; проверьте очередь'
        for chat in chats:
            try:
                self.check_cancel()
                self.report_progress(chat=chat['id'], title=chat['title'], phase='Чтение очереди', status='Проверяется очередь')
                self.log(f"Проверка очереди {self.completed+1}/{self.total}: {chat['title']}")
                messages = await self.history(chat['id'])
                linked, issues = self.sync(chat['id'], messages)
                slow = await self.slowmode(chat['id'])
                diagnosis = diagnose(self.store.jobs(chat['id']), messages, slow, chat.get('manual', 15), self.margin)
                diagnostic_lines += [f"{chat['title']} / {chat['id']}", *diagnosis['lines'], '']
                observed = self.store.db.execute('SELECT COUNT(*) FROM meta WHERE key LIKE ?', (f"delivery:{chat['id']}:%",)).fetchone()[0]
                diagnostic_lines.append(f'Подтверждено полученными серверными событиями: {observed} отправок за время наблюдений. Это не полный итог за время отключения приложения.')
                self.store.meta('diagnostics:' + str(chat['id']), json.dumps({'time': int(time.time()), **diagnosis}, ensure_ascii=False))
                reports.append({'chat': chat['id'], 'slow': slow,
                                'status': f'В очереди {len(messages)}; связанных {len(linked)}; проверить {len(issues)}'})
            except (ValueError, errors.RPCError) as exc:
                reports.append({'chat': chat['id'], 'status': str(exc) if isinstance(exc, ValueError) else error_description(exc), 'error': True})
                self.completed += 1
                self.report_progress(chat=chat['id'], status=reports[-1]['status'])
                continue
            self.completed += 1
            self.report_progress(chat=chat['id'], status=reports[-1]['status'], phase='Очередь проверена', wait_until=0)
            for msg in messages:
                job = linked.get(msg.id)
                rows.append({'chat': chat['id'], 'title': chat['title'], 'id': msg.id,
                             'stamp': int(msg.date.timestamp()), 'text': msg.message or '',
                             'repeat': getattr(msg, 'schedule_repeat_period', None) or 0,
                             'entities': encode(getattr(msg, 'entities', None)), 'media': msg.media is not None, 'job_stamp': job['stamp'] if job else None,
                             'template_id': job.get('template_id') if job else None,
                             'manual': chat.get('manual', 15), 'slow': slow,
                             'allowed': chat.get('allowed', False)})
                rows[-1].update(window_start=chat.get('window_start', 0), window_end=chat.get('window_end', 0))
                rows[-1]['issue'] = operation_issues.get((chat['id'], msg.id)) or (job.get('error') or '' if job else 'Не связано с локальным планом')
        rows.sort(key=lambda r: (r['stamp'], r['chat'], r['id']))
        self.queue_cache = rows
        snapshot_path = self.directory / 'queue_snapshot.json'
        try:
            cached = json.loads(snapshot_path.read_text(encoding='utf-8')) if snapshot_path.exists() else {}
            if not isinstance(cached, dict):
                cached = {}
        except (OSError, ValueError):
            cached = {}
            self.log('Старый снимок обзора недоступен; показана свежая очередь Telegram.')
        for report in reports:
            if not report.get('error'):
                cached[str(report['chat'])] = {'checked': int(time.time()), 'rows': [r for r in rows if r['chat'] == report['chat']]}
        try:
            save_json(snapshot_path, cached)
        except OSError:
            self.log('Не удалось сохранить снимок обзора на диск; текущая очередь прочитана.')
        return {'queue': rows, 'chat_reports': reports, 'diagnostics': diagnostic_lines}

    async def preflight(self, chats, templates, start, repeat, cap, pause):
        reports = []
        self.plan = []
        await self.account()
        for chat in chats:
            self.check_cancel()
            try:
                rights = await self.write_access(chat['id'])
                result = await self.prepare([chat], templates, start, repeat, cap, pause)
                p = result['plan'][0]
                reports.append(f"{chat['title']}: {rights}; в очереди {p['existing']}; можно подготовить {len(p['slots'])}; интервал {p['interval']//60} мин. Фактическую квоту подтвердит Telegram при создании.")
            except (ValueError, errors.RPCError) as exc:
                reports.append(f"{chat['title']}: НЕ ГОТОВО — {str(exc) if isinstance(exc, ValueError) else error_description(exc)}")
            finally:
                self.plan = []
        return {'preflight': reports}

    async def write_access(self, chat):
        peer = self.peer(chat)
        if isinstance(peer, types.InputPeerChannel):
            full = await self.rpc(functions.channels.GetFullChannelRequest(peer))
            membership = await self.rpc(functions.channels.GetParticipantRequest(peer, types.InputPeerSelf()))
            participant = membership.participant
            if isinstance(participant, types.ChannelParticipantLeft) or getattr(participant, 'left', False):
                raise ValueError('Аккаунт не участник группы')
            personal = getattr(participant, 'banned_rights', None)
            if personal and any(getattr(personal, flag, False) for flag in ('view_messages', 'send_messages', 'send_plain')):
                raise ValueError('Аккаунту запрещена отправка текста')
            entity = next((c for c in full.chats if getattr(c, 'id', None) == peer.channel_id), None)
            privileged = isinstance(participant, (types.ChannelParticipantCreator, types.ChannelParticipantAdmin))
        else:
            full = await self.rpc(functions.messages.GetFullChatRequest(peer.chat_id))
            members = getattr(getattr(full.full_chat, 'participants', None), 'participants', None)
            if members is None or not any(getattr(p, 'user_id', None) == int(self.store.meta('account') or 0) for p in members):
                raise ValueError('Telegram не подтвердил участие аккаунта в группе')
            entity = next((c for c in full.chats if getattr(c, 'id', None) == peer.chat_id), None)
            privileged = bool(getattr(entity, 'creator', False) or getattr(entity, 'admin_rights', None))
        if entity is None:
            raise ValueError('Telegram не подтвердил права группы')
        if getattr(entity, 'left', False) or getattr(entity, 'deactivated', False):
            raise ValueError('Группа недоступна аккаунту')
        rights = getattr(entity, 'default_banned_rights', None)
        if not privileged and rights and (getattr(rights, 'send_messages', False) or getattr(rights, 'send_plain', False)):
            raise ValueError('Группа запрещает отправку текста обычным участникам')
        return 'права на текст проверены'

    def sync(self, chat, messages):
        jobs = self.store.jobs(chat)
        matches, issues = reconcile(jobs, messages)
        linked = {}
        for job in jobs:
            msg = matches.get(job['stamp'])
            if msg is not None:
                self.store.update(chat, job['stamp'], message_id=msg.id,
                                  server_stamp=int(msg.date.timestamp()), sync_state='связано')
                linked[msg.id] = job
            elif job['stamp'] in issues:
                self.store.update(chat, job['stamp'], sync_state=issues[job['stamp']])
        return linked, issues

    async def mutate(self, selected, action, text=''):
        plain, entities = parse_offer(text) if action == 'edit' else ('', [])
        changes = [{'row': dict(row), 'action': action, 'text': plain, 'entities': encode(entities)} for row in selected]
        result = await self.apply_changes(changes)
        # Refresh every chat shown in the table, not just the modified selection.
        chats = {row['chat']: {'id': row['chat'], 'title': row['title'],
                 'allowed': row.get('allowed', False), 'manual': row.get('manual', 15),
                 'window_start': row.get('window_start', 0), 'window_end': row.get('window_end', 0)}
                 for row in [*self.queue_cache, *selected]}
        refreshed = await self.queues(list(chats.values()))
        result['queue'] = refreshed['queue']
        result['diagnostics'] = refreshed.get('diagnostics', [])
        result['chat_reports'] = result.get('chat_reports', []) + refreshed.get('chat_reports', [])
        return result

    async def prepare_updates(self, chats, templates):
        result = await self.queues(chats)
        changes = []
        for row in result['queue']:
            template = row.get('template_id')
            if template not in templates:
                continue
            new_text, new_entities = parse_offer(templates[template])
            if row['text'] != new_text or row.get('entities', '[]') != encode(new_entities):
                if not row.get('allowed'):
                    raise ValueError(f"{row['title']}: подтвердите разрешение администратора")
                validate_text(new_text)
                changes.append({'row': row, 'action': 'edit', 'text': new_text, 'entities': encode(new_entities)})
        self.change_plan = changes
        self.mutation_limit([c['row']['chat'] for c in changes], len(changes)) if changes else None
        return {'changes': changes, 'chat_reports': result['chat_reports']}

    async def pending_changes(self, chats):
        allowed_chats = {c['id'] for c in chats}
        self.change_plan = []
        for operation in self.store.operations():
            payload = json.loads(operation['payload'])
            if payload['row']['chat'] in allowed_chats:
                payload['operation_id'] = operation['id']
                self.change_plan.append(payload)
        return {'changes': self.change_plan}

    async def apply_prepared_changes(self):
        changes = list(self.change_plan)
        result = await self.apply_changes(changes)
        self.change_plan = []
        chats = {row['chat']: {'id': row['chat'], 'title': row['title'], 'allowed': row.get('allowed', False),
                             'manual': row.get('manual', 15), 'window_start': row.get('window_start', 0),
                             'window_end': row.get('window_end', 0)} for row in [*self.queue_cache, *(c['row'] for c in changes)]}
        if chats:
            refreshed = await self.queues(list(chats.values()))
            result['queue'] = refreshed['queue']
            result['diagnostics'] = refreshed.get('diagnostics', [])
        return result

    async def apply_changes(self, changes):
        if not changes:
            return {'done': 'Нет изменений для применения.'}
        self.mutation_limit([c['row']['chat'] for c in changes], len(changes))
        await self.account()
        self.completed, self.total = 0, len(changes)
        self.progress_unit = 'подтверждено'
        if any(c['action'] == 'shift' for c in changes):
            await self.validate_shifts(changes)
        reports = []
        blocked_chats = set()
        # Persist the entire confirmed batch before the first server mutation.
        for change in changes:
            if change['action'] not in ('edit', 'delete', 'repeat_daily', 'repeat_once', 'shift'):
                raise ValueError('Неизвестное действие')
            if change['action'] == 'edit':
                validate_text(change['text'])
            if 'operation_id' not in change:
                change['operation_id'] = self.store.new_operation(change)
        for change in changes:
            self.check_cancel()
            row = change['row']
            if row['chat'] in blocked_chats:
                continue
            try:
                self.report_progress(chat=row['chat'], title=row['title'], phase='Изменение очереди')
                await self.apply_one(change)
                self.completed += 1
                self.report_progress(chat=row['chat'], status=f'Подтверждено: {self.completed} из {self.total}')
                reports.append({'chat': row['chat'], 'status': f"Подтверждено: {change['action']}, ID {row['id']}"})
            except Stopped:
                raise
            except (ValueError, errors.RPCError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else error_description(exc)
                self.store.operation_state(change['operation_id'], 'error', message)
                blocked_chats.add(row['chat'])
                reports.append({'chat': row['chat'], 'status': message, 'error': True})
                self.report_progress(chat=row['chat'], status=message)
                if len(changes) == 1:
                    raise
        for report in reports:
            self.log(f"Группа {report['chat']}: {report['status']}")
        return {'done': f"Подтверждено изменений: {sum(not r.get('error') for r in reports)}. "
                        f"Групп с ошибками: {len(blocked_chats)}. Незавершённые изменения сохранены.",
                'chat_reports': reports}

    async def apply_one(self, change):
        row, action, text = change['row'], change['action'], change.get('text', '')
        remote = await self.history(row['chat'])
        job = next((j for j in self.store.jobs(row['chat']) if j['stamp'] == row.get('job_stamp')), None)
        current = None
        if action == 'shift':
            current = next((m for m in remote if m.id == row['id'] and int(m.date.timestamp()) in
                            {row['stamp'], change['target_stamp']}), None)
        elif job:
            # Allow only the approved old/new text, not unrelated external edits.
            candidates = []
            for expected_repeat in {row['repeat'], 86400 if action == 'repeat_daily' else 0 if action == 'repeat_once' else row['repeat']}:
                probe = {**job, 'text': row['text'], 'pending_text': text if action == 'edit' else None,
                         'repeat': expected_repeat, 'entities': row.get('entities', '[]'),
                         'pending_entities': change.get('entities', '[]') if action == 'edit' else None}
                candidates.extend(m for m in remote if compatible(probe, m))
            unique = {m.id: m for m in candidates}
            if len(unique) == 1:
                current = next(iter(unique.values()))
                # Other local slots must not plausibly own the same server message.
                for other in self.store.jobs(row['chat']):
                    if other['stamp'] != job['stamp'] and other['state'] not in ('deleted', 'cancelled') and compatible(other, current):
                        raise ValueError('Неоднозначная связь сообщений. Требуется ручная привязка.')
        else:
            current = next((m for m in remote if m.id == row['id'] and int(m.date.timestamp()) == row['stamp']), None)
        if current is None:
            if action == 'delete' and job and job['state'] == 'deleted':
                self.store.operation_state(change['operation_id'], 'done')
                return
            raise ValueError('Очередь изменилась. Обновите список и подтвердите действие заново.')
        old_repeat = getattr(current, 'schedule_repeat_period', None) or 0
        target_text = text if action == 'edit' else row['text']
        target_entities = change.get('entities', '[]') if action == 'edit' else row.get('entities', '[]')
        if not same_entities(current, row.get('entities')) and not (action == 'edit' and same_entities(current, target_entities)):
            raise ValueError('Форматирование изменено вне программы; обновите предпросмотр')
        target_repeat = 86400 if action == 'repeat_daily' else 0 if action == 'repeat_once' else row['repeat']
        if current.message not in ({row['text'], target_text} if action == 'edit' else {row['text']}):
            raise ValueError('Очередь изменилась: текст редактировался вне программы')
        if old_repeat not in {row['repeat'], target_repeat}:
            raise ValueError('Период повтора изменён вне программы')
        stamp = int(current.date.timestamp())
        if action == 'shift':
            stamp = change['target_stamp']
            await self.validate_shifts([change])
        if action == 'delete':
            self.store.operation_state(change['operation_id'], 'sending')
            await self.rpc(functions.messages.DeleteScheduledMessagesRequest(self.peer(row['chat']), [current.id]))
            if any(m.id == current.id for m in await self.history(row['chat'])):
                raise ValueError('Удаление не подтверждено')
            if job:
                self.store.update(row['chat'], job['stamp'], state='deleted', pending_text=None)
        else:
            if row['media']:
                raise ValueError('Редактирование вложений в этой версии не поддерживается')
            self.repeat_guard(target_repeat)
            if action == 'repeat_daily':
                if not row.get('allowed'):
                    raise ValueError('Подтвердите разрешение администратора')
                interval = interval_seconds(await self.slowmode(row['chat']), row.get('manual', 15), self.margin)
                if interval > DAY or any(distance(stamp, int(m.date.timestamp())) < interval for m in remote if m.id != current.id):
                    raise ValueError('Ежедневный повтор нарушит интервал с другими сообщениями')
            if current.message != target_text or old_repeat != target_repeat or not same_entities(current, target_entities) or int(current.date.timestamp()) != stamp:
                self.future(stamp)
                if job:
                    self.store.update(row['chat'], job['stamp'], pending_text=target_text, pending_entities=target_entities,
                                      server_stamp=int(current.date.timestamp()), message_id=current.id)
                self.store.operation_state(change['operation_id'], 'sending')
                args = dict(peer=self.peer(row['chat']), id=current.id, message=target_text, no_webpage=True,
                            schedule_date=datetime.fromtimestamp(stamp, timezone.utc), entities=decode(target_entities))
                if capability()['repeat']:
                    args['schedule_repeat_period'] = target_repeat
                await self.rpc(functions.messages.EditMessageRequest(**args), lambda: self.future(stamp))
                after = next((m for m in await self.history(row['chat']) if m.id == current.id), None)
                if (after is None or after.message != target_text or not same_entities(after, target_entities) or int(after.date.timestamp()) != stamp
                        or (getattr(after, 'schedule_repeat_period', None) or 0) != target_repeat):
                    raise ValueError('Сервер не подтвердил текст, дату или повтор. Изменение остановлено.')
            if job:
                self.store.update(row['chat'], job['stamp'], text=target_text, repeat=target_repeat,
                                  message_id=current.id, server_stamp=stamp, pending_text=None, entities=target_entities, pending_entities=None, sync_state='связано')
        self.store.operation_state(change['operation_id'], 'done')

    async def prepare_shift(self, rows, seconds):
        if type(seconds) is not int or not seconds:
            raise ValueError('Укажите ненулевой сдвиг времени')
        await self.account()
        self.mutation_limit([r['chat'] for r in rows], len(rows))
        changes = [{'row': dict(r), 'action': 'shift', 'target_stamp': r['stamp'] + seconds} for r in rows]
        # Move later slots first when shifting forward to avoid transient collisions.
        changes.sort(key=lambda c: c['row']['stamp'], reverse=seconds > 0)
        await self.validate_shifts(changes)
        self.change_plan = changes
        return {'shift_changes': changes}

    async def validate_shifts(self, changes):
        by_chat = {}
        for change in changes:
            if change['action'] == 'shift':
                by_chat.setdefault(change['row']['chat'], []).append(change)
        for chat, batch in by_chat.items():
            remote = await self.history(chat)
            positions = {m.id: int(m.date.timestamp()) for m in remote}
            repeats = {m.id: getattr(m, 'schedule_repeat_period', 0) or 0 for m in remote}
            interval = interval_seconds(await self.slowmode(chat), batch[0]['row'].get('manual', 15), self.margin)
            earliest = await self.earliest(chat, interval)
            for change in batch:
                row, target = change['row'], change['target_stamp']
                current = next((m for m in remote if m.id == row['id']), None)
                if not row.get('allowed'):
                    raise ValueError('Подтвердите разрешение администратора для переноса')
                if current is None or current.message != row['text'] or not same_entities(current, row.get('entities')) or repeats[row['id']] != row['repeat'] or positions[row['id']] not in {row['stamp'], target}:
                    raise ValueError('Очередь изменилась. Обновите её перед переносом.')
                self.future(target)
                if not within_window(target, row):
                    raise ValueError('Новое время вне разрешённых часов группы')
                if target >= time.time() + DAY or target < earliest:
                    raise ValueError('Перенос должен быть в ближайших 24 часах и соблюдать интервал после последней публикации.')
                if row['repeat'] and interval > DAY:
                    raise ValueError('Интервал несовместим с ежедневным повтором')
                if any((distance(target, stamp) if row['repeat'] or repeats[ident] else abs(target-stamp)) < interval
                       for ident, stamp in positions.items() if ident != row['id']):
                    raise ValueError('Перенос нарушит интервал или суточный переход. Выберите другой сдвиг; временные конфликты также запрещены.')
                positions[row['id']] = target

    async def bind_rows(self, rows, template_id):
        await self.account()
        if not template_id:
            raise ValueError('Выберите шаблон')
        for row in rows:
            remote = await self.history(row['chat'])
            current = next((m for m in remote if snapshot([m])[0] == (row['id'], row['stamp'], row['text'], row['repeat'])), None)
            if current is None or row['media']:
                raise ValueError('Очередь изменилась либо выбрано вложение')
            matches = [j for j in self.store.jobs(row['chat']) if j['message_id'] == row['id']
                       or distance(j.get('server_stamp') or j['stamp'], row['stamp']) <= 120]
            if len(matches) > 1:
                raise ValueError('Несколько локальных слотов претендуют на сообщение; автоматическая привязка остановлена')
            if matches:
                job = matches[0]
            else:
                self.store.add_plan([{'chat': row['chat'], 'slots': [row['stamp']], 'text': row['text'],
                                      'repeat': row['repeat'], 'template_id': template_id}])
                job = next(j for j in self.store.jobs(row['chat']) if j['stamp'] == row['stamp'])
            self.store.update(row['chat'], job['stamp'], template_id=template_id, text=row['text'],
                              message_id=row['id'], repeat=row['repeat'], server_stamp=row['stamp'],
                              state='verified', pending_text=None, entities=row.get('entities', '[]'), pending_entities=None, sync_state='связано')
        return {'done': f'К шаблону «{template_id}» привязано: {len(rows)}. Тексты в Telegram не менялись.'}

    async def clear_progress(self, chats):
        await self.account()
        for chat in chats:
            if await self.history(chat['id']):
                raise ValueError('Сброс плана разрешён только при пустой серверной очереди выбранной группы')
        for chat in chats:
            self.store.clear(chat['id'])
        return {'done': 'Локальные планы очищены. Новые сообщения не создавались.'}

    async def close(self):
        try:
            if self.client:
                if hasattr(self.client, 'session') and self.identity_verified:
                    self.save_session()
                try:
                    await self.network(lambda: self.client.disconnect(), 'Отключение', cancellable=False, timeout=5)
                except TimeoutError:
                    self.log('Отключение превысило время ожидания. Локальные данные закрываются.')
        finally:
            self.store.db.close()
