import math

import pytest

from engine import GameError
from test_engine import game, roles
from puzzle_helpers import answer_for


def swipe(duration=700,y=0):
    return {'samples':[{'x':i/20,'y':y,'t':i*duration/20} for i in range(21)]}


def test_swipe_speed_and_full_path(game):
    game._validate_swipe(swipe(),{'min_ms':450,'max_ms':1100},2)
    with pytest.raises(GameError,match='быстро'):game._validate_swipe(swipe(140),{'min_ms':450,'max_ms':1100},2)
    with pytest.raises(GameError,match='медленно'):game._validate_swipe(swipe(2400),{'min_ms':450,'max_ms':1100},3)
    with pytest.raises(GameError,match='направляющей'):game._validate_swipe(swipe(y=.8),{'min_ms':450,'max_ms':1100},2)
    bad=swipe();bad['samples'][-1]['x']=.6
    with pytest.raises(GameError):game._validate_swipe(bad,{'min_ms':450,'max_ms':1100},2)
    bad=swipe();bad['samples'][10]['x']=.01
    with pytest.raises(GameError):game._validate_swipe(bad,{'min_ms':450,'max_ms':1100},2)
    with pytest.raises(GameError):game._validate_swipe({'complete':True},{'min_ms':450,'max_ms':1100},2)


def test_fast_swipe_then_hold_does_not_fake_correct_speed(game):
    answer=swipe(120)
    answer['samples'].append({'x':1,'y':0,'t':800})
    with pytest.raises(GameError,match='быстро'):game._validate_swipe(answer,{'min_ms':450,'max_ms':1100},2)


def test_swipe_rejects_invalid_data(game):
    for v in (float('nan'),float('inf'),True,'100'):
        a=swipe();a['samples'][4]['t']=v
        with pytest.raises(GameError):game._validate_swipe(a,{'min_ms':450,'max_ms':1100},2)


def test_calibration_requires_all_three_aligned_contacts(game):
    puzzle={'rotors':[{'phase':180,'speed':90}]*3,'tolerance':24}
    game._validate_calibration({'hits':[2,6,10]},puzzle,10.1)
    for a in ({'hits':[1,6,10]},{'hits':[2,6]},{'hits':[2,6,10]},{'hits':[2,2,2]}):
        with pytest.raises(GameError):game._validate_calibration(a,puzzle,4 if len(a['hits'])==3 and a['hits'][-1]==10 else 10.1)


def test_card_and_calibration_complete_real_assigned_tasks(game):
    g=game;_,crew=roles(g);uid=crew[0]['id']
    for kind in ('card_swipe','calibration'):
        station=g.station(kind,kind)['code']
        def assign():g.room(g.code)['players'][uid]['tasks'][station]={'id':'gesture-'+kind,'done':False,'puzzle':g._puzzle(kind),'started':None}
        g._change(assign)
        opened=g.task_open(g.code,uid,station)
        answer=answer_for(g,opened)
        g.task_finish(g.code,uid,station,opened['id'],opened['epoch'],answer)
        assert g.room(g.code)['players'][uid]['tasks'][station]['done']


def test_legacy_impostor_assignments_are_still_forbidden(game):
    g=game;imp,crew=roles(g);station=next(iter(crew[0]['tasks']))
    import copy
    def legacy():g.room(g.code)['players'][imp['id']]['tasks']=copy.deepcopy(crew[0]['tasks'])
    g._change(legacy)
    assert not g.view(g.code,imp['id'])['me']['tasks']
    with pytest.raises(GameError,match='Предатели'):g.task_open(g.code,imp['id'],station)
    with pytest.raises(GameError,match='Предатели'):g.inspect_scan(g.code,imp['id'],'s_'+station)
