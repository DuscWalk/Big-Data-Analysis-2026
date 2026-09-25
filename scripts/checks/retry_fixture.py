"""Isolated browser fixture. All failures and answers are test doubles, not live service evidence."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
from agent_fixture import AgentFixture, ScriptedModel
from test_followup_dialogue import publish_samples, select_prepared
from movielens_agent.agent.model import ModelError
from movielens_agent.api.app import create_app
import uvicorn

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,required=True)
parser.add_argument('--port',type=int,default=8766)
args=parser.parse_args(); args.root.mkdir(parents=True,exist_ok=False)
f=AgentFixture(args.root.resolve())
first,_=publish_samples(f,'first'); other,_=publish_samples(f,'other')
question='请读取这次任务的实际质量摘要，解释五维前后变化、数据处置损失、时间边界与仍无法核验的问题。'
for task in [first,other]:
    f.agent([select_prepared]).respond(f.session,'explain:'+task,question,task,require_quality=True)
f.agent([select_prepared]).respond(f.session,'sample','给一个评分重复样例',first)
failed=f.agent([ModelError('MODEL_HTTP_ERROR','测试替身 HTTP 504：用于重试交互验证。')]).respond(f.session,'outage','再给一个',first)
metadata={'test_double':True,'session_id':f.session,'task_id':first,'other_task_id':other,'message_id':failed['message_id'],'base_url':f'http://127.0.0.1:{args.port}'}
(args.root/'fixture.json').write_text(json.dumps(metadata,indent=2)+'\n')
print(json.dumps(metadata),flush=True)
model=ScriptedModel(f.settings,[ModelError('MODEL_HTTP_ERROR','测试替身 HTTP 504：第一次重试仍失败。'),select_prepared])
uvicorn.run(create_app(f.settings,model),host='127.0.0.1',port=args.port,log_level='warning')
