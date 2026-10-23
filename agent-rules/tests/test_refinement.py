"""Selective refinement and retry/publication tests use local repositories only."""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import HEX, byte_digest, digest, file_manifest, read, write, validate_question
from ci.pipeline import command
from ci.pipeline import CatalogReviewExhausted
from ci.refinement import DEFAULT_MODELS, TASKS, RefinementPipeline, check_catalog, parse_plan, select_base
from ci.smoke_test import create_fixture_pipeline
from ci.runtime import OpenCode, ollama_preflight
from ci.checkpoints import seal, restore


def fixture(root, inputs=None, publish=False):
    original, remote = create_fixture_pipeline(root, publish=publish)
    pipeline = RefinementPipeline()
    pipeline.__dict__.update(original.__dict__)
    plan = parse_plan(inputs or {'sfs': 'A'})
    for directory in ('json', 'repo-sf'):
        shutil.copytree(pipeline.work / 'inputs' / directory, pipeline.work / 'base' / directory)
        shutil.copytree(pipeline.work / 'inputs' / directory, pipeline.work / directory)
    # Give every starting question its own clear identity to test preservation.
    for slot in pipeline.state['slots']:
        name = 'repo-sf/en/' + slot['repoId'] + '.json'
        record = read(pipeline.work / 'base' / name)
        record['question']['shortBinaryQuestion'] = 'Has base condition ' + slot['repoId'] + ' been met?'
        record['isPlaceholder'] = False
        write(pipeline.work / 'base' / name, record)
        write(pipeline.work / name, record)
    pipeline.state.update(refinementPlan=plan, baseCatalog={'repository':'stefanospetsios/uniq2me',
        'commit':'1'*40}, sourceScopes=plan['sfs'], sfsGates={}, modelRecords={}, activeModelLabel='a',
        baseCatalogManifest=file_manifest(pipeline.work / 'base'), ragMode='repo-web')
    pipeline.save()
    pipeline.sources()
    return pipeline, remote


class PlanTests(unittest.TestCase):
    def test_defaults_and_single_or_all_sfs(self):
        self.assertEqual(parse_plan({})['sfs'], list(HEX))
        plan = parse_plan({'sfs':'f'})
        self.assertEqual(plan['sfs'], ['F'])
        self.assertEqual(plan['models'], DEFAULT_MODELS)
        self.assertEqual([t for t,v in plan['tasks'].items() if v['enabled']], ['complete_fields'])

    def test_empty_conflicting_invalid_model_or_sfs_are_rejected(self):
        for settings in ({'sfs':'10'}, {'sfs':''}, {'rework_questions':True},
            {'complete_fields':False}, {'model_a':'$(secret)'}, {'ai_prompts_model':'d'},
            {'complete_fields':'false'}, {'mode':'real'}, {'mode':'typo'}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                parse_plan(settings)

    def test_independent_model_assignment(self):
        plan = parse_plan({'complete_fields':False, 'ai_prompts':True, 'ai_prompts_model':'c',
                          'translate_fr':True, 'translate_fr_model':'b'})
        self.assertEqual(plan['tasks']['ai_prompts']['model'], 'c')
        self.assertEqual(plan['tasks']['translate_fr']['model'], 'b')

    def test_precedence_selects_last_ai_then_last_curated_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = []
            for repo in ('uniq2me','uniq2me-ai'):
                for number in (1,2):
                    path = root / repo / str(number)
                    write(path / 'json/en/0.json', {})
                    snapshots.append({'repository':'stefanospetsios/'+repo, 'commit':str(number)*40,
                        'root':str(path.relative_to(root)), 'snapshotDigest':'sha256:'+'a'*64})
            write(root / 'manifest.json', {'snapshots':snapshots})
            references = {'manifestRef':'manifest.json'}
            base, metadata = select_base(root, references, '3'*40)
            self.assertEqual(base, root/'uniq2me-ai/2')
            self.assertEqual(metadata['commit'], '2'*40)
            write(root/'manifest.json', {'snapshots':snapshots[:2]})
            self.assertEqual(select_base(root,references,'3'*40)[0],root/'uniq2me/2')
            self.assertEqual(select_base(root,{},'3'*40)[0],root/'inputs')
            (root/'uniq2me/2/json/en/0.json').unlink()
            with self.assertRaisesRegex(ValueError,'no root catalog'):
                select_base(root,references,'3'*40)

    def test_dispatch_form_has_compact_question_selector_and_independent_remaining_tasks(self):
        workflow = yaml.load((ROOT/'examples/air.yml').read_text(),Loader=yaml.BaseLoader)
        inputs = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(len(inputs),25)
        self.assertEqual(inputs['retry_models']['type'],'string')
        self.assertEqual(inputs['retry_models']['default'],'b,c')
        self.assertEqual(next(iter(inputs)),'target_branch')
        self.assertEqual(inputs['sfs']['options'],list(HEX)+['all'])
        self.assertEqual(inputs['mode']['options'],['dry-run','live'])
        self.assertEqual(inputs['question_mode']['options'], ['aY', 'aN', 'skip'])
        self.assertEqual(inputs['question_model']['options'], ['a','b','c'])
        for task in TASKS[2:4]:
            self.assertEqual(inputs[task]['type'],'boolean')
            self.assertEqual(inputs[task+'_model']['options'],['a','b','c'])
        self.assertEqual(inputs['translate']['type'], 'boolean')
        self.assertEqual(inputs['create_masks']['type'], 'boolean')
        for job in workflow['jobs'].values():
            self.assertEqual(job['with']['refinement_plan'],'${{ toJSON(inputs) }}')

    def test_each_sfs_has_credential_free_execution_then_scoped_publication(self):
        workflow=yaml.load((ROOT/'.github/actions/stage/action.yml').read_text(),Loader=yaml.BaseLoader)
        steps=workflow['runs']['steps']
        scopes=[s['with']['sfs'] for s in steps if s.get('uses')=='./controller/.github/actions/refine-sfs']
        self.assertEqual(scopes,list(HEX))
        sf=yaml.load((ROOT/'.github/actions/refine-factor/action.yml').read_text(),Loader=yaml.BaseLoader)['runs']['steps']
        self.assertNotIn('PUSH_TOKEN',sf[0].get('env',{}))
        self.assertIn('incremental.py factor',sf[0]['run'])
        publication=yaml.load((ROOT/'.github/actions/catalog-publication/action.yml').read_text(),Loader=yaml.BaseLoader)['runs']['steps']
        upload=next(s for s in publication if s.get('uses')=='actions/upload-artifact@v4')
        self.assertIn("inputs.checkpoint_url == ''",upload['if'])
        cleanup=next(s for s in publication if 'checkpoint_retention.py' in s.get('run',''))
        publish=next(s for s in publication if 'incremental.py publish' in s.get('run',''))
        self.assertLess(publication.index(cleanup),publication.index(publish))
        self.assertIn('PUSH_TOKEN',publish['env'])
        self.assertNotIn('GITHUB_TOKEN',publish['env'])
        sfs=yaml.load((ROOT/'.github/actions/refine-sfs/action.yml').read_text(),Loader=yaml.BaseLoader)['runs']['steps']
        self.assertEqual([s['with']['sf'] for s in sfs if s.get('uses')=='./controller/.github/actions/refine-factor'],list(HEX))
        jobs=yaml.load((ROOT/'.github/workflows/catalog.yml').read_text(),Loader=yaml.BaseLoader)['jobs']
        self.assertEqual(jobs['prompts']['needs'], 'refine')
        self.assertEqual(jobs['solutions']['needs'], 'prompts')
        self.assertEqual(jobs['french']['needs'], 'solutions')
        self.assertEqual(jobs['greek']['needs'], 'french')
        self.assertEqual(jobs['german']['needs'], 'greek')
        self.assertEqual(jobs['retry_1']['needs'], ['refine', 'german'])
        self.assertEqual(jobs['retry_2']['needs'],'retry_1')
        self.assertEqual(jobs['retry_3']['needs'],'retry_2')
        self.assertEqual(jobs['catalog']['needs'],'retry_3')

    def test_workflow_tests_isolate_the_dispatch_destination(self):
        workflow=yaml.load((ROOT/'.github/actions/stage/action.yml').read_text(),Loader=yaml.BaseLoader)
        step=next(s for s in workflow['runs']['steps'] if s['name']=='Test the controller')
        inherited={'TARGET_BRANCH':'en/qwen3b','REFINEMENT_PLAN':json.dumps({'sfs':'0'}),
                   'DRY_RUN':'false'}
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{**inherited,**step['env']}),redirect_stdout(io.StringIO()):
            pipeline,_=fixture(Path(directory))
            pipeline.pinned()

    def test_workflow_rejects_conflicting_selections_before_controller_tests(self):
        workflow=yaml.load((ROOT/'.github/actions/stage/action.yml').read_text(),Loader=yaml.BaseLoader)
        steps=workflow['runs']['steps']
        validation=next(s for s in steps if s['name']=='Validate refinement selections')
        self.assertLess(steps.index(validation),next(i for i,s in enumerate(steps) if s['name']=='Test the controller'))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'controller').symlink_to(ROOT,target_is_directory=True)
            for rewrite,preserve in ((True,True),(True,False),(False,True)):
                with self.subTest(rewrite=rewrite,preserve=preserve):
                    env={**os.environ,'PATH':str(Path(sys.executable).parent)+os.pathsep+os.environ.get('PATH',''),
                         'REFINEMENT_PLAN':json.dumps({'rework_questions':rewrite,'complete_fields':preserve})}
                    result=subprocess.run(['bash','-e','-c',validation['run']],cwd=directory,
                                          env=env,text=True,capture_output=True)
                    self.assertEqual(result.returncode,1 if rewrite and preserve else 0,result.stderr)
                    if rewrite and preserve:
                        self.assertIn('::error title=Invalid refinement inputs::Choose either (a1)',result.stdout)

    def test_selected_text_only_model_warms_at_native_context_without_fake_tool_support(self):
        tag = DEFAULT_MODELS['b']
        requests = []
        def api(url, data=None, **kwargs):
            requests.append((url,data))
            if url.endswith('/api/version'):
                return {'version':'synthetic'}
            if url.endswith('/api/tags'):
                return {'models':[{'name':tag,'digest':'a'*64}]}
            if url.endswith('/api/show'):
                self.assertEqual(data['model'],tag)
                return {'model_info':{'general.architecture':'test','test.context_length':131072},
                        'capabilities':['completion']}
            if url.endswith('/api/generate'):
                self.assertEqual(data['options']['num_ctx'],131072)
                self.assertEqual(data['model'],tag)
                return {'response':'Synthetic ready','prompt_eval_count':2,'eval_count':1}
            if url.endswith('/api/ps'):
                return {'models':[{'digest':'a'*64,'context_length':131072}]}
            if url.endswith('/v1/chat/completions'):
                self.assertEqual(data['model'],tag)
                return {'choices':[{'message':{'content':'Synthetic ready'}}]}
            raise AssertionError('Unexpected inference/tool endpoint: '+url)
        with tempfile.TemporaryDirectory() as directory,patch('ci.runtime.http_json',side_effect=api),redirect_stdout(io.StringIO()):
            record=ollama_preflight('http://fixture',directory,tag=tag,native_context=True,require_tools=False)
        self.assertEqual(record['tag'],tag)
        self.assertEqual(record['effectiveContext'],131072)
        self.assertTrue(record['explicitContextFallback'])
        self.assertFalse(record['nativeToolCallingSupported'])
        self.assertTrue(record['textOnlyReview'])
        self.assertFalse(any(url.endswith('/api/chat') for url,_ in requests))

    def test_opencode_submits_and_checks_the_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            tag=DEFAULT_MODELS['c']
            record={'tag':tag,'digest':'a'*64,'effectiveContext':32768}
            server=OpenCode(directory,ROOT,read(ROOT/'opencode.json'),directory,record)
            submissions=[]
            def api(path,data=None,**kwargs):
                if path=='/session': return {'id':'synthetic-selected-model'}
                if path.endswith('/prompt_async'):
                    submissions.append(data)
                if path=='/session/status': return {}
                if path.endswith('/message'):
                    return [{'info':{'role':'assistant','providerID':'ollama','modelID':tag,
                        'time':{'completed':1},'finish':'stop','tokens':{'total':3}},
                        'parts':[{'type':'text','text':'{"synthetic":true}'}]}]
                return None
            server.api=api
            server.check_residency=lambda:None
            with redirect_stdout(io.StringIO()):
                self.assertTrue(server.ask('sf-completer','Synthetic prompt',30,'selected-model')['synthetic'])
            self.assertEqual(submissions[0]['model'],{'providerID':'ollama','modelID':tag})
            self.assertEqual(read(Path(directory)/'calls/selected-model/envelope.json')['modelRequested'],tag)


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        quiet=redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__,None,None,None)
        env=patch.dict(os.environ,{'REFINEMENT_PLAN':'','PUSH_TOKEN':'synthetic-local-token',
            'DRY_RUN':'true','TARGET_BRANCH':'',
            'GITHUB_STEP_SUMMARY':'','UNIQ2ME_INPUT_COMMITS':'','UNIQ2ME_AI_INPUT_COMMITS':'',
            'RAG_MODE':'repo-web','SOURCE_CACHE_MODE':'off'})
        env.start();self.addCleanup(env.stop)

    def test_one_sfs_preserves_all_questions_and_unselected_json_bytes(self):
        pipeline,_=fixture(self.root)
        base=file_manifest(pipeline.work/'repo-sf/en')
        with patch('ci.runtime.http_json',side_effect=AssertionError('No live inference in dry run')):
            pipeline.refine('A')
            pipeline.refine('B')
        for slot in pipeline.state['slots']:
            name=slot['repoId']+'.json'
            value=read(pipeline.work/'repo-sf/en'/name)
            original=read(pipeline.work/'base/repo-sf/en'/name)
            self.assertEqual(value['question']['shortBinaryQuestion'],original['question']['shortBinaryQuestion'])
            if slot['sfsId']!='A':
                self.assertEqual(byte_digest(pipeline.work/'repo-sf/en'/name),base[name])
        self.assertEqual(set(pipeline.state['sfsGates']),{'A'})
        from ci.metrics import collect
        metrics=collect(pipeline.root,pipeline.state)
        self.assertEqual(metrics['stages']['refine']['executionAttempts'],2)
        self.assertIsNotNone(metrics['stages']['refine']['activeSeconds'])
        pipeline.publish_sfs('A')
        first=command(['git','rev-parse','HEAD'],pipeline.output).stdout
        pipeline.publish_sfs('A')
        self.assertEqual(command(['git','rev-parse','HEAD'],pipeline.output).stdout,first)
        check_catalog(pipeline.output,pipeline.state['slots'])
        self.assertFalse((pipeline.output/'next-base.json').exists())
        self.assertIn('Has base condition 00A0', (pipeline.output/'pages/catalog/sets/A/0.en-US.mdx').read_text())

    def test_all_tasks_produce_solutions_prompts_and_all_translations(self):
        settings={task:True for task in TASKS}
        settings.update(complete_fields=False,sfs='A',ai_prompts_model='b',canonical_solutions_model='c')
        pipeline,_=fixture(self.root,settings)
        pipeline.refine('A')
        pipeline.publish_sfs('A')
        for language in ('en','fr','el','de'):
            value=read(pipeline.output/'repo-sf'/language/'00A0.json')
            self.assertEqual(value['identity']['language'],language)
            self.assertTrue((pipeline.output/'solutions'/language/'00A0.json').exists())
        self.assertIn('SYNTHETIC DRY RUN: investigate',read(pipeline.output/'repo-sf/en/00A0.json')['question']['promptAiOptimization'])
        self.assertEqual(read(pipeline.output/'solutions/en/00A0.json')['questionDigest'],
                         digest(read(pipeline.output/'catalog/questions/en/00A0.json')))
        self.assertEqual(set(read(pipeline.output/'refinement-status.json')['completedSfs']),{'A'})

    def test_failure_and_retry_replay_checked_tasks_without_reauthoring(self):
        pipeline,_=fixture(self.root,{'sfs':'A','complete_fields':True,'ai_prompts':True})
        original=pipeline.prompt_task
        count=0
        def fail(server,slot,record):
            nonlocal count
            count+=1
            if count==4:
                raise RuntimeError('Synthetic interruption')
            return original(server,slot,record)
        pipeline.prompt_task=fail
        with self.assertRaisesRegex(RuntimeError,'interruption'):
            pipeline.refine('A')
        self.assertNotIn('A',pipeline.state['sfsGates'])
        pipeline.prompt_task=original
        with patch.object(pipeline,'fields_task',side_effect=AssertionError('Completed fields regenerated')):
            pipeline.refine('A')
        pipeline.publish_sfs('A')
        self.assertEqual(len(pipeline.state['publications']),1)

    def test_changed_pinned_plan_is_rejected(self):
        pipeline,_=fixture(self.root)
        with patch.dict(os.environ,{'REFINEMENT_PLAN':json.dumps({'sfs':'B'})}):
            with self.assertRaisesRegex(ValueError,'Cannot change tasks'):
                pipeline.refine('A')

    def test_changed_destination_is_still_rejected_on_retry(self):
        pipeline,_=fixture(self.root)
        with patch.dict(os.environ,{'TARGET_BRANCH':'en/different-destination'}):
            with self.assertRaisesRegex(ValueError,'Cannot change destination on retry'):
                pipeline.pinned()

    def test_all_jobs_retry_checks_model_a_after_a_later_task_used_model_b(self):
        pipeline,_=fixture(self.root)
        pipeline.state.update(activeModelLabel='b',activeTask='ai_prompts')
        with patch('ci.pipeline.Pipeline.preflight',return_value=None):
            pipeline.preflight()
        self.assertEqual(pipeline.state['activeModelLabel'],'a')
        self.assertNotIn('activeTask',pipeline.state)

    def test_field_updates_repair_string_arrays_without_rewriting_protected_data(self):
        from ci.dry_run import synthetic_review
        pipeline,_=fixture(self.root)
        slot=next(s for s in pipeline.state['slots'] if s['sfsId']=='A')
        current=read(pipeline.work/'repo-sf/en'/(slot['repoId']+'.json'))
        current['meta']['customMetadata']={'nested':['preserve',None,{'flag':True}]}
        original=copy.deepcopy(current)
        source=next(iter(pipeline.frozen_sources().values()))
        updates={'question':{key:'Synthetic updated '+key for key in ('plain','technical-short','keywords')},
                 'meta':{'title':'Synthetic updated title'},
                 'clarificationQuestion':[{'key1':'What evidence is needed?','key2':'Synthetic evidence guidance'}],
                 'wikiLinks':[{'key1':'Synthetic source','key2':source['url']}]}
        invalid=copy.deepcopy(updates)
        invalid['clarificationQuestion']=['What evidence is needed?']
        invalid['wikiLinks']=[source['url']]
        pipeline.state.update(dryRun=False,model={'digest':'a'*64})
        prompts=[]
        class Server:
            def ask(self,role,prompt,*args,**kwargs):
                if role=='catalog-validator':
                    payload,_=json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidate":'):])
                    return synthetic_review(payload['candidate'],payload['requiredChecks'])
                prompts.append(prompt)
                return copy.deepcopy(invalid if len(prompts)==1 else updates)
        result=pipeline.fields_task(Server(),'complete_fields',slot,current,None)
        self.assertEqual(len(prompts),2)
        self.assertIn('clarificationQuestion[0] must be an object with exactly key1 and key2',prompts[1])
        self.assertIn('outputExample',prompts[0])
        self.assertNotIn('"fieldUpdateFormat":',prompts[0])
        self.assertEqual(current,original)
        self.assertEqual(result['question']['shortBinaryQuestion'],original['question']['shortBinaryQuestion'])
        self.assertEqual(result['question']['promptAiOptimization'],original['question']['promptAiOptimization'])
        for key in original:
            if key not in ('question','meta','clarificationQuestion','wikiLinks'):
                self.assertEqual(result[key],original[key],key)
        self.assertEqual(result['meta']['customMetadata'],original['meta']['customMetadata'])
        self.assertEqual(result['meta']['theme'],original['meta']['theme'])
        self.assertEqual(result['clarificationQuestion'],updates['clarificationQuestion'])
        cached=read(pipeline.work/'.task-cache/refine-complete_fields'/(slot['repoId']+'-fields.json'))
        self.assertEqual(cached['value'],updates)
        with patch.object(Server,'ask',side_effect=AssertionError('Checked field updates regenerated')):
            self.assertEqual(pipeline.fields_task(Server(),'complete_fields',slot,current,None),result)

    def test_field_update_shape_errors_identify_the_exact_field_and_reject_protected_keys(self):
        pipeline,_=fixture(self.root)
        slot=pipeline.state['slots'][0]
        current=read(pipeline.work/'repo-sf/en'/(slot['repoId']+'.json'))
        def check(mutator,reason):
            def task(server,task,key,prompt,validate,checks,synthetic):
                candidate=copy.deepcopy(synthetic)
                mutator(candidate)
                validate(candidate)
                return candidate
            with patch.object(pipeline,'task',side_effect=task),self.assertRaisesRegex(ValueError,reason):
                pipeline.fields_task(None,'complete_fields',slot,current,None)
        check(lambda v:v['wikiLinks'].__setitem__(0,'https://example.invalid'),r'wikiLinks\[0\].*key1 and key2')
        check(lambda v:v.update(identity={'repoId':'wrong'}),r'unexpected: identity')
        check(lambda v:v.update(fieldUpdateFormat='sf-field-updates-v1'),r'unexpected: fieldUpdateFormat')
        check(lambda v:v['question'].update(shortBinaryQuestion='Changed question'),r'unexpected: shortBinaryQuestion')
        check(lambda v:v['meta'].update(theme={}),r'unexpected: theme')
        check(lambda v:v['question'].pop('plain'),r'missing: plain')
        check(lambda v:v['wikiLinks'][0].update(key2='https://example.invalid'),r'wikiLinks\[0\].key2.*admitted')

    def test_llm_only_field_updates_do_not_require_a_source_or_invent_links(self):
        pipeline,_=fixture(self.root)
        pipeline.state.update(ragMode='disabled',dryRun=False)
        pipeline.frozen_sources=lambda:{}
        slot=pipeline.state['slots'][0]
        current=read(pipeline.work/'repo-sf/en'/(slot['repoId']+'.json'))
        def task(server,task,key,prompt,validate,checks,synthetic):
            self.assertEqual(synthetic['wikiLinks'],[])
            validate(synthetic)
            return synthetic
        with patch.object(pipeline,'task',side_effect=task):
            result=pipeline.fields_task(None,'complete_fields',slot,current,None)
        self.assertEqual(result['wikiLinks'],[])
        self.assertEqual(result['question']['shortBinaryQuestion'],current['question']['shortBinaryQuestion'])

    def test_partial_base_placeholders_do_not_block_other_areas_or_become_fake_solutions(self):
        pipeline,_=fixture(self.root,{'sfs':'A','canonical_solutions':True})
        for root in ('base',''):
            path=pipeline.work/root/'repo-sf/en/00A0.json'
            record=read(path)
            record['isPlaceholder']=True
            record['question']['shortBinaryQuestion']='[Placeholder] Has an undefined condition been met?'
            write(path,record)
        pipeline.state['baseCatalogManifest']=file_manifest(pipeline.work/'base')
        pipeline.refine('A');pipeline.publish_sfs('A')
        self.assertTrue(read(pipeline.output/'repo-sf/en/00A0.json')['isPlaceholder'])
        self.assertFalse((pipeline.output/'solutions/en/00A0.json').exists())
        self.assertTrue((pipeline.output/'solutions/en/00A1.json').exists())
        self.assertEqual(pipeline.state['sfsGates']['A']['pendingSlots'],['00A0'])

    def test_pending_source_downloads_preserve_scope_bytes_and_allow_later_sfs_publication(self):
        self.pending_source_publication('pending-source-downloads')

    def test_pending_source_research_preserves_scope_bytes_and_allows_later_sfs_publication(self):
        self.pending_source_publication('pending-source-research')

    def pending_source_publication(self, pending_status):
        pipeline,_=fixture(self.root,{'sfs':'all'})
        pipeline.state['pendingSourceScopes'] = {'D': {'reason': 'HTTP 400 left too few documents',
            'status': pending_status, 'retrievalFailures': []}}
        base_files = pipeline.sfs_files('D')
        with patch.object(pipeline,'server',side_effect=AssertionError('Deferred SFS must not call a model')):
            pipeline.refine('D')
        self.assertEqual(pipeline.sfs_files('D'),base_files)
        self.assertEqual(len(pipeline.state['sfsGates']['D']['pendingSlots']),16)
        self.assertEqual(pipeline.state['sfsGates']['D']['tasks'],[])
        pipeline.publish_sfs('D')
        status = read(pipeline.output/'refinement-status.json')
        self.assertEqual(len(status['pendingPositions']),16)
        self.assertEqual(status['pendingSourceScopes']['D']['status'],pending_status)
        self.assertTrue(all(p['status']==pending_status for p in status['pendingPositions']))
        self.assertIn('Selected updates are pending',
            (pipeline.output/'pages/catalog/sets/D/0.en-US.mdx').read_text())
        for name, checksum in base_files.items():
            self.assertEqual(byte_digest(pipeline.output/name),checksum)
        pipeline.refine('E');pipeline.publish_sfs('E')
        self.assertEqual(pipeline.state['sfsGates']['E']['pendingSlots'],[])
        self.assertEqual([p['stage'] for p in pipeline.state['publications']],['sfs-D','sfs-E'])

    def test_exhausted_question_review_keeps_base_and_continues_other_positions(self):
        pipeline,_=fixture(self.root,{'sfs':'A','rework_questions':True,'complete_fields':False,
                                    'canonical_solutions':True})
        original=pipeline.question_task
        def author(server,slot,record):
            if slot['sfId']=='0':
                raise CatalogReviewExhausted('Synthetic unresolved review',{'rejected':True},{'decision':'rejected'}, {})
            return original(server,slot,record)
        pipeline.question_task=author
        pipeline.refine('A');pipeline.publish_sfs('A')
        base=read(pipeline.work/'base/repo-sf/en/00A0.json')
        actual=read(pipeline.output/'repo-sf/en/00A0.json')
        self.assertEqual(actual['question']['shortBinaryQuestion'],base['question']['shortBinaryQuestion'])
        self.assertFalse((pipeline.output/'solutions/en/00A0.json').exists())
        self.assertTrue((pipeline.output/'solutions/en/00A1.json').exists())
        self.assertEqual(pipeline.state['sfsGates']['A']['pendingSlots'],['00A0'])
        self.assertTrue((pipeline.root/'pending-question-repairs/00A0.json').exists())

    def test_exhausted_a1_fields_restore_base_question_and_remove_unpublished_contract(self):
        from ci.pipeline import CandidateAttemptsExhausted
        pipeline,_=fixture(self.root,{'sfs':'A','rework_questions':True,'complete_fields':False})
        fields=pipeline.fields_task
        def complete(server,task,slot,current,question):
            if slot['sfId']=='0':
                raise CandidateAttemptsExhausted('Synthetic invalid wrapper after all models',{'invalid':True})
            return fields(server,task,slot,current,question)
        pipeline.fields_task=complete
        pipeline.refine('A')
        original=read(pipeline.work/'base/repo-sf/en/00A0.json')
        actual=read(pipeline.work/'repo-sf/en/00A0.json')
        self.assertEqual(actual['question'],original['question'])
        self.assertFalse((pipeline.work/'catalog/questions/en/00A0.json').exists())
        self.assertTrue((pipeline.work/'catalog/questions/en/00A1.json').exists())
        self.assertEqual(pipeline.state['sfsGates']['A']['pendingSlots'],['00A0'])
        self.assertFalse((pipeline.root/'refinement-tasks/rework_questions/00A0.json').exists())
        self.assertTrue((pipeline.root/'refinement-tasks/rework_questions/00A1.json').exists())

    def test_question_task_repairs_missing_type_and_empty_alternatives_before_review(self):
        from ci.dry_run import question,synthetic_review
        from ci.contracts import QUESTION_CHECKS
        pipeline,_=fixture(self.root,{'sfs':'6','rework_questions':True,'complete_fields':False})
        slot=next(s for s in pipeline.state['slots'] if s['repoId']=='0060')
        record=read(pipeline.work/'repo-sf/en/0060.json')
        candidate=question(slot)
        candidate.update(parentQuestionDigest=digest(record['question']['shortBinaryQuestion']),semanticRelation='changed')
        pipeline.state.update(dryRun=False,maximumAttempts=2,model={'digest':'a'*64})
        authors,reviews=[],[]
        class Server:
            def ask(self,role,prompt,*args,**kwargs):
                if role=='catalog-validator':
                    reviews.append(prompt)
                    return synthetic_review(candidate,pipeline.draft_checks(QUESTION_CHECKS),candidate['fixtures'])
                authors.append(prompt)
                value=copy.deepcopy(candidate)
                if len(authors)==1:
                    value.pop('recordType')
                    value['contract']['alternatives']=[]
                return value
        result=pipeline.question_task(Server(),slot,record)
        self.assertEqual(len(authors),2)
        self.assertEqual(len(reviews),1)
        self.assertIn('Top-level "recordType" must equal "question-candidate"',authors[0])
        self.assertIn('Both contract arrays must contain at least one substantive nonempty string',authors[0])
        self.assertIn('recordType must equal "question-candidate"; received missing',authors[1])
        self.assertIn('contract.alternatives requires a nonempty array',authors[1])
        self.assertEqual(result,candidate)
        self.assertEqual(read(pipeline.work/'catalog/questions/en/0060.json'),candidate)
        self.assertTrue((pipeline.work/'.task-cache/refine-rework_questions/0060.json').exists())

    def test_repo_only_review_keeps_source_citations_and_accepts_local_candidate_checks(self):
        from ci.dry_run import question,synthetic_review
        from ci.contracts import QUESTION_CHECKS
        pipeline,_=fixture(self.root,{'sfs':'0','rework_questions':True,'complete_fields':False})
        slot=next(s for s in pipeline.state['slots'] if s['repoId']=='0001')
        record=read(pipeline.work/'repo-sf/en/0001.json')
        candidate=question(slot)
        candidate.update(parentQuestionDigest=digest(record['question']['shortBinaryQuestion']),semanticRelation='changed')
        pipeline.state.update(dryRun=False,ragMode='repo-only',maximumAttempts=1,model={'digest':'a'*64})
        requests=[]
        class Server:
            def ask(self,role,prompt,*args,**kwargs):
                if role!='catalog-validator':
                    return copy.deepcopy(candidate)
                payload,_=json.JSONDecoder().raw_decode('{"candidate":'+prompt.split('{"candidate":',1)[1])
                requests.append(payload)
                response=synthetic_review(candidate,pipeline.draft_checks(QUESTION_CHECKS),candidate['fixtures'])
                for row in response['checks']:
                    if row['ruleId'] not in payload['checksRequiringSourceRefs']:
                        row['sourceRefs']=[]
                return response
        self.assertEqual(pipeline.question_task(Server(),slot,record),candidate)
        self.assertEqual(requests[0]['checksRequiringSourceRefs'],
            ['concern-coverage','no-false-source-verification','scope-and-evidence'])
        self.assertTrue((pipeline.work/'.task-cache/refine-rework_questions/0001.json').exists())

    def test_live_refinement_question_review_uses_pending_policy_but_fields_stay_strict(self):
        from ci.dry_run import question
        pipeline,_=fixture(self.root)
        pipeline.state.update(dryRun=False,ragMode='disabled',maximumAttempts=1,model={'digest':'a'*64})
        pipeline.frozen_sources=lambda:{}
        slot=next(s for s in pipeline.state['slots'] if s['repoId']=='00A0')
        candidate=question(slot);candidate['sourceRefs']=[]
        class Server:
            def ask(self,role,prompt,*args,**kwargs):
                if role=='catalog-validator':
                    return {'artifactDigest':digest(candidate),'decision':'rejected','checks':[],
                            'unresolved':['Synthetic unresolved semantic finding']}
                return candidate
        validate=lambda v:validate_question(v,slot,{},set(candidate['concernIds']),allow_unsourced=True)
        with self.assertRaises(CatalogReviewExhausted):
            pipeline.generate(Server(),'refine-rework_questions','00A0','sf-completer','Synthetic question task',0,
                              validate,['source-support'])
        with self.assertRaises(RuntimeError) as error:
            pipeline.generate(Server(),'refine-rework_questions','00A0-fields','sf-completer','Synthetic field task',0,
                              validate,['source-support'])
        self.assertNotIsInstance(error.exception,CatalogReviewExhausted)

    def test_remote_push_recovery_does_not_duplicate_commit(self):
        pipeline,remote=fixture(self.root,publish=True)
        pipeline.refine('A')
        original=pipeline.finish_publication
        pipeline.finish_publication=lambda pending: (_ for _ in ()).throw(RuntimeError('Synthetic crash before push'))
        with self.assertRaisesRegex(RuntimeError,'before push'):
            pipeline.publish_sfs('A')
        pending=copy.deepcopy(pipeline.state['publicationIntent'])
        pipeline.finish_publication=original
        pipeline.publish_sfs('A')
        self.assertEqual(pipeline.state['publications'][0]['commit'],pending['commit'])
        self.assertEqual(command(['git','rev-list','--count',pipeline.state['branch']],remote).stdout.strip(),'2')

    def test_destination_workflow_is_preserved_and_language_refs_advance_atomically(self):
        pipeline,remote=fixture(self.root,{'sfs':'A','translate_fr':True,'translate_el':True},publish=True)
        pipeline.state['branch']='en/synthetic'
        write(pipeline.output/'.github/owner.json',{'destination-owned-workflow':'preserve exactly'})
        command(['git','add','.github'],pipeline.output)
        command(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','Owner infrastructure'],pipeline.output)
        before=file_manifest(pipeline.output/'.github')
        pipeline.refine('A')
        pipeline.publish_sfs('A')
        commit=pipeline.state['publications'][0]['commit']
        for ref in ('en/synthetic','fr/synthetic','el/synthetic'):
            self.assertEqual(command(['git','rev-parse',ref],remote).stdout.strip(),commit)
        self.assertEqual(file_manifest(pipeline.output/'.github'),before)

    def test_checkpoint_restores_completed_and_partial_sfs_on_a_fresh_checkout(self):
        pipeline,remote=fixture(self.root,{'sfs':'all','complete_fields':False,'ai_prompts':True},publish=True)
        pipeline.refine('0');pipeline.publish_sfs('0')
        seal(pipeline)
        root=self.root/'fresh-runner';root.mkdir()
        command(['git','clone','-q',str(remote),str(root/'uniq2me-ai')],root)
        shutil.copytree(pipeline.root,root/'checkpoint')
        new=RefinementPipeline()
        new.root=root/'checkpoint';new.work=new.root/'work';new.output=root/'uniq2me-ai'
        new.state_path=new.root/'controller-state.json';new.state=read(new.state_path)
        restore(new)
        new.refine('0');new.publish_sfs('0')
        new.refine('1');new.publish_sfs('1')
        self.assertEqual([p['stage'] for p in new.state['publications']],['sfs-0','sfs-1'])
        self.assertEqual(command(['git','rev-list','--count',new.state['branch']],remote).stdout.strip(),'3')

    def test_pre_push_checkpoint_recovers_a_push_whose_completion_was_never_uploaded(self):
        pipeline,remote=fixture(self.root,publish=True)
        pipeline.refine('A');pipeline.publish_sfs('A',prepare_only=True)
        self.assertEqual(pipeline.state['publications'],[])
        pending=copy.deepcopy(pipeline.state['publicationIntent'])
        seal(pipeline)
        backup=self.root/'saved-before-push';shutil.copytree(pipeline.root,backup)
        # Remote received the commit, but the only durable artifact still has
        # the earlier publication intent and expected remote parent.
        pipeline.publish_sfs('A')
        root=self.root/'retry';root.mkdir()
        command(['git','clone','-q',str(remote),str(root/'uniq2me-ai')],root)
        shutil.copytree(backup,root/'checkpoint')
        new=RefinementPipeline();new.root=root/'checkpoint';new.work=new.root/'work'
        new.output=root/'uniq2me-ai';new.state_path=new.root/'controller-state.json';new.state=read(new.state_path)
        restore(new)
        new.refine('A');new.publish_sfs('A',prepare_only=True);new.publish_sfs('A')
        self.assertEqual(new.state['publications'][0]['commit'],pending['commit'])
        self.assertEqual(len(new.state['publications']),1)
        self.assertEqual(command(['git','rev-list','--count',new.state['branch']],remote).stdout.strip(),'2')

    def test_all_sixteen_sfs_produce_exactly_sixteen_intermediate_commits(self):
        pipeline,remote=fixture(self.root,{'sfs':'all','complete_fields':False,'ai_prompts':True},publish=True)
        for sfs in HEX:
            pipeline.refine(sfs)
            pipeline.publish_sfs(sfs)
        self.assertEqual([p['stage'] for p in pipeline.state['publications']],['sfs-'+s for s in HEX])
        self.assertEqual(command(['git','rev-list','--count',pipeline.state['branch']],remote).stdout.strip(),'17')
        self.assertEqual(read(pipeline.output/'refinement-status.json')['completedSfs'],list(HEX))


if __name__=='__main__':
    unittest.main()
