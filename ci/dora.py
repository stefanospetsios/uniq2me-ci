"""DORA taxonomy binding with unchanged UNIQ2Me record layout and scoring semantics."""
import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci.contracts import HEX, digest, file_manifest, read, require, write
from ci.refinement import RefinementPipeline, environment
from ci.progress import log

PROFILE = Path(__file__).resolve().parents[1] / 'profiles/dora.json'


def load_profile(path=PROFILE):
    profile = read(path)
    return validate_profile(profile)


def validate_profile(profile):
    require(profile.get('frameworkId') == 'dora' and profile.get('methodologyId') == 'uniq2me', 'Wrong catalog profile')
    require(profile.get('sfsCount') == 16 and profile.get('sfPerSfs') == 16 and
            profile.get('questionMaxCharacters') == 512, 'DORA must retain the UNIQ2Me 16x16/512-character contract')
    require([a['id'] for a in profile['areas']] == list(HEX), 'DORA needs exactly sixteen ordered SFSs 0–F')
    sources = {s['id']: s for s in profile['sources']}
    require(len(sources) == len(profile['sources']) and 'dora' in sources, 'DORA source registry is malformed')
    for area in profile['areas']:
        require(all(isinstance(area.get(k), str) and area[k].strip() for k in ('title','summary','boundaries','keywords')),
                'DORA scope text is missing: ' + area['id'])
        require(bool(area['articles']) and set(area['sourceIds']) <= set(sources) and 'dora' in area['sourceIds'],
                'DORA article/source mapping is missing: ' + area['id'])
    return profile


def generated_profile(proposal):
    profile = copy.deepcopy(load_profile())
    profile['baseProfileDigest'] = digest(profile)
    profile['areas'] = [{**area, 'keywords': ', '.join(area['keywords'])} for area in proposal['areas']]
    profile['id'] = 'dora-generated-' + digest(profile['areas'])[7:]
    return validate_profile(profile)


def profile_for_catalog(root):
    path = Path(root) / 'catalog/taxonomy/dora-profile.json'
    if not path.exists():
        return load_profile()
    profile = validate_profile(read(path))
    original = load_profile()
    require(profile.get('baseProfileDigest') == digest(original), 'Generated DORA profile has a different regulatory foundation')
    require({k: v for k, v in profile.items() if k not in ('id', 'areas', 'baseProfileDigest')} ==
            {k: v for k, v in original.items() if k not in ('id', 'areas')},
            'Generated DORA profile changed protected regulatory requirements')
    require(profile['id'] == 'dora-generated-' + digest(profile['areas'])[7:], 'Generated DORA profile identity changed')
    from ci.taxonomy import validate_taxonomy
    proposal = read(Path(root) / 'catalog/taxonomy/areas.json')
    validate_taxonomy(proposal, framework='dora')
    require(profile == generated_profile(proposal), 'Generated DORA areas differ from their proposal')
    return profile


def marker(profile):
    return {'schemaVersion': 1, 'recordType': 'catalog-profile', 'frameworkId': 'dora',
        'methodologyId': 'uniq2me', 'profileId': profile['id'], 'profileVersion': profile['profileVersion'],
        'taxonomyDigest': digest([profile['areas'], profile['applicabilityPolicy']]),
        'profileDigest': digest(profile), 'sfsCount': 16, 'sfPerSfs': 16,
        'status': 'draft-authoring-taxonomy', 'taxonomyIsOfficial': False,
        'humanApproved': False, 'assessmentPerformed': False}


def description(area):
    return area['summary'] + ' ' + area['boundaries'] + ' DORA article references: ' + ', '.join(area['articles']) + '.'


def blank_signers(value):
    if isinstance(value, dict):
        return {k: (blank_signers(v) if isinstance(v, (dict,list)) else False if k == 'isBiased' else None)
                for k,v in value.items()}
    return [blank_signers(v) for v in value]


def seed_catalog(root, profile, slots, branch):
    """Build all placeholders from real schema templates; never reuse generic assessment content."""
    root = Path(root)
    for area in profile['areas']:
        path = root / 'json/en' / (area['id'] + '.json')
        record = read(path)
        record.update(keyTitle=area['title'], keyShortInfo=area['summary'], keyDescription=description(area),
            keyWords='DORA, Regulation (EU) 2022/2554, ' + area['keywords'], generate=True,
            promptAi='Develop sixteen bounded yes/no factors for ' + area['title'] + '. ' + description(area) +
                ' Verify entity-role applicability and current primary sources. Missing evidence stays pending; no compliance or assessment is implied.')
        record['meta'].update(title=area['title'], menuTitle=area['title'].removeprefix('DORA: '))
        if 'resultScore' in record:
            record['resultScore'] = None
        for entry in record['set']:
            entry['score'] = None
            entry['signer'] = blank_signers(entry['signer'])
        write(path, record)
        for slot in (s for s in slots if s['sfsId'] == area['id']):
            sf_path = root / 'repo-sf/en' / (slot['repoId'] + '.json')
            sf = read(sf_path)
            sf['question'].update(shortBinaryQuestion='[Placeholder] Has DORA condition ' + slot['sfId'] +
                ' in "' + area['title'] + '" been evidenced?', plain='DORA authoring placeholder; this is not an assessment.',
                **{'technical-short':'Define the applicable entity role, condition, evidence period and decision rule from verified DORA sources.'},
                keywords=record['keyWords'], promptAiOptimization=record['promptAi'])
            sf.update(isPlaceholder=True, resultScore=None, signatures=blank_signers(sf['signatures']),
                review={'status':'draft','approvals':[]}, clarificationQuestion=[], wikiLinks=[],
                openSourceApproach=None)
            sf['meta']['title'] = 'DORA SFS ' + area['id'] + ' / SF ' + slot['sfId'] + ' (placeholder)'
            for key, action in [('editLink','edit'),('jsonLink','blob')]:
                sf[key] = 'https://github.com/stefanospetsios/uniq2me-ai/' + action + '/' + branch + '/repo-sf/en/' + slot['repoId'] + '.json'
            for key in ('glacierWiki','glacierLink','glacerQRcode','linkLatest'):
                if key in sf:
                    sf[key] = '' if isinstance(sf[key], str) else None
            write(sf_path, sf)
    write(root / 'catalog-profile.json', marker(profile))


def validate_profile_catalog(root, profile):
    root = Path(root)
    stored = read(root / 'catalog-profile.json')
    expected = marker(profile)
    binding = ('schemaVersion', 'recordType', 'frameworkId', 'methodologyId', 'profileId',
               'profileVersion', 'taxonomyDigest', 'sfsCount', 'sfPerSfs')
    require(all(stored.get(key) == expected[key] for key in binding),
            'Catalog belongs to another framework or DORA taxonomy; it cannot be a DORA base')
    for area in profile['areas']:
        record = read(root / 'json/en' / (area['id'] + '.json'))
        require(record['keyTitle'] == area['title'] and record['keyDescription'] == description(area),
                'DORA scope changed at SFS ' + area['id'])


def plan_input(raw):
    value = json.loads(raw or '{}') if isinstance(raw, str) else copy.deepcopy(raw)
    require(isinstance(value, dict), 'Refinement inputs must be an object')
    if 'question_mode' not in value and 'rework_questions' not in value and 'complete_fields' not in value:
        value.update(rework_questions=True, complete_fields=False)
    return value


class DoraPipeline(RefinementPipeline):
    def initialize(self):
        target = os.environ.get('TARGET_BRANCH') or 'en/dora-qwen3'
        instance = target.removeprefix('en/')
        require(bool(re.fullmatch(r'(?:en/)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?', target)) and
                len(instance) <= 63 and any(re.fullmatch(r'dora[0-9]*', part) for part in instance.split('-')),
                'Use a DORA instance branch such as dora or en/dora-qwen3')
        with environment({'TARGET_BRANCH': target, 'REFINEMENT_PLAN': json.dumps(plan_input(os.environ.get('REFINEMENT_PLAN', '{}')))}):
            super().initialize()
        if self.state['refinementPlan'].get('maskRequest'):
            config = read(self.work / 'agent-rules/run-config.json')
            config['masks'].update(scopeRef='DORA refinementPlan.maskRequest', jurisdictions=['EU'])
            write(self.work / 'agent-rules/run-config.json', config)
            self.state['rulesManifest'] = file_manifest(self.work / 'agent-rules')
            self.save()

    def source_profile(self):
        return profile_for_catalog(self.work / 'base')

    def source_area(self, scope):
        if scope != 'legal':
            return super().source_area(scope)
        request = self.state['refinementPlan'].get('maskRequest', {})
        return {'keyTitle': 'DORA applicability and source-linked requirement masks',
            'keyDescription': 'Verify the current full Regulation (EU) 2022/2554, relevant binding RTS/ITS '
                'and official supervisory guidance at the frozen cutoff. ' + self.source_profile()['applicabilityPolicy'] +
                ' Requested mask research: ' + request.get('instructions', ''),
            'keyWords': 'DORA, Regulation (EU) 2022/2554, financial entities, ICT third-party providers, applicability'}

    def seed_generated_taxonomy(self, proposal):
        profile = generated_profile(proposal)
        for root in (self.work / 'base', self.work):
            seed_catalog(root, profile, self.state['slots'], self.state['branch'])
            write(root / 'catalog/taxonomy/dora-profile.json', profile)
        return marker(profile)

    def prepare_profile_inputs(self):
        profile = load_profile()
        if self.state['expectedRemote']:
            require((self.output / 'catalog-profile.json').exists(), 'Destination already contains an unbound catalog; choose a new DORA branch')
            validate_profile_catalog(self.output, profile_for_catalog(self.output))
        inputs = self.work / 'inputs'
        seed_catalog(inputs, profile, self.state['slots'], self.state['branch'])
        write(inputs / 'dora-profile.json', profile)
        guide = (self.trusted / 'methodology/dora-profile.md').read_bytes()
        (inputs / 'dora-profile.md').write_bytes(guide)
        self.state.update(catalogProfile=marker(profile), inputManifest=file_manifest(inputs))
        config = read(self.work / 'agent-rules/run-config.json')
        config['catalogProfile'] = self.state['catalogProfile']
        config['methodologyProfileRef'] = 'inputs/dora-profile.md'
        config['sourceDiscovery'].update(keywordScope='selected-dora-sfs', officialSourceSeedsScope='selected-dora-sfs',
            keywords=['DORA','Regulation (EU) 2022/2554'], officialSourceSeeds=[s['url'] for s in profile['sources']])
        config['masks']['scopeRef'] = 'DORA catalog-bound applicability; no MASK generation selected'
        write(self.work / 'agent-rules/run-config.json', config)
        self.state['rulesManifest'] = file_manifest(self.work / 'agent-rules')
        self.save()

    def select_profile_base(self, preferred):
        profile = load_profile()
        references = self.state.get('referenceInputs', {})
        snapshots = read(self.work / references['manifestRef'])['snapshots'] if references else []
        reference_only = []
        for repository in ('stefanospetsios/uniq2me-ai','stefanospetsios/uniq2me'):
            matches = [s for s in snapshots if s['repository'] == repository]
            if not matches:
                continue
            chosen = next((s for s in matches if s['commit'] == preferred.get(repository)), matches[-1])
            root = self.work / chosen['root']
            if (root / 'catalog-profile.json').exists():
                chosen_profile = profile_for_catalog(root)
                validate_profile_catalog(root, chosen_profile)
                self.state['catalogProfile'] = marker(chosen_profile)
                self.state['referenceOnlyCatalogs'] = reference_only
                return root, {k:chosen[k] for k in ('repository','commit','root','snapshotDigest')}
            reference_only.append({'repository':repository,'commit':chosen['commit'],'reason':'No DORA catalog profile; reference only'})
        self.state['referenceOnlyCatalogs'] = reference_only
        log('dora', 'Starting from complete DORA placeholders; non-DORA commits remain reference inputs')
        return self.work / 'inputs', {'repository':self.state.get('controllerRepository') or 'stefanospetsios/uniq2me-ci',
            'commit':self.state['controllerCommit'],'root':'inputs','templateInputCommit':self.state['inputCommit'],
            'profile':marker(profile)}

    def pinned(self):
        with environment({'REFINEMENT_PLAN': json.dumps(plan_input(os.environ['REFINEMENT_PLAN']))} if os.environ.get('REFINEMENT_PLAN') else {}):
            super().pinned()
        if self.state.get('catalogProfile'):
            profile = self.source_profile()
            require(marker(profile) == self.state['catalogProfile'], 'Pinned DORA profile changed')
            validate_profile_catalog(self.work / 'base', profile)

    def instructions(self):
        profile = self.source_profile()
        profile_ref = ('base/catalog/taxonomy/dora-profile.json' if 'baseProfileDigest' in profile
                       else 'inputs/dora-profile.json')
        return super().instructions() + ('\nDORA PROFILE OVERRIDE: retain the UNIQ2Me mathematics, 16x16 positions, '
            'binary evidence decisions and JSON schema, but use the sixteen DORA areas in ' + profile_ref + '. '
            'They replace the original manuscript\'s cybersecurity demonstration allocation. '
            'NIS2/Greek Law 5160 are not mandatory DORA instruments. Read inputs/dora-profile.md. '
            + profile['applicabilityPolicy'] + ' This taxonomy is an authored draft, not an official taxonomy, '
            'legal opinion, human approval or a compliance certificate. Citations and proposed implementations '
            'require current verified sources; no operational execution is implied.\n')

    def write_publication_status(self, sfs):
        super().write_publication_status(sfs)
        write(self.work / 'catalog-profile.json', self.state['catalogProfile'])
        for name in ('refinement-status.json','site-publication.json'):
            record = read(self.work / name)
            record.update(catalogProfile=self.state['catalogProfile'], referenceOnlyCatalogs=self.state.get('referenceOnlyCatalogs', []))
            write(self.work / name, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check-profile'])
    parser.parse_args()
    profile = load_profile()
    print('DORA profile valid: sixteen SFSs / 256 SF positions; no questions or scores generated.')
    for area in profile['areas']:
        print(area['id'] + ': ' + area['title'])


if __name__ == '__main__':
    main()
