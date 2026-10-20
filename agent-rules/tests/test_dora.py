"""DORA-specific isolation, legal admission and publication; no remote writes/inference."""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ci.contracts import HEX, byte_digest, digest, file_manifest, read, same_shape, write
from ci.dora import DoraPipeline, load_profile, marker, seed_catalog, validate_profile_catalog
from ci.pipeline import command, check_source_passages, fetch_source_snapshot
from ci.reference_inputs import data_path
from ci.refinement import DATA_DIRECTORIES, check_catalog, parse_plan
from ci.source_cache import SourceCache, configuration
from ci.source_research import SourceAdmissionError, SourceDownloadPending, dora_core_source, research_scope, select_sources, url_identity
from ci.rag_store import capture_searches, query_records
from ci.smoke_test import create_fixture_pipeline
import test_source_research as source_fixtures

CORE = 'https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32022R2554'
RTS = 'https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32024R1774'


def fixture(root):
    original, remote = create_fixture_pipeline(root, publish=False)
    pipeline = DoraPipeline()
    pipeline.__dict__.update(original.__dict__)
    pipeline.state.update(branch='en/dora-qwen3', ragMode='repo-web', sourceCache={'mode':'off'},
        refinementPlan=parse_plan({'sfs':'0','rework_questions':True,'complete_fields':False}),
        sfsGates={}, modelRecords={}, activeModelLabel='a', sourceScopes=['0'])
    pipeline.prepare_profile_inputs()
    for directory in DATA_DIRECTORIES:
        source = pipeline.work / 'inputs' / directory
        if source.exists():
            shutil.copytree(source, pipeline.work / 'base' / directory, dirs_exist_ok=True)
            shutil.copytree(source, pipeline.work / directory, dirs_exist_ok=True)
    write(pipeline.work / 'base/catalog-profile.json', pipeline.state['catalogProfile'])
    pipeline.state.update(baseCatalog={'repository':'stefanospetsios/uniq2me-dora','commit':'1'*40},
        baseCatalogManifest=file_manifest(pipeline.work / 'base'))
    pipeline.save()
    pipeline.sources()
    return pipeline, remote


class DoraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stream = redirect_stdout(io.StringIO())
        stream.__enter__()
        self.addCleanup(stream.__exit__,None,None,None)

    def test_profile_has_sixteen_distinct_dora_scopes_and_primary_article_mapping(self):
        profile = load_profile()
        self.assertEqual([a['id'] for a in profile['areas']],list(HEX))
        self.assertEqual(len({a['title'] for a in profile['areas']}),16)
        self.assertTrue(all(a['articles'] and 'dora' in a['sourceIds'] for a in profile['areas']))
        self.assertFalse(profile['taxonomyIsOfficial'])
        self.assertIn('TLPT',profile['areas'][12]['boundaries'])

    def test_seed_retains_all_256_record_shapes_and_clears_assessment_content(self):
        pipeline,_=fixture(self.root)
        check_catalog(pipeline.work,pipeline.state['slots'])
        validate_profile_catalog(pipeline.work/'base',load_profile())
        self.assertEqual(len(list((pipeline.work/'repo-sf/en').glob('*.json'))),256)
        schema=read(ROOT/'agent-rules/tests/fixtures/sf-template.json')
        for slot in pipeline.state['slots']:
            record=read(pipeline.work/'repo-sf/en'/(slot['repoId']+'.json'))
            same_shape(record,schema)
            self.assertTrue(record['isPlaceholder'])
            self.assertIsNone(record['resultScore'])
            self.assertEqual(record['review'],{'status':'draft','approvals':[]})
            self.assertTrue(all(v['vote'] is None for v in record['signatures'].values()))
            self.assertIn('DORA',record['question']['shortBinaryQuestion'])
            self.assertEqual(record['wikiLinks'],[])

    def test_generic_commits_stay_references_and_matching_ai_base_takes_precedence(self):
        pipeline,_=fixture(self.root)
        generic=pipeline.work/'inputs/reference-inputs/generic'
        generic.mkdir(parents=True)
        dora=pipeline.work/'inputs/reference-inputs/dora'
        shutil.copytree(pipeline.work/'base',dora)
        entries=[{'repository':'stefanospetsios/uniq2me','commit':'2'*40,'root':str(generic.relative_to(pipeline.work)),'snapshotDigest':'sha256:'+'2'*64},
            {'repository':'stefanospetsios/uniq2me-ai','commit':'3'*40,'root':str(dora.relative_to(pipeline.work)),'snapshotDigest':'sha256:'+'3'*64}]
        manifest=pipeline.work/'inputs/references.json'
        write(manifest,{'snapshots':entries[:1]})
        pipeline.state['referenceInputs']={'manifestRef':'inputs/references.json'}
        base,_=pipeline.select_profile_base({})
        self.assertEqual(base,pipeline.work/'inputs')
        self.assertEqual(len(pipeline.state['referenceOnlyCatalogs']),1)
        write(manifest,{'snapshots':entries})
        base,provenance=pipeline.select_profile_base({})
        self.assertEqual(base,dora)
        self.assertEqual(provenance['repository'],'stefanospetsios/uniq2me-ai')
        self.assertTrue(data_path('catalog-profile.json'))
        self.assertFalse(data_path('opencode.json'))

    def test_generic_branch_and_changed_taxonomy_are_rejected(self):
        pipeline,_=fixture(self.root)
        with patch.dict(os.environ,{'TARGET_BRANCH':'en/qwen3'}),self.assertRaisesRegex(ValueError,'DORA instance'):
            pipeline.initialize()
        path=pipeline.work/'base/json/en/0.json'
        area=read(path);area['keyTitle']='Generic cybersecurity';write(path,area)
        with self.assertRaisesRegex(ValueError,'scope changed'):
            validate_profile_catalog(pipeline.work/'base',load_profile())

    def test_profile_identity_and_dimensions_cannot_be_spoofed_with_taxonomy_digest(self):
        pipeline,_=fixture(self.root)
        path=pipeline.work/'base/catalog-profile.json'
        original=read(path)
        for key,value in [('methodologyId','other'),('profileId','other'),('profileVersion',2),('sfPerSfs',32)]:
            with self.subTest(key=key):
                write(path,{**original,key:value})
                with self.assertRaisesRegex(ValueError,'another framework or DORA taxonomy'):
                    validate_profile_catalog(pipeline.work/'base',load_profile())

    def test_existing_unbound_destination_is_rejected_before_seeding(self):
        pipeline,_=fixture(self.root)
        pipeline.state['expectedRemote']='a'*40
        with self.assertRaisesRegex(ValueError,'unbound catalog'):
            pipeline.prepare_profile_inputs()

    def test_scope_fingerprint_is_bound_to_profile_even_with_same_topic_text(self):
        pipeline,_=fixture(self.root)
        pipeline.state['model']={'digest':'1'*64,'effectiveContext':262144}
        cache=SourceCache(pipeline)
        area={'keyTitle':'Same scope text'}
        dora=cache.scope_fingerprint('0',area)
        pipeline.state.pop('catalogProfile')
        self.assertNotEqual(dora,cache.scope_fingerprint('0',area))

    def test_dora_laws_are_allowed_and_generic_technical_topic_rules_stay_strict(self):
        sources=[{**source_fixtures.metadata(u),'type':'law'} for u in [CORE,RTS]]
        observed={url_identity(u) for u in [CORE,RTS]}
        selected,_=select_sources('0',sources,observed,[],framework='dora')
        self.assertEqual(len(selected),2)
        with self.assertRaises(SourceAdmissionError):
            select_sources('0',sources,observed,[])
        with self.assertRaisesRegex(SourceAdmissionError,'primary Regulation'):
            select_sources('0',[sources[1],{**sources[1],'url':'https://example.invalid/other'}],observed,[],framework='dora')

    def test_dora_live_source_path_accepts_primary_and_rts_with_exact_passages(self):
        case=source_fixtures.SourceResearchTests();case.setUp();self.addCleanup(case.doCleanups)
        case.server.discovery['sources']=[{**source_fixtures.metadata(u),'type':'law'} for u in [CORE,RTS]]
        case.server.outputs[0]=json.dumps({'results':[{'url':u} for u in [CORE,RTS]]})
        case.contents={CORE:'Regulation (EU) 2022/2554 Article 1 Subject matter. '+source_fixtures.PASSAGES[0]+' Article 64 Entry into force.',RTS:source_fixtures.PASSAGES[1]}
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=case.response
            admitted=research_scope(case.server,'0',{'keyTitle':'DORA scope','keyWords':'DORA'},source_fixtures.GROUPS,
                [],'Synthetic rules',case.work,time.monotonic()+60,'sources-0-1',{},fetch_source_snapshot,
                check_source_passages,framework='dora')
        self.assertEqual(admitted['frameworkId'],'dora')
        self.assertEqual(len(admitted['sources']),2)
        self.assertTrue(all(s['type']=='law' for s in admitted['sources'].values()))
        self.assertNotIn('Retrieve legal instruments only in the legal scope',case.server.calls[0]['prompt'])

    def test_observed_http_eli_alias_fetches_https_and_preserves_original_provenance(self):
        alias='http://data.europa.eu/eli/reg/2022/2554/oj'
        requested=alias.replace('http:','https:',1)
        case=source_fixtures.SourceResearchTests();case.setUp();self.addCleanup(case.doCleanups)
        case.server.discovery['sources']=[{**source_fixtures.metadata(u),'type':'law'} for u in [alias,RTS]]
        case.server.outputs[0]=json.dumps({'results':[{'url':u} for u in [alias,RTS]]})
        case.contents={requested:'Regulation (EU) 2022/2554 Article 1 Subject matter. '+source_fixtures.PASSAGES[0]+
            ' Article 64 Entry into force.',RTS:source_fixtures.PASSAGES[1]}
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=case.response
            admitted=research_scope(case.server,'0',{'keyTitle':'DORA scope','keyWords':'DORA'},
                source_fixtures.GROUPS,[],'Synthetic rules',case.work,time.monotonic()+60,'sources-0-1',{},
                fetch_source_snapshot,check_source_passages,framework='dora')
        source=admitted['sources']['0-source-1']
        self.assertEqual(source['url'],alias)
        self.assertEqual(source['finalUrl'],requested)
        self.assertEqual(case.fetches,[requested,RTS])
        self.assertIn(alias,admitted['observedResultUrls'])
        self.assertTrue(dora_core_source({**source,'finalUrl':'https://eur-lex.europa.eu/eli/reg/2022/2554/oj'}))
        self.assertFalse(dora_core_source({**source,'finalUrl':'https://unrelated.invalid/eli/reg/2022/2554/oj'}))

    def test_alias_does_not_admit_unobserved_or_spoofed_primary_regulations(self):
        alias='http://data.europa.eu/eli/reg/2022/2554/oj'
        for url in ('https://eur-lex.europa.eu/eli/reg/2022/2554/oj/eng/pdf',
                    'https://eur-lex.europa.eu/eli/reg/2022/2554/2025-01-17/eng',
                    'http://data.europa.eu/eli/reg/2022/2554/oj/eng/pdf',
                    'https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02022R2554-20250117'):
            self.assertTrue(dora_core_source({'type':'law','url':url}))
        sources=[{**source_fixtures.metadata(u),'type':'law'} for u in [alias,RTS]]
        with self.assertRaisesRegex(SourceAdmissionError,'not observed'):
            select_sources('0',sources,{url_identity(RTS)},[],framework='dora')
        for url in ('https://data.europa.eu.evil.invalid/eli/reg/2022/2554/oj',
                    'https://data.europa.eu/eli/reg/2022/2555/oj',
                    'https://eur-lex.europa.eu/unrelated?query=32022R2554'):
            self.assertFalse(dora_core_source({'type':'law','url':url}))

    def test_supporting_regulation_challenge_is_queued_and_scope_becomes_pending(self):
        from test_source_documents import CHALLENGE
        pipeline,_=fixture(self.root)
        case=source_fixtures.SourceResearchTests();case.setUp();self.addCleanup(case.doCleanups)
        case.server.discovery['sources']=[{**source_fixtures.metadata(u),'type':'law'} for u in [CORE,RTS]]
        case.server.outputs[0]=json.dumps({'results':[{'url':u} for u in [CORE,RTS]]})
        case.contents={CORE:'Regulation (EU) 2022/2554 Article 1 Subject matter. '+source_fixtures.PASSAGES[0]+
            ' Article 64 Entry into force.',RTS:CHALLENGE}
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=case.response
            with self.assertRaises(SourceDownloadPending) as failure:
                research_scope(case.server,'0',{'keyTitle':'DORA scope','keyWords':'DORA'},source_fixtures.GROUPS,
                    [],'Synthetic rules',case.work,time.monotonic()+60,'sources-0-1',{},
                    SourceCache(pipeline).fetch_snapshot,check_source_passages,framework='dora')
        self.assertTrue(failure.exception.details['retrievalFailures'][0]['retryableDownload'])
        queued=next((pipeline.root/'source-cache-outbox/download-retries').glob('*.json'))
        self.assertEqual(read(queued)['sourceUrl'],RTS)
        self.assertEqual(read(queued)['status'],'pending-download')

    def test_optional_portal_exclusion_retains_primary_regulation_and_supporting_law(self):
        portal='https://www.eba.europa.eu/synthetic-optional-portal'
        case=source_fixtures.SourceResearchTests();case.setUp();self.addCleanup(case.doCleanups)
        urls=[CORE,RTS,portal]
        case.server.discovery['sources']=[{**source_fixtures.metadata(u),'type':'law' if u!=portal else 'guidance'} for u in urls]
        case.server.outputs[0]=json.dumps({'results':[{'url':u} for u in urls]})
        case.contents={CORE:'Regulation (EU) 2022/2554 Article 1 Subject matter. '+source_fixtures.PASSAGES[0]+
            ' Article 64 Entry into force.',RTS:source_fixtures.PASSAGES[1],portal:'Synthetic portal navigation; no substantive supporting text.'}
        case.server.evidence['excludedSources']=[{'sourceId':'0-source-3','reason':'Redundant navigation portal; primary law and supporting standards retain coverage.'}]
        def research():
            with patch('ci.pipeline.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect=case.response
                return research_scope(case.server,'0',{'keyTitle':'DORA scope','keyWords':'DORA'},source_fixtures.GROUPS,
                    [],'Synthetic rules',case.work,time.monotonic()+60,'sources-0-1',{},fetch_source_snapshot,
                    check_source_passages,framework='dora')
        self.assertEqual(set(research()['sources']),{'0-source-1','0-source-2'})
        case.server.evidence['sources'][0]={'id':'0-source-3','passages':[{'id':'p1','locator':'Portal','text':case.contents[portal]}]}
        case.server.evidence['excludedSources']=[{'sourceId':'0-source-1','reason':'Attempt to exclude primary law.'}]
        with self.assertRaisesRegex(SourceAdmissionError,'Missing fetched full DORA'):
            research()

    def test_unavailable_primary_document_is_queued_instead_of_accepted(self):
        pipeline,_=fixture(self.root)
        case=source_fixtures.SourceResearchTests();case.contents={CORE:'JavaScript is disabled. Verify you are not a robot.'}
        case.fetches=[];case.retrieval_errors={};case.content_types={}
        with patch('ci.pipeline.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=case.response
            with self.assertRaisesRegex(OSError,'unavailable or incomplete'):
                SourceCache(pipeline).fetch_snapshot({**source_fixtures.metadata(CORE),'type':'law','id':'0-source-1'},
                    pipeline.work/'retry-snapshots',time.monotonic()+60)
        self.assertEqual(len(list((pipeline.root/'source-cache-outbox/download-retries').glob('*.json'))),1)

    def test_shared_searches_can_be_filtered_by_dora_framework(self):
        transcript=[{'parts':[{'type':'tool','tool':'searxng_search','state':{'status':'completed',
            'input':{'query':'DORA'},'output':json.dumps({'results':[{'url':CORE}]})}}]}]
        capture_searches(self.root,transcript,{'runId':'test','frameworkId':'dora','phase':'sources','scopeId':'0'},'2026-10-16T00:00:00Z')
        self.assertEqual(len(list(query_records(self.root,framework='dora',scope='0'))),1)
        self.assertEqual(list(query_records(self.root,framework='other')),[])

    def test_real_initializer_with_local_repositories_produces_dora_scaffold(self):
        original,_=create_fixture_pipeline(self.root/'workspace',publish=False)
        template=original.workspace/'uniq2me'
        shutil.copytree(original.work/'inputs',template)
        command(['git','init','-q','-b','main'],template)
        command(['git','add','.'],template)
        command(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','Synthetic templates'],template)
        command(['git','remote','add','origin','https://github.com/stefanospetsios/uniq2me.git'],template)
        command(['git','remote','set-url','origin','https://github.com/stefanospetsios/uniq2me-ai.git'],original.output)
        pipeline=DoraPipeline();pipeline.workspace=original.workspace;pipeline.output=original.output
        pipeline.root=original.workspace/'fresh-run';pipeline.work=pipeline.root/'work'
        pipeline.state_path=pipeline.root/'controller-state.json';pipeline.state={}
        environment={'CATALOG_PROFILE':'dora','REFINEMENT_PLAN':json.dumps({'sfs':'0','rework_questions':True,'complete_fields':False}),
            'TARGET_BRANCH':'en/dora-qwen3','DRY_RUN':'true','RAG_MODE':'disabled','SOURCE_CACHE_MODE':'off','PUBLISH':'false',
            'RELEASE':'synthetic-dora','UNIQ2ME_INPUT_COMMITS':'','UNIQ2ME_AI_INPUT_COMMITS':'','GITHUB_EVENT_NAME':'workflow_dispatch'}
        with patch.dict(os.environ,environment),patch('ci.pipeline.remote_head',return_value=None):
            pipeline.initialize();pipeline.sources();pipeline.refine('0');pipeline.publish_sfs('0')
        validate_profile_catalog(pipeline.output,load_profile())
        self.assertEqual(len(list((pipeline.output/'repo-sf/en').glob('*.json'))),256)
        self.assertEqual(read(pipeline.output/'catalog-profile.json')['frameworkId'],'dora')

    def test_intermediate_output_preserves_full_catalog_and_profile_marker(self):
        pipeline,_=fixture(self.root)
        pipeline.refine('0');pipeline.publish_sfs('0')
        validate_profile_catalog(pipeline.output,load_profile())
        self.assertEqual(len(list((pipeline.output/'json/en').glob('*.json'))),16)
        self.assertEqual(len(list((pipeline.output/'repo-sf/en').glob('*.json'))),256)
        self.assertFalse(read(pipeline.output/'repo-sf/en/0000.json')['isPlaceholder'])
        self.assertTrue(read(pipeline.output/'repo-sf/en/00F0.json')['isPlaceholder'])
        self.assertEqual(read(pipeline.output/'site-publication.json')['catalogProfile']['frameworkId'],'dora')

    @unittest.skipUnless(shutil.which('node') and (ROOT.parent/'uniq2me-pages/ci/assemble-site.mjs').exists(), 'Local website template checkout required')
    def test_generated_dora_catalog_assembles_with_existing_pages_tooling(self):
        pipeline,_=fixture(self.root)
        pipeline.refine('0');pipeline.publish_sfs('0')
        manifest=self.root/'catalog-sources.json'
        write(manifest,{'instance':'dora-qwen3','sources':[{'language':'en','ref':'en/dora-qwen3',
            'root':str(pipeline.output),'commit':command(['git','rev-parse','HEAD'],pipeline.output).stdout.strip()}]})
        pages=ROOT.parent/'uniq2me-pages';site=self.root/'site'
        result=subprocess.run(['node',str(pages/'ci/assemble-site.mjs'),'--sources',str(manifest),
            '--pages-source',str(pages),'--site-root',str(site)],capture_output=True,text=True,check=True)
        summary=json.loads(result.stdout)
        self.assertEqual(summary['catalogs'][0]['nativeSets'],16)
        self.assertEqual(summary['catalogs'][0]['nativeFactors'],256)
        self.assertIn('DORA:',read(site/'json/en/0.json')['keyTitle'])
        environment={**os.environ,'UNIQ2ME_INSTANCE':'dora-qwen3','UNIQ2ME_LANGUAGE':'en',
            'UNIQ2ME_SITE_MODE':'language','UNIQ2ME_CATALOG_ENABLED':'true','UNIQ2ME_REPOSITORY_ENABLED':'false'}
        subprocess.run(['node',str(pages/'ci/configure-site.mjs'),'--site-root',str(site)],
            capture_output=True,text=True,check=True,env=environment)
        checked=subprocess.run(['node',str(pages/'ci/check-site.mjs'),'--site-root',str(site)],
            capture_output=True,text=True,env=environment)
        self.assertEqual(checked.returncode,0,checked.stderr)
        self.assertEqual(read(site/'site-build-settings.json')['siteUrl'],
            'https://dora-qwen3.en.uniq2me.petsios.gr')


if __name__=='__main__':
    unittest.main()
