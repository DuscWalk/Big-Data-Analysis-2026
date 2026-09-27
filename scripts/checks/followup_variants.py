"""Opt-in live variants and sample dialogue against one immutable published task."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import time
from uuid import uuid4

from movielens_agent.agent.service import AgentService
from movielens_agent.agent.settings import Settings
from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.storage.catalog import DatasetCatalog
from movielens_agent.storage.conversations import ConversationStore
from movielens_agent.storage.tasks import TaskStore
from movielens_agent.workflows.governance import implementation_digest

# Expectations are independent of the requirement parser under test.
CASES = [
    ('eleven', '请读取十一条 ratings/R14_VALID_KEY_CONFLICT 的异常样例。', [('examples','ratings/R14_VALID_KEY_CONFLICT',11,0)], None),
    ('ordinal', '请读取第十一条 ratings/R14_VALID_KEY_CONFLICT 的异常样例。', [('examples','ratings/R14_VALID_KEY_CONFLICT',1,10)], None),
    ('mixed', '分别给两个 ratings/R14_VALID_KEY_CONFLICT 样例和一个 users/R15_DUPLICATE 样例。', [('examples','ratings/R14_VALID_KEY_CONFLICT',2,0),('examples','users/R15_DUPLICATE',1,0)], None),
    ('ambiguous', '再给一个。', 'clarification', 'mixed'),
    ('scoped_next', '再给一个评分样例。', [('examples','ratings/R14_VALID_KEY_CONFLICT',1,2)], 'mixed'),
    ('real_sample', '给一个真实的评分冲突样例，不要用户样例，也不要完整报告。', [('examples','ratings/R14_VALID_KEY_CONFLICT',1,0)], None),
    ('next', '换一个例子。', [('examples','ratings/R14_VALID_KEY_CONFLICT',1,1)], 'real_sample'),
    ('more', '再给两条。', [('examples','ratings/R14_VALID_KEY_CONFLICT',2,2)], 'next'),
    ('files', '给两条 users.jsonl 清洗样例和第十一条 movies.jsonl 清洗样例。', [('sample','users.jsonl',2,0),('sample','movies.jsonl',1,10)], None),
    ('english', 'Give eleven examples from ratings/R14_VALID_KEY_CONFLICT.', [('examples','ratings/R14_VALID_KEY_CONFLICT',11,0)], None),
    ('quantity_ambiguity', '给两条评分和用户样例。', 'clarification', None),
    ('brief1', '请依据本任务证据解释：为什么清洗后四项质量达到 100 分，时效性却仍然很低？这些分数能否证明数据真实？请简短回答，不要重新清洗。', 'brief', None),
    ('brief2', '四项满分能证明数据真实吗？为什么时效性低？请简要说明，不要给样例。', 'brief', None),
    ('brief3', '请简短说明：为何四项得分提高而时效性依然低？能否保证下一轮分类准确率？', 'brief', None),
]


def run(args):
    out=args.output
    out.mkdir(parents=True,exist_ok=True)
    if (out/'result.json').exists(): raise ValueError('Use a new output directory.')
    settings=Settings.load(catalog=args.catalog)
    if settings.model_provider!='backup': raise ValueError('This check requires MODEL_PROVIDER=backup.')
    chats=ConversationStore(args.catalog); chats.initialize(); chats.recover_interrupted_messages()
    tasks=TaskStore(args.catalog); task=tasks.get(args.task_id,args.session_id)
    if task['status']!='succeeded': raise ValueError('Select a published task.')
    ref=next(a['ref'] for a in task['artifacts'] if a['kind']=='quality_report')
    agent=AgentService(settings,chats,tasks,DatasetCatalog(args.catalog),GovernanceConfig.read(settings.governance_config))
    with tasks.connect() as conn: original_count=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    started=time.monotonic(); run_id=uuid4().hex
    result={'started_at':datetime.now(timezone.utc).isoformat(),'run_id':run_id,'implementation_sha256':implementation_digest(),
            'session_id':args.session_id,'task_id':args.task_id,'quality_ref':ref,'cases':[]}
    completed={}
    for name,question,expected,dependency in CASES:
        if args.case and name not in args.case:continue
        if dependency and (dependency not in completed or not completed[dependency]['passed']):
            case={'name':name,'status':'skipped_dependency','dependency':dependency,'passed':False}
        else:
            before=time.monotonic()
            reply=agent.respond(args.session_id,'variant:'+run_id+':'+name,question,args.task_id)
            calls=chats.calls(args.session_id,reply['message_id']); models=chats.model_calls(args.session_id,reply['message_id'])
            v=reply.get('validation') or {}; sections=v.get('sections',[])
            attempts=[a for m in reply.get('model_calls',[]) for a in m.get('attempts',[])]
            checks={'completed':reply['status']=='completed'}
            if expected=='clarification':
                checks.update(clarified=reply['response_origin']=='application_clarification',no_model_calls=not models,no_tool_calls=not calls)
            else:
                checks.update(rendered=reply['response_origin']=='evidence_rendered',exact_report=v.get('quality_ref')==ref,
                              fixed_model=bool(attempts) and all(a['provider']=='backup' and a['model']==settings.backup_name for a in attempts))
                if expected=='brief':
                    checks.update(scope=set(sections)=={'scores','metric_method','freshness','limitations'},characters=len(reply['content'])<=800,unsupported=v.get('unsupported') is True)
                else:
                    actual=[c for c in calls if c['request_key'].startswith('evidence:') and c['arguments'].get('mode') in ('examples','sample')]
                    queries=[(c['arguments']['mode'],c['arguments'].get('reason') or c['arguments'].get('file_name'),c['arguments']['limit'],c['arguments']['offset']) for c in actual]
                    checks['queries_match']=queries==expected
                    checks['sample_coverage']=len(v.get('sample_checks',[]))==len(expected) and len(sections)==len(expected) and all(k.startswith(('examples:','sample:')) for k in sections)
                    checks['current_source_rows']=bool(actual) and all(c['status']=='completed' and all(
                        'source_ref' in item and (c['arguments']['mode']!='examples' or item['raw_preview'] in reply['content'])
                        for item in c['result']['data']['value']['items']) for c in actual)
                    if any(c['status']=='completed' and not c['result']['data']['value']['items'] for c in actual): checks['empty_is_unsupported']=v.get('unsupported') is True
                    if dependency:checks['cursor_source']=v.get('requirements',{}).get('continuation_of')==completed[dependency]['response']['message_id']
            with tasks.connect() as conn:checks['no_new_tasks']=conn.execute('SELECT count(*) FROM tasks').fetchone()[0]==original_count
            usages=[(m.get('response') or {}).get('usage') or {} for m in models]
            case={'name':name,'question':question,'status':reply['status'],'passed':all(checks.values()),'checks':checks,
                  'duration_seconds':round(time.monotonic()-before,3),'response':reply,'tool_calls':calls,'model_calls':models,
                  'metrics':{'model_rounds':len(models),'answer_characters':len(reply['content']),'corrections':len(v.get('rejected_attempts',[])),
                             'prompt_tokens':sum(u.get('prompt_tokens',0) for u in usages)}}
            if not checks['no_new_tasks']:raise RuntimeError('Unexpected task submission.')
        completed[name]=case; result['cases'].append(case)
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({k:case[k] for k in ['name','status','passed','checks','metrics'] if k in case},ensure_ascii=False),flush=True)
    result.update(duration_seconds=round(time.monotonic()-started,3),passed=all(c['passed'] for c in result['cases']))
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result['passed']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-id',required=True); parser.add_argument('--task-id',required=True)
    parser.add_argument('--catalog',type=Path,default=Path('var/catalog.sqlite3'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--case',action='append',choices=[c[0] for c in CASES])
    args=parser.parse_args()
    with Path(str(args.catalog.resolve())+'.api.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:parser.error('Stop the API and other standalone checks first.')
        if not run(args):raise SystemExit('Some variants failed or were blocked by a prerequisite; records preserved.')


if __name__=='__main__':main()
