import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import time

import pytest
from fastapi.testclient import TestClient

import agent
import budgets
import main
import publications
import settings
import tools
from purchasing import discovery
from purchasing.database import Database
from test_agent import chunk, scripted, tool_call_chunks
from test_publications import setup, render_stub, SOURCE
from test_recovery import recovery_app


def choice(cents):
    return budgets.normalize({'status': 'set', 'amount_cents': cents})


def quote(price=10500, quantity=1):
    return dict(id='quote', revision='a'*64, currency='USD', checked_at=time.time(), subtotal=price*quantity,
                lines=[dict(part='3001', color='4', name='Brick', unit_price=price, quantity=quantity, line_total=price*quantity)],
                fees=[dict(name='Separate service fee', amount=900)])


@pytest.mark.parametrize('amount', [0, -1, True, 1.23, '100', budgets.MAX_CENTS+1])
def test_invalid_amounts_are_rejected(amount):
    with pytest.raises(ValueError): choice(amount)


@pytest.mark.parametrize('amount,maximum', [(1,1), (19,19), (20,21), (9999,10498), (10000,10500)])
def test_integer_cent_limits(amount, maximum):
    assert choice(amount)['maximum_cents'] == maximum


def test_exact_limit_and_one_cent_over_exclude_fees():
    assert budgets.require(choice(10000), quote())['status'] == 'within_allowance'
    with pytest.raises(ValueError): budgets.require(choice(10000), quote(10501))
    assert budgets.require(choice(10000), quote(10000))['status'] == 'within_budget'
    assert budgets.require(None, quote(100000))['status'] == 'unbudgeted'


@pytest.mark.parametrize('field,value', [('unit_price',None), ('unit_price',-1), ('quantity',True), ('line_total',0)])
def test_incomplete_or_inconsistent_prices_cannot_pass(field,value):
    data=quote();data['lines'][0][field]=value
    assert budgets.assess(choice(10000),data)['subtotal'] is None
    with pytest.raises(ValueError): budgets.require(choice(10000),data)


def test_client_cannot_raise_the_allowance_or_change_currency():
    for key,value in [('maximum_cents',999999),('allowance_percent',100),('currency','EUR'),('basis','total')]:
        with pytest.raises(ValueError): budgets.normalize({**choice(100),key:value})


def test_budget_api_updates_skip_legacy_and_active_conflicts(recovery_app, monkeypatch):
    store,entry=recovery_app
    client=TestClient(main.app)
    chat=client.post('/api/chats',json={}).json();chat_id=chat['id']
    assert chat['budget']['status']=='pending'
    assert client.patch(f'/api/chats/{chat_id}',json={'budget':choice(10000)}).json()['budget']==choice(10000)
    assert client.patch(f'/api/chats/{chat_id}',json={'budget':None}).json()['budget']['status']=='skipped'
    run=agent.Run(chat_id,starting=True)
    agent._runs[chat_id]=run
    assert client.patch(f'/api/chats/{chat_id}',json={'budget':choice(100)}).status_code==409
    run.starting=False
    assert client.patch(f'/api/chats/{chat_id}',json={'budget':{'status':'set','amount_cents':1.25}}).status_code==400
    assert client.post('/api/chats',json={'budget':{'status':'set','amount_cents':False}}).status_code==400
    legacy=store.get_chat(chat_id);legacy.pop('budget');store._write_chat(legacy)
    assert store.get_chat(chat_id)['budget']['status']=='skipped'


def test_request_freezes_budget_and_recovery_preserves_it(recovery_app, monkeypatch):
    store,entry=recovery_app
    chat_id=store.create_chat(budget=choice(10000))['id']
    waiting=asyncio.Event()
    async def pending(**kwargs):
        waiting.set();await asyncio.Future()
    monkeypatch.setattr(agent.litellm,'acompletion',pending)
    async def check():
        async with main.lifespan(main.app):
            await agent.start_turn(store,chat_id,'Build it',entry['id'],{'permissions':'full'})
            await waiting.wait()
            assert next(m for m in store.messages(chat_id) if m['role']=='user')['_budget']==choice(10000)
            store.update_chat(chat_id,budget=choice(20000))  # Simulate a later setting; active run owns its snapshot.
            assert agent._runs[chat_id].budget==choice(10000)
        agent._runs.clear()
        fake,_=scripted([[chunk(content='Recovered.')]])
        monkeypatch.setattr(agent.litellm,'acompletion',fake)
        async with main.lifespan(main.app):
            assert agent._runs[chat_id].budget==choice(10000)
            await agent._runs[chat_id].task
            await agent.start_turn(store,chat_id,'Next version',entry['id'],{'mode':'plan'},budget=choice(30000))
            assert agent._runs[chat_id].budget==choice(30000)
            await agent.cancel(chat_id)
    asyncio.run(check())
    assert store.run_state(chat_id)['budget']==choice(30000)


def test_exhausted_runtime_keeps_budget_and_offers_continuation(recovery_app, monkeypatch):
    store,entry=recovery_app
    chat_id=store.create_chat(budget=choice(100))['id']
    fake,_=scripted([[*tool_call_chunks('progress','report_progress',{'summary':'Checking the budget'})]])
    monkeypatch.setattr(agent.litellm,'acompletion',fake)
    monkeypatch.setattr(agent,'MAX_STEPS',1)
    async def check():
        await agent.start_turn(store,chat_id,'Build it',entry['id'],{'permissions':'full'})
        await agent._runs[chat_id].task
    asyncio.run(check())
    assert store.run_state(chat_id)['status']=='limited'
    assert store.get_chat(chat_id)['budget']==choice(100)
    message=store.messages(chat_id)[-1]['content']
    assert '$1.05' in message and 'simpler design' in message and 'change the budget' in message
    assert not store.get_chat(chat_id).get('models')


def test_budgeted_service_failure_retains_draft_and_publishes_nothing(setup,monkeypatch):
    ctx,db,source,_=setup
    from purchasing import availability
    async def unavailable(*args,**kwargs):raise RuntimeError('private connection error')
    monkeypatch.setattr(availability,'check',unavailable)
    result=asyncio.run(tools.t_publish_model(replace(ctx,budget=choice(100)),'draft.mpd','Cannot verify'))
    assert not result.models and not db.records and source.read_bytes()==SOURCE
    assert 'private connection' not in result.content and 'saved' in result.content


def test_plan_keeps_unanswered_choice_and_old_clients_can_skip(recovery_app, monkeypatch):
    store,entry=recovery_app
    chat_id=store.create_chat()['id']
    fake,_=scripted([[chunk(content='A plan.')],[chunk(content='Build.')]])
    monkeypatch.setattr(agent.litellm,'acompletion',fake)
    async def check():
        await agent.start_turn(store,chat_id,'Plan it',entry['id'],{'mode':'plan'})
        await agent._runs[chat_id].task
        assert store.get_chat(chat_id)['budget']['status']=='pending'
        await agent.start_turn(store,chat_id,'Build it',entry['id'],{'permissions':'full'})
        await agent._runs[chat_id].task
    asyncio.run(check())
    assert store.get_chat(chat_id)['budget']['status']=='skipped'


def test_over_budget_blocks_before_render_and_repair_passes(setup,render_stub,monkeypatch):
    ctx,db,source,events=setup
    ctx=replace(ctx,budget=choice(19))  # 21-cent brick cannot fit the 19-cent maximum.
    calls=[]
    async def review(*args): calls.append(args[1][0]);return await render_stub(*args)
    monkeypatch.setattr(tools,'_trusted_review',review)
    result=asyncio.run(tools.t_publish_model(ctx,'draft.mpd','Budget model'))
    feedback=json.loads(result.content)
    assert not result.models and feedback['problems'][0]['issue']=='budget_exceeded'
    assert feedback['budget_assessment']['excess_cents']==2 and not calls
    assert not list(settings.GENERATED_DIR.iterdir())
    # An available cheaper colour repairs the draft without weakening the stock check.
    db.elements['300124']=replace(db.elements['300124'],unit_price=18)
    source.write_bytes(SOURCE.replace(b'1 4 ',b'1 14 '))
    result=asyncio.run(tools.t_publish_model(ctx,'draft.mpd','Budget model'))
    assessment=json.loads(result.content)['budget_assessment']
    assert result.models and assessment['subtotal']==18 and assessment['status']=='within_budget'
    assert calls==['validate','render']
    assert next(iter(db.records.values()))['budget_assessment']==assessment


def test_price_increase_after_render_blocks_publication(setup,render_stub,monkeypatch):
    ctx,db,source,events=setup
    ctx=replace(ctx,budget=choice(20))  # exact 21-cent allowance boundary before rendering
    async def review(*args):
        result=await render_stub(*args)
        if args[1][0]=='render':db.elements['300121']=replace(db.elements['300121'],unit_price=22)
        return result
    monkeypatch.setattr(tools,'_trusted_review',review)
    result=asyncio.run(tools.t_publish_model(ctx,'draft.mpd','Budget rise'))
    assert not result.models and not db.records and source.read_bytes()==SOURCE
    assert json.loads(result.content)['budget_assessment']['excess_cents']==1


def test_repeated_assemblies_use_full_cost(setup):
    ctx,db,source,_=setup
    ctx=replace(ctx,budget=choice(60))
    source.write_text('0 FILE main.ldr\n' + '\n'.join(f'1 4 {x*80} 0 0 1 0 0 0 1 0 0 0 1 sub.ldr' for x in range(4))
                      + '\n0 FILE sub.ldr\n1 16 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n')
    feedback=json.loads(asyncio.run(tools.t_check_build_parts(ctx,'draft.mpd')).content)
    assert feedback['budget_assessment']['subtotal']==84
    assert feedback['budget_assessment']['largest_costs'][0]['quantity']==4
    assert feedback['status']=='blocked'


def test_identical_revision_has_separate_immutable_budget_evidence(setup,render_stub):
    ctx,db,source,_=setup
    first=asyncio.run(tools.t_publish_model(replace(ctx,budget=choice(20)),'draft.mpd','Same bytes'))
    first_record=deepcopy(next(iter(db.records.values())))
    second=asyncio.run(tools.t_publish_model(replace(ctx,budget=choice(100)),'draft.mpd','Same bytes'))
    records=list(db.records.values())
    assert first.models and second.models and records[0]==first_record
    assert records[0]['revision']==records[1]['revision']
    assert records[0]['budget_assessment']['status']=='within_allowance'
    assert records[1]['budget_assessment']['status']=='within_budget'
    assert all('budget' not in q and 'budget_assessment' not in q for q in db.quotes.values())
    path=ctx.store.resolve(ctx.chat_id,first.models[0]['model'])
    assert main.model_info(path)['budget_assessment']==first_record['budget_assessment']


def test_commit_and_database_recheck_budget(setup):
    ctx,db,source,_=setup
    from purchasing import availability
    async def check():
        snapshot=publications.capture(source.read_bytes())
        after=time.time()
        q,_=await availability.check(snapshot,refresh=True,final=True)
        with pytest.raises(ValueError,match='budget'):
            await publications.commit(snapshot,'Cannot bypass',q,after=after,budget=choice(19))
        record={'revision':q['revision'],'policy_version':2,'budget_assessment':budgets.assess(choice(19),q)}
        with pytest.raises(ValueError,match='budget'):Database().save_publication(record,q,after)
    asyncio.run(check())
    assert not db.records


def test_discovery_filters_price_before_both_rankers(setup,monkeypatch):
    ctx,db,source,_=setup
    db.elements['300124']=replace(db.elements['300124'],unit_price=99)
    observed=[]
    original_rank=discovery.rank
    def ranking(groups,*args,**kwargs):
        observed.extend(c['unit_price'] for g in groups.values() for c in g['choices'])
        return list(groups), 'typesafe', None
    monkeypatch.setattr(discovery,'rank',ranking)
    result=discovery.search('brick',quantity=2,max_unit_price=21)
    assert observed==[21] and result['results'][0]['choices'][0]['estimated_total']==42
    monkeypatch.setattr(discovery,'rank',original_rank)
    monkeypatch.setattr(discovery.environment_config,'snapshot',lambda: {})
    fallback=discovery.search('brick',max_unit_price=21)
    assert fallback['engine']=='keyword'
    assert [c['unit_price'] for g in fallback['results'] for c in g['choices']]==[21]
