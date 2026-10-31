"""The actual Telegram chat flow. Also reused by the local chat simulator."""
import json
import re
from urllib.parse import urlencode

from engine import GameError, require


CREATE = "Создать комнату"
JOIN = "Войти по коду"
OPEN = "Открыть игру"


class ChatBot:
    def __init__(self, engine, config):
        self.engine, self.config = engine, config
        with engine.lock:
            engine.db.execute('CREATE TABLE IF NOT EXISTS chat_updates(id INTEGER PRIMARY KEY, response TEXT)')
            engine.db.commit()

    def process(self, uid, name, text, update_id=None):
        with self.engine.lock:
            if update_id is not None:
                row = self.engine.db.execute('SELECT response FROM chat_updates WHERE id=?', (update_id,)).fetchone()
                if row:
                    return json.loads(row[0])
            result = self.handle(uid, name, text)
            if update_id is not None:
                self.engine.db.execute('INSERT INTO chat_updates VALUES(?,?)', (update_id, json.dumps(result, ensure_ascii=False)))
                self.engine.db.execute('DELETE FROM chat_updates WHERE id NOT IN (SELECT id FROM chat_updates ORDER BY id DESC LIMIT 1000)')
                self.engine.db.commit()
            return result

    def keyboard(self):
        rows = [[{"text": CREATE}, {"text": JOIN}]]
        # KeyboardButton Mini Apps have empty initData. Open through an inline
        # button or the bot menu instead, so the backend can authenticate users.
        rows.append([{"text": OPEN}])
        return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}

    def pending(self, uid, value=None):
        key = 'chat_pending_' + str(uid)
        with self.engine.lock:
            if value is None:
                row = self.engine.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                return json.loads(row[0]) if row else {}
            self.engine.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value)))
            self.engine.db.commit()

    def open_message(self, room, admin=False):
        code = room['code']
        url = self.config.get('public_url', '')
        if url:
            url += '?' + urlencode({'room': code, 'panel': 'admin' if admin else 'player'})
        return {
            'text': f"{room['name']}\nКод комнаты: {code}\n\n" + ("Вы — администратор. Выберите задания и правила в мини-приложении.\nПередайте код остальным игрокам." if admin else "Вы в комнате. Ваша роль появится после начала игры."),
            'reply_markup': {'inline_keyboard': [[{'text': 'Настроить комнату' if admin else OPEN,
                                                  **({'web_app': {'url': url}} if url else {'callback_data': 'demo_open_' + code})}]]},
            'demo_room': code, 'demo_panel': 'admin' if admin else 'player',
        }

    def handle(self, uid, name, text):
        uid, text = str(uid), str(text).strip()
        name = str(name)[:60]
        self.engine.register(uid, name)
        command, _, args = text.partition(' ')
        command = command.split('@')[0].lower()
        current = self.engine.current_room(uid)
        reply = {'text': "Создайте комнату или войдите по коду. Настройки и вся игра откроются в мини-приложении.", 'reply_markup': self.keyboard()}
        try:
            if command == '/start' and re.fullmatch(r'[sbmx]_[A-Za-z0-9_-]{1,62}', args):
                self.pending(uid, {})
                require(current, 'Сначала войдите в комнату по коду в чате бота, затем отсканируйте QR ещё раз.')
                require(self.config.get('public_url'), 'Приложение ещё запускается. Попробуйте через несколько секунд.')
                labels = {'s': 'Открыть задание', 'b': 'Проверить бейдж', 'm': 'Открыть совещание', 'x': 'Открыть панель системы'}
                return [{'text': 'QR получен. Откройте действие в игре.', 'reply_markup': {
                    'inline_keyboard': [[{'text': labels[args[0]], 'web_app': {
                        'url': self.config['public_url'] + '?' + urlencode({'start': args})}}]]}}]
            if command in ('/cancel', '/start') and not args:
                self.pending(uid, {})
                return [reply] + ([self.open_message(current, uid in current['admins'])] if current else [])
            if command == '/whoami':
                return [{'text': f"Ваш Telegram ID: {uid}", 'reply_markup': self.keyboard()}]
            if text == CREATE or command == '/create':
                self.pending(uid, {})
                if current:
                    return [{'text': 'Вы уже в комнате. Выйти из лобби можно командой /leave.', 'reply_markup': self.keyboard()}, self.open_message(current, uid in current['admins'])]
                room = self.engine.create_room(uid, args if command == '/create' and args else f"Комната {name}"[:60])
                self.engine.join(room['code'], uid)
                return [{'text': 'Комната создана.', 'reply_markup': self.keyboard()}, self.open_message(room, True)]
            if text == JOIN or command == '/join' and not args:
                self.pending(uid, {'mode': 'join', 'until': self.engine.clock() + 600})
                return [{'text': 'Отправьте код комнаты из 6 символов. Например: A1B2C3.\nОтмена: /cancel', 'reply_markup': self.keyboard()}]
            start_join = command == '/start' and args.startswith('j_')
            pending = self.pending(uid)
            code = args[2:] if start_join else args if command == '/join' else text if pending.get('mode') == 'join' and pending.get('until', 0) >= self.engine.clock() else None
            if code is not None:
                require(re.fullmatch('[A-Fa-f0-9]{6}', code.strip()), 'Нужен код комнаты из 6 символов. Попробуйте ещё раз или отправьте /cancel.')
                room = self.engine.join(code.strip().upper(), uid)
                self.pending(uid, {})
                return [{'text': 'Вы присоединились.', 'reply_markup': self.keyboard()}, self.open_message(room, uid in room['admins'])]
            if command == '/leave':
                require(current, 'Вы ещё не в комнате.')
                self.engine.leave(current['code'], uid)
                self.pending(uid, {})
                return [{'text': 'Вы вышли из лобби.', 'reply_markup': self.keyboard()}]
            if command in ('/room', '/play') or text == OPEN:
                return [self.open_message(current, uid in current['admins'])] if current else [reply]
            return [reply] + ([self.open_message(current, uid in current['admins'])] if current else [])
        except GameError as exc:
            return [{'text': str(exc), 'reply_markup': self.keyboard()}]
