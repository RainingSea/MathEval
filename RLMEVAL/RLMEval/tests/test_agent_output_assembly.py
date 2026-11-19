import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eval_agent_outputs', ROOT / 'scripts/eval_agent_outputs.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)
REPORT_SPEC = importlib.util.spec_from_file_location('summarize_codex_run', ROOT / 'scripts/summarize_codex_run.py')
report = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(report)


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.items = [{'index': i, 'repo': 'PFR', 'name': f'target{i}', 'label': f'label{i}',
                       'file': 'Example.lean', 'context': 'namespace Example\n',
                       'decl': f'theorem target{i} : True := by', 'informal_proof': 'Trivial.'} for i in (1, 2)]
        self.dataset = self.root / 'dataset.jsonl'
        module.dump_rows(self.dataset, self.items)

    def tearDown(self):
        self.temp.cleanup()

    def record(self, index=1, sample=0):
        return {'run_id': 'result', 'sample_id': sample, 'item_index': index, 'item_repo': 'PFR',
                'source_format': 'declaration', 'status': 'ok',
                'raw_text': self.items[index - 1]['decl'] + '\n  trivial'}

    def test_candidate_directory_follows_mounted_results_and_includes_missing_items(self):
        actual = self.root / 'mounted-result'
        (actual / 'input').mkdir(parents=True)
        (actual / 'input/Candidate.lean').write_text('seed')
        candidate = actual / 'Candidate.lean'
        candidate.write_text(self.record()['raw_text'])
        source = self.root / 'outputs'
        (source / 'task-1').mkdir(parents=True)
        (source / 'task-1/result').symlink_to(actual, target_is_directory=True)
        output = self.root / 'evaluation'

        manifest = module.assemble_candidates(source, self.dataset, output)
        predictions = module.rows(output / 'generations.jsonl')
        self.assertEqual(manifest['missing_items'], [2])
        self.assertEqual(predictions[0]['raw_text'], candidate.read_text())
        self.assertEqual(predictions[1]['status'], 'missing_candidate')
        self.assertEqual(predictions[1]['raw_text'], '')

    def test_candidate_directory_keeps_generation_errors_and_uses_candidate_file(self):
        source = self.root / 'outputs'
        result = source / 'task-1/result'
        sidecar = dict(self.record(), status='agent_timeout', termination_reason='timeout',
                       error_detail='HTTP 503: service unavailable', exit_code=1, raw_text='stale snapshot')
        module.dump(result / 'generation.json', sidecar)
        (result / 'Candidate.lean').write_text(self.record()['raw_text'])
        module.dump_rows(source / 'generations.jsonl', [self.record(), dict(self.record(2),
                         status='model_error', error_detail='HTTP 402: insufficient balance')])
        output = self.root / 'evaluation'
        module.assemble_candidates(source, self.dataset, output)
        first, second = module.rows(output / 'generations.jsonl')
        self.assertEqual(first['raw_text'], self.record()['raw_text'])
        self.assertEqual(first['status'], 'agent_timeout')
        self.assertEqual(first['error_detail'], 'HTTP 503: service unavailable')
        self.assertEqual(second['status'], 'model_error')
        self.assertFalse(second['candidate_present'])
        self.assertEqual(second['error_detail'], 'HTTP 402: insufficient balance')

    def test_candidate_directory_keeps_multiple_samples_and_rejects_conflicts(self):
        source = self.root / 'outputs'
        for name in ('000001-0000', '000001-0001'):
            folder = source / name
            folder.mkdir(parents=True)
            (folder / 'candidate.lean').write_text(name)
        output = self.root / 'evaluation'
        module.assemble_candidates(source, self.dataset, output)
        predictions = module.rows(output / 'generations.jsonl')
        self.assertEqual([(r['item_index'], r['sample_id']) for r in predictions], [(1, 0), (1, 1), (2, 0)])
        (source / '1').mkdir()
        (source / '1/Candidate.lean').write_text('different candidate')
        with self.assertRaisesRegex(ValueError, 'Conflicting candidates'):
            module.assemble_candidates(source, self.dataset, self.root / 'conflict')

    def test_single_mounted_generation_preserves_standard_input(self):
        folder = self.root / 'mount/result'
        record = self.record()
        module.dump(folder / 'generation.json', record)
        module.dump_rows(folder / 'generations.jsonl', [record])
        output = self.root / 'evaluation'
        manifest = module.assemble([folder.parent], self.dataset, output)
        self.assertEqual(manifest['prediction_count'], 1)
        self.assertEqual(module.rows(output / 'generations.jsonl')[0]['raw_text'], record['raw_text'])
        self.assertEqual(module.rows(output / 'source_generations.jsonl'), [record])

    def test_uneven_sample_counts_are_not_padded(self):
        source = self.root / 'generations.jsonl'
        module.dump_rows(source, [self.record(1, 0), self.record(1, 1), self.record(2, 0)])
        manifest = module.assemble([source], self.dataset, self.root / 'evaluation')
        self.assertEqual(manifest['sample_counts'], {1: 2, 2: 1})
        self.assertEqual(manifest['prediction_count'], 3)

    def test_same_run_name_in_distinct_mounts_retains_both_samples(self):
        for directory in ('a', 'b'):
            module.dump(self.root / directory / 'generation.json', self.record())
        self.assertEqual(len(module.read_generations([self.root / 'a', self.root / 'b'])), 2)

    def test_conflicting_duplicate_sample_is_reported(self):
        record = self.record()
        source = self.root / 'generations.jsonl'
        module.dump_rows(source, [record, dict(record, raw_text='other')])
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate'):
            module.read_generations([source])

    def test_sorry_and_modified_statement_reach_existing_scorer_unchanged(self):
        records = [dict(self.record(), raw_text=self.items[0]['decl'] + '\n  sorry'),
                   dict(self.record(sample=1), raw_text='theorem changed : False := by trivial')]
        source = self.root / 'generations.jsonl'
        module.dump_rows(source, records)
        output = self.root / 'evaluation'
        module.assemble([source], self.dataset, output)
        self.assertEqual([r['raw_text'] for r in module.rows(output / 'generations.jsonl')],
                         [r['raw_text'] for r in records])

    def test_container_event_path_is_relocated_for_existing_api_error_report(self):
        folder = self.root / 'mount/result'
        source = folder / 'generation.json'
        record = dict(self.record(), status='agent_error', exit_code=1,
                      workspace='/outputs/result/workspace', events_path='/outputs/result/workspace/.archon/logs')
        module.dump(source, record)
        event = {'type': 'turn.failed', 'error': {'message': 'HTTP 429: rate limit exceeded'}}
        log = folder / 'workspace/.archon/logs/iter-001/plan.codex-raw.jsonl'
        log.parent.mkdir(parents=True)
        log.write_text(json.dumps(event) + '\n{"truncated":')
        output = self.root / 'evaluation'
        module.assemble([source], self.dataset, output)
        row = module.rows(output / 'generations.jsonl')[0]
        self.assertEqual(report.generation_error_reason(row), 'HTTP 429: rate limit exceeded')
        details = module.rows(output / 'generation_diagnostics.jsonl')[0]
        self.assertEqual(details['error_events'][0]['event'], event)
        self.assertEqual(len(details['invalid_event_lines']), 1)
        self.assertEqual(row['status'], 'agent_error')

    def test_api_error_detail_without_event_file_reaches_existing_report(self):
        source = self.root / 'generation.json'
        module.dump(source, dict(self.record(), status='api_error', exit_code=1,
                                 error_detail='HTTP 401: invalid API key'))
        output = self.root / 'evaluation'
        module.assemble([source], self.dataset, output)
        generation = module.rows(output / 'generations.jsonl')[0]
        self.assertEqual(generation['error_detail'], 'HTTP 401: invalid API key')
        self.assertEqual(report.generation_error_reason(generation), 'HTTP 401: invalid API key')
        event = module.rows(Path(generation['events_path']))[0]
        self.assertEqual(event['source'], 'generation.error_detail')

    def test_full_task_is_split_without_changing_the_target(self):
        record = self.record()
        expected = record['raw_text']
        record['raw_text'] = self.items[0]['context'] + '\n' + expected
        module.dump(self.root / 'generation.json', record)
        output = self.root / 'evaluation'
        module.assemble([self.root / 'generation.json'], self.dataset, output)
        self.assertEqual(module.rows(output / 'generations.jsonl')[0]['raw_text'], expected)

    def test_evaluation_calls_existing_cli_and_report_without_reformatting_scores(self):
        output = self.root / 'evaluation'
        output.mkdir()
        module.dump(output / 'summary.json', {'score_statuses': {'statement_modified': 1}})
        with patch.object(module.subprocess, 'run') as run:
            result = module.evaluate(output, self.root / 'elan')
        self.assertEqual(run.call_count, 2)
        score_command = run.call_args_list[0].args[0]
        self.assertEqual(score_command[1:6], ['-m', 'rlm_agent.cli', 'score', '--task', 'proof'])
        self.assertIn(str(ROOT / 'scripts/summarize_codex_run.py'), run.call_args_list[1].args[0])
        self.assertEqual(result['score_statuses'], {'statement_modified': 1})


if __name__ == '__main__':
    unittest.main()
