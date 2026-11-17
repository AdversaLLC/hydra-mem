"""Codex, generic-agent and optional vault-handoff regressions; temporary DBs only."""
import json,os,sqlite3,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import hydra_mem as hm

class CodexParsing(unittest.TestCase):
    def test_claude_and_codex_assistant_records_only(self):
        text='INSIGHT: synthetic compatibility observation [V]'
        entries=[{'type':'assistant','message':{'content':[{'type':'text','text':text}]}},
                 {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}},
                 {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'output_text','text':'NOT WISDOM'}]}},
                 {'type':'response_item','payload':{'type':'function_call','content':text}}]
        self.assertEqual(hm.assistant_texts(entries),[text,text])

    def test_direct_stop_payload_redacts_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as t:
            store=hm.Store(Path(t)/'mem.db')
            payload={'session_id':'codex-test','cwd':t,'last_assistant_message':'DECISION: synthetic password=example-secret-value is not retained [V]'}
            try:
                with patch.object(hm,'read_hook_payload',return_value=payload),patch.object(hm,'bridge_promote_async',return_value=False):
                    hm.hook_main('Stop',store);hm.hook_main('Stop',store)
                rows=store.db.execute("select text from observations where type='decision'").fetchall()
                self.assertEqual(len(rows),1);self.assertNotIn('example-secret-value',rows[0][0])
            finally:store.db.close()

    def test_graph_fallback_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as t:
            graph=Path(t)/'graph.py';graph.touch()
            with patch.object(hm,'_hydra_config',side_effect=lambda k:str(graph) if k=='graph_py' else ''),patch.object(hm,'BRIDGE_CANDIDATE_DIRS',[]):
                with patch.dict(os.environ,{'HYDRA_BRIDGE':'/missing/explicit.py'}):self.assertIsNone(hm.bridge_script())
                with patch.dict(os.environ,{},clear=True):self.assertEqual(hm.bridge_script(),graph)

class VaultHandoff(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=hm.Store(Path(self.temp.name)/'mem.db')
        self.args={'text':'One synthetic explicit observation for vault handoff','project':'test','type':'note'}
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_default_off_preserves_original_reply(self):
        with patch.dict(os.environ,{},clear=True),patch.object(hm,'_hydra_config',return_value=''),patch.object(hm,'bridge_promote_async') as queue:
            result=hm.mcp_call(self.store,'add',self.args)
        self.assertRegex(result,r'^stored id=\d+$');queue.assert_not_called()
    def test_enabled_save_then_queue_once(self):
        def queue(_, db=None):
            self.assertEqual(db,self.store.path)
            with sqlite3.connect(self.store.path) as independent:
                self.assertEqual(independent.execute("select count(*) from observations").fetchone()[0],1)
            return True
        with patch.dict(os.environ,{'HYDRA_SYNC_ON_ADD':'1'}),patch.object(hm,'bridge_promote_async',side_effect=queue) as queued:
            result=hm.mcp_call(self.store,'add',self.args)
            duplicate=hm.mcp_call(self.store,'add',self.args)
        self.assertIn('vault_sync=queued',result);self.assertEqual(duplicate,'duplicate; not stored');self.assertEqual(queued.call_count,1)
    def test_environment_false_overrides_true_config(self):
        with patch.dict(os.environ,{'HYDRA_SYNC_ON_ADD':'0'}),patch.object(hm,'_hydra_config',return_value='True'),patch.object(hm,'bridge_promote_async') as queue:
            self.assertEqual(hm.sync_after_add(),'')
        queue.assert_not_called()
    def test_config_can_enable_handoff(self):
        with patch.dict(os.environ,{},clear=True),patch.object(hm,'_hydra_config',return_value='True'),patch.object(hm,'bridge_promote_async',return_value=True):
            self.assertEqual(hm.sync_after_add(),'vault_sync=queued')
    def test_missing_bridge_does_not_lose_saved_row(self):
        with patch.dict(os.environ,{'HYDRA_SYNC_ON_ADD':'1'}),patch.object(hm,'bridge_promote_async',return_value=False):
            result=hm.mcp_call(self.store,'add',self.args)
        self.assertIn('vault_sync=unavailable',result)
        self.assertEqual(self.store.db.execute('select count(*) from observations').fetchone()[0],1)
    def test_selected_db_is_forwarded_to_child_and_owns_logs(self):
        with tempfile.TemporaryDirectory() as t:
            db=Path(t)/'selected.db'
            with patch.object(hm,'bridge_available',return_value=(sys.executable,Path(t)/'bridge.py','ok')),patch.object(hm.subprocess,'Popen') as spawn,patch.dict(os.environ,{'HYDRA_MEM_DB':'/wrong/database.db'}):
                self.assertTrue(hm.bridge_promote_async(t,db=db))
            args,kwargs=spawn.call_args
            self.assertEqual(kwargs['env']['HYDRA_MEM_DB'],str(db.resolve()))
            self.assertEqual(Path(args[0][-1]).parent,db.parent)
            self.assertEqual(Path(args[0][-2]).parent.parent,db.parent)

    def test_cli_db_override_reaches_actual_bridge_child(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);db=root/'selected.db';marker=root/'child.json';bridge=root/'bridge.py'
            bridge.write_text("import os,json,pathlib,sys\nif sys.argv[1]=='promote': pathlib.Path(os.environ['TEST_MARKER']).write_text(json.dumps({'db':os.environ.get('HYDRA_MEM_DB')}))\nelse: print('synthetic context')\n")
            env=dict(os.environ,HYDRA_MEM_DB=str(root/'wrong.db'),HYDRA_SYNC_ON_ADD='1',HYDRA_BRIDGE=str(bridge),HYDRA_BRIDGE_PYTHON=sys.executable,TEST_MARKER=str(marker))
            result=subprocess.run([sys.executable,str(Path(hm.__file__)),'--db',str(db),'add','Synthetic CLI-selected database propagation regression','--type','note'],env=env,cwd=root,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr);self.assertIn('vault_sync=queued',result.stdout)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if marker.exists() and not (root/'bridge-chain.lock').exists():break
                time.sleep(0.05)
            self.assertTrue(marker.exists());self.assertEqual(json.loads(marker.read_text())['db'],str(db))

    def test_queue_exception_does_not_turn_save_into_error(self):
        with patch.dict(os.environ,{'HYDRA_SYNC_ON_ADD':'1'}),patch.object(hm,'bridge_promote_async',side_effect=OSError('synthetic spawn failure')):
            result=hm.mcp_call(self.store,'add',self.args)
        self.assertIn('vault_sync=unavailable',result)
        self.assertEqual(self.store.db.execute('select count(*) from observations').fetchone()[0],1)

if __name__=='__main__':unittest.main(verbosity=2)
