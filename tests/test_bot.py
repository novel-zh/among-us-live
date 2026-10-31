import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from test_api import server, signed
from bot import ChatBot, CREATE, JOIN, OPEN


def test_chat_create_owner_join_and_real_webapp_buttons(server):
    b=ChatBot(server.engine, {'public_url':'https://game.example.com'})
    start=b.process('101','Алекс','/start')
    assert start[0]['reply_markup']['keyboard'][0] == [{'text':CREATE},{'text':JOIN}]
    assert start[0]['reply_markup']['keyboard'][1] == [{'text':OPEN}]
    messages=b.process('101','Алекс',CREATE,update_id=1)
    room=server.engine.current_room('101')
    assert room['name']=='Комната Алекс'
    assert room['owner']=='101' and '101' in room['admins'] and '101' in room['players']
    button=messages[-1]['reply_markup']['inline_keyboard'][0][0]
    assert button['web_app']['url']==f"https://game.example.com?room={room['code']}&panel=admin"
    b.process('202','Саша',JOIN)
    joined=b.process('202','Саша',room['code'])
    assert server.engine.current_room('202')['code']==room['code']
    assert joined[-1]['demo_panel']=='player'
    assert b.process('101','Алекс',CREATE,update_id=1)==messages
    assert len(server.engine.state['rooms'])==1


def test_printed_qr_opens_current_tunnel_without_botfather_setup_or_automatic_action(server):
    s=server
    s.cfg.update(mode='telegram',public_url='https://first.trycloudflare.com',bot_username='our_bot')
    s.chat_bot.process('101','Алекс','/create')
    room=s.engine.current_room('101')
    initial=s.deep_link('b_abcdef123456')
    s.cfg['public_url']='https://second.trycloudflare.com'
    assert s.deep_link('b_abcdef123456')==initial
    for payload in ('s_abcdef123456','b_abcdef123456','m_abcdef123456','x_abcdef123456'):
        msg=s.chat_bot.process('101','Алекс','/start '+payload)[0]
        assert msg['reply_markup']['inline_keyboard'][0][0]['web_app']['url']=='https://second.trycloudflare.com?start='+payload
    assert s.engine.room(room['code'])['phase']=='lobby'
    assert 'Сначала войдите' in s.chat_bot.process('999','Новый','/start s_abcdef123456')[0]['text']


def test_chat_commands_invites_pending_persistence_and_cancel(server):
    b=server.chat_bot
    b.process('1','Алекс','/create Вечерняя игра')
    room=server.engine.current_room('1')
    assert room['name']=='Вечерняя игра'
    b.process('2','Саша','/join')
    assert '6 символов' in b.process('2','Саша','wrong')[0]['text']
    restored=ChatBot(server.engine,server.cfg)
    restored.process('2','Саша',room['code'])
    assert '2' in server.engine.room(room['code'])['players']
    b.process('3','Дана','/start j_'+room['code'])
    assert '3' in server.engine.room(room['code'])['players']
    b.process('2','Саша','/leave')
    assert not server.engine.current_room('2')
    b.process('2','Саша',JOIN)
    b.process('2','Саша','/cancel')
    b.process('2','Саша',room['code'])
    assert not server.engine.current_room('2')
    assert 'Telegram ID: 2' in b.process('2','Саша','/whoami')[0]['text']


def test_chat_refuses_duplicate_rooms_and_late_joins(server):
    b=server.chat_bot
    b.process('1','Алекс',CREATE)
    b.process('1','Алекс','/create Ещё')
    assert len(server.engine.state['rooms'])==1
    room=server.engine.current_room('1')
    for uid in ('2','3'):
        b.process(uid,'Игрок '+uid,'/join '+room['code'])
    station=server.engine.station('Провода','wires')['code']
    server.engine.configure(room['code'],'1',{'stations':[station],'tasks':1})
    server.engine.start(room['code'],'1')
    assert 'до начала' in b.process('4','Новый','/join '+room['code'])[0]['text']
    assert '4' not in server.engine.room(room['code'])['players']


def test_telegram_mode_cannot_create_or_join_in_mini_app(server):
    s=server
    s.cfg.update(mode='telegram',bot_token='12345:fake-test-token',admin_ids=['101'])
    c=TestClient(s.app)
    auth=c.post('/api/auth/telegram',json={'initData':signed(s.cfg['bot_token'],uid=101)}).json()
    headers={'Authorization':'Bearer '+auth['token']}
    assert c.post('/api/rooms',json={'name':'Через API'},headers=headers).status_code==400
    assert c.post('/api/join',json={'code':'ABC123'},headers=headers).status_code==400
    assert c.post('/api/demo/chat',json={'text':'/create'},headers=headers).status_code==400
    assert not s.engine.state['rooms']
    s.chat_bot.process('101','Алекс','/create В чате')
    room=s.engine.current_room('101')
    assert c.get('/api/rooms/'+room['code'],headers=headers).json()['admin']
    c.close()


def test_normal_chat_creator_can_manage_room_without_server_owner(server):
    s=server
    s.cfg.update(mode='telegram',bot_token='12345:fake-test-token',admin_ids=[])
    s.chat_bot.process('123','Создатель',CREATE)
    room=s.engine.current_room('123')
    c=TestClient(s.app)
    token=c.post('/api/auth/telegram',json={'initData':signed(s.cfg['bot_token'])}).json()['token']
    headers={'Authorization':'Bearer '+token}
    assert c.get('/api/me',headers=headers).json()['host'] is False
    station=c.post('/api/stations',headers=headers,json={'name':'Карта','kind':'card_swipe'})
    assert station.status_code==200
    assert c.post('/api/rooms/'+room['code']+'/configure',headers=headers,json={'stations':[station.json()['code']],'tasks':1}).status_code==200
    c.close()


def test_bot_poll_delivers_chat_messages_with_actual_telegram_payloads(server, monkeypatch):
    s=server;s.cfg.update(mode='telegram',bot_token='fake',public_url='https://game.example.com')
    calls=[];fetched=0
    async def fake_tg(client,method,data):
        nonlocal fetched
        calls.append((method,data))
        if method=='getMe':return {'username':'my_test_bot'}
        if method=='getUpdates':
            fetched+=1
            if fetched>1:raise asyncio.CancelledError()
            return [{'update_id':50,'message':{'chat':{'type':'private'},'from':{'id':101,'first_name':'Алекс'},'text':CREATE}}]
        return True
    monkeypatch.setattr(s,'tg',fake_tg)
    with pytest.raises(asyncio.CancelledError):asyncio.run(s.bot_loop())
    sent=[d for m,d in calls if m=='sendMessage']
    assert len(sent)==2 and all(d['chat_id']=='101' for d in sent)
    assert all(not any(k.startswith('demo_') for k in d) for d in sent)
    assert sent[1]['reply_markup']['inline_keyboard'][0][0]['web_app']['url'].startswith('https://game.example.com?room=')
