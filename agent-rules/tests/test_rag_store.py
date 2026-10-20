"""Shared multilingual evidence metadata and all-phase capture; synthetic services only."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from ci.contracts import read, write
from ci.rag_store import capture_searches, document_record, query_records
from ci.runtime import OpenCode


class RagStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_greek_search_keeps_observed_urls_original_text_task_and_unknown_source_verification(self):
        url = 'https://example.invalid/τεκμηρίωση'
        output = json.dumps({'query':'https://invented.invalid/query-echo',
            'results':[{'url':url,'title':'Τεκμηρίωση','content':'Αρχικό ελληνικό κείμενο'}]},ensure_ascii=False)
        part = {'type':'tool','tool':'searxng_search','state':{'status':'completed',
            'input':{'query':'συνθετική αναζήτηση','language':'el','Authorization':'must not persist'},'output':output}}
        context = {'runId':'synthetic-run','phase':'refine-A-ai_prompts','task':'ai_prompts',
                   'callId':'refine-ai_prompts-00A4-1-author','modelTag':'synthetic-model'}
        self.assertEqual(capture_searches(self.root,[{'parts':[part]}],context,'2026-10-15T12:00:00Z'),1)
        values = list(query_records(self.root,language_filter='el',scope='A',phase='ai_prompts'))
        self.assertEqual(len(values),1)
        value = values[0]
        self.assertEqual(value['foundIn']['repoId'],'00A4')
        self.assertEqual(value['filters']['status'],'search-result-unverified')
        self.assertIn('Αρχικό ελληνικό κείμενο',value['originalText'])
        self.assertNotIn('Authorization',value['request'])
        self.assertEqual(len(value['resultUrls']),1)
        self.assertNotIn('query-echo',value['resultUrls'][0])
        self.assertEqual(list(query_records(self.root,language_filter='fr')),[])
        self.assertEqual(list(query_records(self.root,status='passage-checked')),[])

    def test_french_document_metadata_can_be_filtered_by_host_publisher_scope_date_and_content(self):
        text='Extrait synthétique original pour les sauvegardes et la restauration.'
        record={'schemaVersion':1,'cachedAt':'2026-10-15T10:00:00Z','originRunId':'origin',
            'files':{},'source':{'id':'2-source-1','url':'https://example.invalid/fr/backup',
                'language':'fr','title':'Sauvegardes','publisher':'Éditeur synthétique',
                'retrievedAt':'2026-10-15T10:00:00Z','sourceStatus':'fetched-awaiting-passage-check'}}
        value=document_record(record,text,{'runId':'origin','phase':'prepare'})
        write(self.root/'documents'/('a'*64+'.json'),value)
        matches=list(query_records(self.root,language_filter='fr',scope='2',publisher='ÉDITEUR',
            host='example.invalid',since='2026-10-15',search='restauration'))
        self.assertEqual(matches,[value])
        self.assertEqual(matches[0]['originalText'],text)
        self.assertEqual(list(query_records(self.root,since='2026-10-16')),[])

    def test_failed_model_call_still_saves_redacted_search_results_once(self):
        server=OpenCode(self.root,ROOT,{},self.root/'runtime',{'digest':'a'*64,'effectiveContext':32768})
        server.sensitive.append('synthetic-secret')
        server.rag_capture={'outbox':self.root/'data','context':{'runId':'synthetic',
            'phase':'refine-0-canonical_solutions','task':'canonical_solutions'}}
        messages=[]
        def api(path,data=None,**kwargs):
            if path=='/session':return {'id':'synthetic'}
            if path=='/session/status':return {}
            if path.endswith('/prompt_async'):
                messages.append({'info':{'role':'assistant','providerID':'ollama','modelID':'qwen3:235b',
                    'time':{'completed':1},'finish':'stop','tokens':{'total':10}},'parts':[
                    {'type':'tool','tool':'websearch_search','state':{'status':'completed',
                     'input':{'query':'synthetic query'},'output':'https://example.invalid/docs synthetic-secret'}},
                    {'type':'text','text':'{"duplicate":1,"duplicate":2}'}]})
            if path.endswith('/message'):return messages
            return None
        server.api,server.check_residency=api,lambda:None
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(ValueError,'Duplicate output key'):
            server.ask('sf-completer','Synthetic failed response',30,'refine-canonical_solutions-0004-1-author')
        records=list(query_records(self.root/'data',phase='canonical_solutions',scope='0'))
        self.assertEqual(len(records),1)
        self.assertNotIn('synthetic-secret',json.dumps(records))
        self.assertIn('[REDACTED]',records[0]['originalText'])

    def test_tool_errors_are_retained_without_promoting_input_urls_or_inventing_languages(self):
        part={'type':'tool','tool':'websearch_search','state':{'status':'error',
            'input':{'query':'https://example.invalid/query-only'},'error':'Synthetic rate limit'}}
        capture_searches(self.root,[{'parts':[part]}],{'phase':'prepare','callId':'sources-F-1-discovery'},
                         '2026-10-15T12:00:00Z')
        record=next(query_records(self.root,scope='F'))
        self.assertEqual(record['resultUrls'],[])
        self.assertEqual(record['filters']['languages'],['und'])
        self.assertEqual(record['executionStatus'],'error')
        self.assertEqual(record['error'],'Synthetic rate limit')

    def test_github_repository_searches_are_captured_and_filterable_by_result_host(self):
        part={'type':'tool','tool':'github_search_repositories','state':{'status':'completed',
            'input':{'query':'synthetic backup tools'},
            'output':json.dumps({'items':[{'html_url':'https://github.com/example/synthetic-backup'}]})}}
        capture_searches(self.root,[{'parts':[part]}],{'phase':'refine-C-canonical_solutions'},'2026-10-15T12:00:00Z')
        records=list(query_records(self.root,scope='C',host='github.com'))
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['toolId'],'github_search_repositories')
        self.assertEqual(records[0]['filters']['status'],'search-result-unverified')

    def test_malformed_result_url_does_not_erase_search_observation(self):
        part={'type':'tool','tool':'searxng_search','state':{'status':'completed',
            'input':{'query':'synthetic'},'output':'An incomplete URL https://[ was returned'}}
        capture_searches(self.root,[{'parts':[part]}],{'phase':'prepare'},'2026-10-15T12:00:00Z')
        record=next(query_records(self.root))
        self.assertEqual(record['resultUrls'],[])
        self.assertTrue(record['resultUrlError'])
        self.assertIn('https://[',record['originalText'])

    def test_workflow_checks_out_rag_first_and_publishes_at_end_even_when_catalog_publication_is_off(self):
        workflow=yaml.load((ROOT/'.github/workflows/stage.yml').read_text(),Loader=yaml.BaseLoader)
        bootstrap=workflow['jobs']['stage']['steps']
        checkout=next(s for s in bootstrap if s.get('with',{}).get('repository')=='stefanospetsios/uniq2me-rag')
        self.assertEqual(checkout['with']['ref'],'main')
        self.assertNotIn('if',checkout)
        self.assertLess(bootstrap.index(checkout),next(i for i,s in enumerate(bootstrap) if s['name']=='Load current shared controller'))
        steps=yaml.load((ROOT/'.github/actions/stage/action.yml').read_text(),Loader=yaml.BaseLoader)['runs']['steps']
        publisher=next(s for s in steps if s.get('run')=='python controller/ci/source_cache.py publish')
        self.assertIn('always()',publisher['if'])
        self.assertNotIn('inputs.publish',publisher['if'])
        self.assertNotIn("inputs.stage == 'prepare'",publisher['if'])
        self.assertGreater(steps.index(publisher),next(i for i,s in enumerate(steps) if
            s.get('uses')=='./controller/.github/actions/refine-sfs' and s['with']['sfs']=='F'))
        self.assertNotIn('github.token',publisher['env']['SOURCE_CACHE_TOKEN'])


if __name__=='__main__':
    unittest.main()
