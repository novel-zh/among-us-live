"""Host-controlled test participants. Never impersonate real Telegram users."""
import secrets
from sabotage import route_solution
from game_errors import GameError, require


class TestPlayersMixin:
    def add_bots(self, code, uid, count=1, host=False):
        def change():
            r = self.room(code); self.admin(r, uid, host)
            require(r['phase'] == 'lobby', 'Добавляйте тестовых игроков до начала партии')
            require(type(count) is int and 1 <= count <= 10, 'Можно добавить от 1 до 10 ботов за раз')
            require(len(r['players']) + count <= 40, 'В комнате максимум 40 участников')
            r['test_mode'] = True
            serial = r.get('bot_serial', 0)
            for _ in range(count):
                serial += 1; pid = 'bot_' + r['code'] + '_' + secrets.token_hex(4)
                name = 'Бот ' + str(serial)
                self.state['users'][pid] = {'id': pid, 'name': name, 'badge': secrets.token_urlsafe(12)}
                r['players'][pid] = {'id': pid, 'name': name, 'role': None, 'status': 'alive', 'known_dead': False,
                                     'reported': False, 'tasks': {}, 'kill_ready': 0, 'emergencies': 0,
                                     'ghost_ready': 0, 'death_id': None, 'bot': True, 'auto': True}
            r['bot_serial'] = serial
        return self._change(change, str(code).upper(), uid, 'Хост добавил тестовых игроков')

    def test_bot(self, code, uid, player, host=False):
        r = self.room(code); self.admin(r, uid, host)
        require(r.get('test_mode'), 'Это не тестовая сессия')
        p = self.member(r, player)
        require(p.get('bot'), 'Эта команда доступна только тестовым ботам')
        return r, p

    def bot_manual(self, code, uid, player, host=False):
        def change():
            _, p = self.test_bot(code, uid, player, host); p['auto'] = False
        return self._change(change, str(code).upper(), uid, 'Хост взял управление ботом')

    def bot_action(self, code, uid, player, action, host=False):
        with self.lock:
            r, p = self.test_bot(code, uid, player, host)
            if action == 'repair':
                self.active(r); require(p['status'] == 'alive', 'Погибший бот не ремонтирует саботажи')
                s = r.get('sabotage'); require(s, 'Сейчас нет саботажа')
                if s['kind'] == 'oxygen': panel = next(k for k in ('oxygen_a', 'oxygen_b') if k not in s['fixed'])
                elif s['kind'] == 'reactor':
                    panel = next((k for k in ('reactor_a', 'reactor_b') if self.repair_free(r, player, k)), None)
                    require(panel, 'Оба сенсора уже заняты')
                else: panel = 'comms'
                grant = self.sabotage_open(code, player, self.state['sabotage_codes'][panel])
                if s['kind'] == 'oxygen': return self.sabotage_fix(code, player, grant | {'code': grant['puzzle']['code']})
                if s['kind'] == 'comms': return self.sabotage_fix(code, player, grant | {'directions': route_solution(grant['puzzle'])})
                def help_reactor():
                    self.member(self.room(code), player)['bot_repair'] = {**grant, 'until': self.clock() + 15}
                self._change(help_reactor, str(code).upper(), uid, 'Тест: бот удерживает сенсор 15 секунд')
                return self.reactor_hold(code, player, grant | {'pressed': True})
            def change():
                r, p = self.test_bot(code, uid, player, host)
                if action == 'remove':
                    require(r['phase'] == 'lobby', 'Удалять ботов можно только в лобби'); del r['players'][str(player)]
                elif action == 'auto': p['auto'] = not p.get('auto', True)
                elif action == 'task':
                    self.active(r); require(p['role'] == 'crew', 'Предатель не выполняет задания, в том числе в тесте')
                    require(p['status'] != 'dead' or self.clock() >= p.get('ghost_ready', 0), 'У бота ещё идёт перезарядка заданий')
                    available = [s for s, t in p['tasks'].items() if not t['done'] and
                                 (not r.get('sabotage') or self.state['stations'][s]['kind'] not in self.sabotage_view(r, str(player))['blocked'])]
                    require(available, 'Нет доступных невыполненных заданий')
                    t = p['tasks'][available[0]]
                    t.update(done=True, stage=t.get('stage_count', 1), id=secrets.token_urlsafe(12), started=None)
                    if p['status'] == 'dead': p['ghost_ready'] = self.clock() + 60
                    self._win(r)
                else: raise GameError('Неизвестная тестовая команда')
            return self._change(change, str(code).upper(), uid, 'Тест: команда боту — ' + str(action))

    def tick_bots(self, code):
        r = self.room(code)
        if not r.get('test_mode') or r['paused_at'] is not None or r['phase'] in ('lobby', 'ended'): return
        for pid in list(r['players']):
            r = self.room(code); p = r['players'].get(pid)
            if not p or not p.get('bot') or p['status'] != 'alive' or r['phase'] == 'ended': continue
            grant = p.get('bot_repair')
            if grant and r['phase'] == 'running' and r.get('sabotage') and r['sabotage']['id'] == grant['id'] and grant['until'] > self.clock():
                try: self.reactor_hold(code, pid, grant | {'pressed': True})
                except GameError: pass
            if not p.get('auto', True): continue
            m = r.get('meeting')
            if m and self.clock() >= m.get('started', self.clock()) + 2:
                if r['phase'] == 'gathering' and pid not in m['arrived']: self.meeting_join(code, pid)
                elif r['phase'] == 'voting' and pid not in m['votes']: self.vote(code, pid, m['id'], 'skip')
