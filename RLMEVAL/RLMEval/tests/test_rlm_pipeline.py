import json
from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from rlm_pipeline.archon_runner import collect_generation, resolve_lean, prepare_archon
from rlm_pipeline.credentials import resolve_api
from rlm_pipeline.inputs import export_item, load_item, write_json
from rlm_pipeline.task_bundle import file_digest, load_task, prepare_task

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker/archon'))
from prepare_credentials import read_profiles


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.item = {'index': 161, 'repo': 'TLB', 'name': 'target', 'label': 'lem:target',
                     'file': 'Example.lean', 'context': 'namespace Example\n',
                     'context_normal': 'wrong normal context', 'decl': 'lemma target : True := by',
                     'informal_stmt': 'The target is true.', 'informal_proof': 'Trivial.'}

    def tearDown(self):
        self.temporary.cleanup()

    def test_api_profile_selects_paired_key_and_url(self):
        path = self.root / 'profiles.json'
        write_json(path, {'zhipu-2': {'api_key': 'second-key', 'base_url': 'https://official.example/v1'}})
        args = Namespace(api_profile='zhipu-2', api_key_env='ARCHON_API_KEY', api_base=None)
        with patch.dict('os.environ', {'RLM_API_PROFILES': str(path), 'ARCHON_API_KEY': 'unrelated-key',
                                       'ARCHON_API_BASE': 'https://unrelated.example/v1'}):
            self.assertEqual(resolve_api(args), ('second-key', 'https://official.example/v1'))
            args.api_base = 'https://override.example/v1'
            self.assertEqual(resolve_api(args), ('second-key', args.api_base))

    def test_env_profile_retains_existing_key_loading(self):
        args = Namespace(api_profile='env', api_key_env='CUSTOM_KEY', api_base=None)
        with patch.dict('os.environ', {'CUSTOM_KEY': 'provided-key', 'ARCHON_API_BASE': 'https://custom.example/v1'}, clear=True):
            self.assertEqual(resolve_api(args), ('provided-key', 'https://custom.example/v1'))
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'CUSTOM_KEY'):
                resolve_api(args)

    def test_missing_baked_profile_is_an_error(self):
        path = self.root / 'profiles.json'
        write_json(path, {})
        args = Namespace(api_profile='zhipu', api_key_env='ARCHON_API_KEY', api_base=None)
        with patch.dict('os.environ', {'RLM_API_PROFILES': str(path)}):
            with self.assertRaisesRegex(ValueError, 'not included'):
                resolve_api(args)

    def test_prepare_workspace_selects_requested_engine(self):
        for harness in ('codex', 'claude-code'):
            with self.subTest(harness=harness):
                output = self.root / harness
                output.mkdir()
                args = Namespace(harness=harness, codex_bin='codex', model='model-id',
                                 max_iterations=3, review=False, sandbox='danger-full-access',
                                 reasoning_effort='high', input=self.root, elan_home=self.root,
                                 envs_root=self.root)
                def which(name):
                    expected = 'claude' if harness == 'claude-code' else 'codex'
                    self.assertEqual(name, expected)
                    return '/usr/local/bin/' + name
                with patch('rlm_pipeline.archon_runner.shutil.which', side_effect=which), \
                     patch('rlm_pipeline.archon_runner.resolve_lean', return_value=('', '', 'leanprover/lean4:v4.14.0-rc3')):
                    workspace, _ = prepare_archon(self.item, output, args)
                cfg = json.loads((workspace / '.archon/config.json').read_text())
                self.assertEqual(cfg['loop']['harness'], harness)
                self.assertEqual(cfg['harnesses'][harness]['runner'], harness)
                self.assertEqual(cfg['harnesses'][harness]['model'], 'model-id')

    def test_cc_requires_separate_explicit_base_url(self):
        args = Namespace(harness='claude-code', api_profile='env', api_key_env='ARCHON_API_KEY', api_base=None)
        with patch.dict('os.environ', {'ARCHON_API_KEY': 'key', 'ARCHON_API_BASE': 'https://responses.example/v1'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Anthropic Messages'):
                resolve_api(args)

    def test_cc_env_accepts_anthropic_credentials(self):
        args = Namespace(harness='claude-code', api_profile='env', api_key_env='ARCHON_API_KEY', api_base=None)
        with patch.dict('os.environ', {'ANTHROPIC_AUTH_TOKEN': 'cc-key', 'ANTHROPIC_BASE_URL': 'https://messages.example/api/anthropic'}, clear=True):
            self.assertEqual(resolve_api(args), ('cc-key', 'https://messages.example/api/anthropic'))
            args.api_base = 'https://explicit.example'
            self.assertEqual(resolve_api(args), ('cc-key', args.api_base))

    def test_cc_profile_keeps_key_and_overrides_codex_url(self):
        path = self.root / 'profiles.json'
        write_json(path, {'zhipu': {'api_key': 'baked-key', 'base_url': 'https://responses.example/v1'}})
        args = Namespace(harness='claude-code', api_profile='zhipu', api_key_env='ARCHON_API_KEY', api_base='https://messages.example/api/anthropic')
        with patch.dict('os.environ', {'RLM_API_PROFILES': str(path)}, clear=True):
            self.assertEqual(resolve_api(args), ('baked-key', args.api_base))

    def test_secret_rules_pair_zhipu_keys_with_codex_url(self):
        path = self.root / 'secret.txt'
        text = ''
        for number, heading in enumerate(('外部中转', '内部中转', '自己平台', '反代平台')):
            text += f"## {heading}\nexport OPENAI_API_KEY='key-{number}'\nexport OPENAI_BASE_URL='https://service-{number}.example/v1'\n"
        text += ('## 智谱URL\ncodex：https://official.example/api/v1\n'
                 'claude code: https://official.example/api/anthropic\n'
                 '## 智谱API KEY\nfirst-key\n## 智谱API KEY （第二个）\nsecond-key\n')
        path.write_text(text)
        profiles = read_profiles(path)
        self.assertEqual(len(profiles), 6)
        self.assertEqual(profiles['zhipu']['api_key'], 'first-key')
        self.assertEqual(profiles['zhipu-2']['api_key'], 'second-key')
        self.assertEqual(profiles['zhipu-2']['base_url'], 'https://official.example/api/v1')
        self.assertEqual(profiles['internal']['base_url'], 'https://service-1.example/v1')
        path.write_text(text.replace('second-key', ''))
        with self.assertRaisesRegex(ValueError, 'zhipu-2'):
            read_profiles(path)

    def test_export_uses_index_and_exact_easy_context(self):
        dataset = self.root / 'items.jsonl'
        dataset.write_text(json.dumps(self.item) + '\n')
        item = load_item(dataset, 161)
        export_item(item, self.root / 'input')
        self.assertEqual((self.root / 'input/Context.lean').read_text(), self.item['context'])
        self.assertEqual((self.root / 'input/informal_proof.md').read_text(), 'Trivial.')
        with self.assertRaises(ValueError):
            load_item(dataset, 1)
        dataset.write_text(dataset.read_text() * 2)
        with self.assertRaises(ValueError):
            load_item(dataset, 161)

    def bundle(self):
        directory = self.root / 'task'
        export_item(self.item, directory)
        (directory / 'lib').mkdir()
        (directory / 'lean-toolchain').write_text('leanprover/lean4:v4.13.0-rc3\n')
        write_json(directory / 'dependencies.json', {'schema_version': 1, 'item_index': 161,
                   'toolchain': 'leanprover/lean4:v4.13.0-rc3', 'files': {}})
        return directory

    def test_single_task_bundle_round_trip(self):
        item = load_task(self.bundle())
        self.assertEqual(item['index'], 161)
        self.assertEqual(item['context'], self.item['context'])
        self.assertNotIn('context_normal', item)

    def test_missing_informal_proof_is_excluded(self):
        self.item['informal_proof'] = ''
        with self.assertRaisesRegex(ValueError, 'excluded'):
            prepare_task(self.item, self.root / 'export', self.root / 'projects', self.root / 'elan')
        with self.assertRaisesRegex(ValueError, 'excluded'):
            load_task(self.bundle())

    def test_shared_image_environment_must_match_task_toolchain(self):
        directory = self.bundle()
        elan = self.root / 'elan'
        binary = elan / 'toolchains/leanprover--lean4---v4.13.0-rc3/bin/lean'
        binary.parent.mkdir(parents=True)
        binary.write_text('fake binary')
        environment = self.root / 'environment'
        (environment / 'lib').mkdir(parents=True)
        version = environment / 'lean-toolchain'
        version.write_text('leanprover/lean4:v4.14.0-rc3')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            resolve_lean(directory, elan, environment)
        version.write_text('leanprover/lean4:v4.13.0-rc3')
        _, lean_path, _ = resolve_lean(directory, elan, environment)
        self.assertEqual(lean_path.split(':'), [str(directory / 'lib'), str(environment / 'lib')])

    def test_export_copies_only_resolved_import_closure(self):
        project = self.root / 'projects/tlb'
        library = project / '.lake/build/lib'
        library.mkdir(parents=True)
        (project / '.lake/packages').mkdir(parents=True)
        (project / 'lean-toolchain').write_text('leanprover/lean4:v4.13.0-rc3\n')
        allowed = library / 'Allowed.olean'
        allowed.write_bytes(b'allowed module')
        (library / 'OtherTask.olean').write_bytes(b'other task answer')
        self.item['context'] = 'import Allowed\n'
        resolved = subprocess.CompletedProcess([], 0, f'Allowed\t{allowed}\n', '')
        with patch('rlm_pipeline.task_bundle.subprocess.run', return_value=resolved):
            prepare_task(self.item, self.root / 'export', self.root / 'projects', self.root / 'elan')
        self.assertEqual(load_task(self.root / 'export')['index'], 161)
        self.assertFalse((self.root / 'export/lib/OtherTask.olean').exists())
        resolved.stdout = f'Example\t{library / "Example.olean"}\n'
        with patch('rlm_pipeline.task_bundle.subprocess.run', return_value=resolved):
            with self.assertRaisesRegex(ValueError, 'compiled answer'):
                prepare_task(self.item, self.root / 'rejected', self.root / 'projects', self.root / 'elan')
        self.assertFalse((self.root / 'rejected').exists())

    def test_bundle_rejects_other_tasks_and_source_answers(self):
        directory = self.bundle()
        for filename in ('items.jsonl', 'other-task.json', 'lib/Answer.lean'):
            extra = directory / filename
            extra.write_text('other task answer')
            with self.assertRaisesRegex(ValueError, 'exactly one task'):
                load_task(directory)
            extra.unlink()
        extra = directory / 'other-task'
        extra.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            load_task(directory)

    def test_bundle_rejects_modified_context_and_solved_input(self):
        directory = self.bundle()
        context = directory / 'Context.lean'
        context.write_text('changed context')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            load_task(directory)
        context.write_text(self.item['context'])
        (directory / 'Candidate.lean').write_text(self.item['decl'] + '\n  trivial\n')
        with self.assertRaisesRegex(ValueError, 'unsolved template'):
            load_task(directory)

    def test_bundle_rejects_target_olean_and_changed_dependencies(self):
        directory = self.bundle()
        manifest_path = directory / 'dependencies.json'
        manifest = json.loads(manifest_path.read_text())
        library = directory / 'lib/Allowed.olean'
        library.write_bytes(b'compiled dependency')
        manifest['files'] = {'lib/Allowed.olean': file_digest(library)}
        write_json(manifest_path, manifest)
        load_task(directory)
        library.write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            load_task(directory)
        library.unlink()
        library = directory / 'lib/Example.olean'
        library.write_bytes(b'compiled target answer')
        manifest['files'] = {'lib/Example.olean': file_digest(library)}
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'target source module'):
            load_task(directory)

    def test_missing_input_cli_emits_failure_generation(self):
        output = self.root / 'failed-run'
        source = Path(__file__).resolve().parents[1] / 'src'
        result = subprocess.run([sys.executable, '-m', 'rlm_pipeline', 'run',
                                 '--input', str(self.root / 'missing-task'), '--output', str(output)],
                                cwd=source, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        generation = json.loads((output / 'generation.json').read_text())
        self.assertIsNone(generation['item_index'])
        self.assertEqual(generation['status'], 'setup_error')

    def test_runtime_refuses_dataset_selector(self):
        source = Path(__file__).resolve().parents[1] / 'src'
        result = subprocess.run([sys.executable, '-m', 'rlm_pipeline', 'run',
                                 '--dataset', '/all/items.jsonl', '--id', '161',
                                 '--input', '/input', '--output', '/output'],
                                cwd=source, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_generation_exports_without_judging_proof(self):
        export_item(self.item, self.root / 'input')
        workspace = self.root / 'workspace'
        workspace.mkdir()
        task = self.item['context'] + '\n' + self.item['decl'] + '\n  sorry\n'
        (workspace / 'Task.lean').write_text(task)
        write_json(self.root / 'run.json', {
            'run_id': 'test', 'item': {key: self.item[key] for key in ('index', 'repo', 'name', 'label', 'file')},
            'model': 'test-model', 'sample_id': 0, 'status': 'ok', 'exit_code': 0, 'started_at': 'now'})
        record = collect_generation(self.root)
        self.assertEqual(record['evaluation_status'], 'not_evaluated')
        self.assertNotIn('validation', record)
        self.assertNotIn('compile_attempts', record)
        self.assertIn('sorry', record['raw_text'])
        self.assertEqual(record, json.loads((self.root / 'generations.jsonl').read_text()))
        (workspace / 'Task.lean').write_text(task.replace('namespace Example', 'namespace Changed'))
        record = collect_generation(self.root)
        self.assertFalse(record['context_prefix_preserved'])
        self.assertEqual(record['evaluation_status'], 'not_evaluated')
        self.assertEqual((self.root / 'Task.lean').read_text(), record['raw_text'])

    def test_setup_failure_still_has_generation(self):
        write_json(self.root / 'run.json', {
            'run_id': 'failed', 'item': {key: self.item[key] for key in ('index', 'repo', 'name', 'label', 'file')},
            'model': 'model', 'sample_id': 0, 'status': 'setup_error', 'started_at': 'now', 'error_detail': 'missing tools'})
        result = collect_generation(self.root)
        self.assertEqual(result['status'], 'setup_error')
        self.assertEqual(result['raw_text'], '')
        self.assertTrue((self.root / 'generation.json').exists())


if __name__ == '__main__':
    unittest.main()
