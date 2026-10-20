"""Retained URL provenance is scoped, immutable and derived from actual tool results."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ci.contracts import read,write
from ci.source_observations import history,remember

GROUPS={'searxng':['searxng_search'],'websearch':['websearch_search']}


class SourceObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'work'
        self.server=SimpleNamespace(archive=self.root/'runtime')
        self.path=self.server.archive/'calls/sources-0-1-discovery/messages.json'

    def record(self,url):
        write(self.path,[{'parts':[{'type':'tool','tool':tools[0],'state':{'status':'completed',
            'input':{'query':'synthetic'},'output':{'url':url}}} for tools in GROUPS.values()]}])
        remember(self.server,self.work,'0','sources-0-1-discovery',GROUPS)

    def test_reusing_call_path_preserves_both_independently_observed_results(self):
        self.record('https://example.invalid/first')
        self.record('https://example.invalid/second')
        observed,queries=history(self.work,'0',GROUPS)
        self.assertEqual(observed,{'https://example.invalid/first','https://example.invalid/second'})
        self.assertEqual(len(queries),4)
        self.assertTrue(all(q['reusedScopeObservation'] for q in queries))
        self.assertEqual(history(self.work,'1',GROUPS),(set(),[]))

    def test_changed_retained_transcript_is_an_integrity_failure(self):
        self.record('https://example.invalid/first')
        record=read(next((self.work/'discovery-observations/0').glob('*.json')))
        write(self.root/record['transcriptRef'],[{'parts':[]}])
        with self.assertRaisesRegex(ValueError,'transcript changed'):
            history(self.work,'0',GROUPS)

    def test_missing_search_group_does_not_create_reusable_provenance(self):
        write(self.path,[{'parts':[{'type':'tool','tool':'searxng_search','state':{
            'status':'completed','input':{},'output':{'url':'https://example.invalid/first'}}}]}])
        remember(self.server,self.work,'0','sources-0-1-discovery',GROUPS)
        self.assertEqual(history(self.work,'0',GROUPS),(set(),[]))

    def test_completed_reads_of_saved_truncated_search_results_are_retained(self):
        self.record('https://example.invalid/first')
        transcript=read(self.path)
        transcript[0]['parts'].append({'type':'tool','tool':'filesystem_read_file','state':{
            'status':'completed','input':{'path':'synthetic-saved-tool-output'},
            'output':{'results':[{'url':'https://example.invalid/truncated-result'}]}}})
        transcript[0]['parts'].append({'type':'text','text':'https://example.invalid/model-claim'})
        write(self.path,transcript)
        remember(self.server,self.work,'0','sources-0-1-discovery',GROUPS)
        observed,_=history(self.work,'0',GROUPS)
        self.assertEqual(observed,{'https://example.invalid/first','https://example.invalid/truncated-result'})
