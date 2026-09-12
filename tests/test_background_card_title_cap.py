import ast, logging, time, types, uuid
from pathlib import Path
import pytest
ROOT=Path(__file__).parents[1]
@pytest.fixture
def background_functions():
 source=Path(__import__('os').environ.get('BG_CARD_SOURCE','/home/ops/hermes-webui-src')) / 'api/background_process.py'
 tree=ast.parse(source.read_text())
 wanted={'_build_payload','_truncate'}
 ns={'time':time,'uuid':uuid,'Any':object,'logger':logging.getLogger(__name__),'completion_delivery_id':lambda e:e.get('delegation_id') or e.get('session_id'),'format_wakeup_prompt':lambda e:'','process_id':None}
 exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in wanted],type_ignores=[]),str(source),'exec'),ns)
 return ns
@pytest.mark.parametrize('length',[200,201])
def test_title_boundary_uses_real_title_path(background_functions,length):
 result=background_functions['_build_payload']({'type':'async_delegation','delegation_id':'boundary','goal':'x'*length,'status':'completed'},'test')
 assert len(result['title'])==min(length,200)
 assert '\n' not in result['title']
