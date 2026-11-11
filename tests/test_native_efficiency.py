"""Scope, durable authoring and cheap inspection regressions."""
from contextlib import redirect_stderr
import copy
import io
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, review_session
import contract_boundary
import review_coordinator as coordination
import review_flow as flow
import review_author_operation as author


class NativeEfficiencyTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def test_prefetch_respects_change_scope_in_both_batch_entrypoints(self):
        self.make_repo('consumer')
        self.make_repo('unrelated')
        for entry in ('batch', 'step'):
            with self.subTest(entry=entry):
                context, manifest = self.capture(policy='boundary-contracts/v2')
                change = contract_boundary.changes(manifest)[0]['id']
                queries = [{'query': value, 'change_id': change} for value in ('contract', 'new')]
                calls = []
                original = workspace.FixedSearchCache.prefetch
                def prefetch(cache, fixed, requests):
                    calls.extend(copy.deepcopy(requests))
                    return original(cache, fixed, requests)
                with mock.patch.object(workspace.FixedSearchCache, 'prefetch', prefetch), \
                     mock.patch.object(api, 'run_git_capture', wraps=api.run_git_capture) as git_calls:
                    if entry == 'batch':
                        workspace.batch_search(context, manifest, queries, api)
                    else:
                        result = workspace.ordered_step(context, manifest, [dict(q, id=f's-{i}', op='search')
                                                        for i, q in enumerate(queries)], api)
                        self.assertTrue(result['complete'], result)
                self.assertEqual(2, len(calls))
                self.assertEqual({manifest['selected_repo']}, {row['repo'] for row in calls})
                self.assertEqual({manifest['selected_repo']}, {row['repo'] for row in manifest['searches']})
                grep_roots = {str(call.args[0]) for call in git_calls.call_args_list if 'grep' in call.args[1]}
                selected = workspace.repository(manifest, manifest['selected_repo'])
                self.assertEqual({str(workspace.evidence_repository(context, selected))}, grep_roots)

    def test_identified_consumer_prefetch_excludes_unrelated_repositories(self):
        consumer = self.make_repo('consumer')
        self.make_repo('unrelated')
        context, manifest = self.capture(policy='boundary-contracts/v2')
        self.draft(context, manifest)
        contract = manifest['changed_contracts'][0]
        consumer_id = next(row['id'] for row in manifest['repositories'] if row['path'] == str(consumer))
        contract.update(search_scope='identified-repos', candidate_repos=[consumer_id])
        manifest['searches'] = []
        workspace.save(context, manifest)
        with mock.patch.object(api, 'run_git_capture', wraps=api.run_git_capture) as git_calls:
            workspace.batch_search(context, manifest, [{'query': contract['symbol'], 'contract': contract['id']}], api)
        wanted = {contract['evidence']['repo'], consumer_id}
        roots = {str(workspace.evidence_repository(context, workspace.repository(manifest, identifier))) for identifier in wanted}
        self.assertEqual(roots, {str(call.args[0]) for call in git_calls.call_args_list if 'grep' in call.args[1]})
        self.assertEqual(wanted, {row['repo'] for row in manifest['searches']})

    def test_invalid_scope_never_runs_git_during_prefetch(self):
        self.make_repo('unrelated')
        context, manifest = self.capture(policy='boundary-contracts/v2')
        change = contract_boundary.changes(manifest)[0]['id']
        peer = next(row['id'] for row in manifest['repositories'] if row['id'] != manifest['selected_repo'])
        with mock.patch.object(api, 'run_git_capture', side_effect=AssertionError('speculative Git')):
            result = workspace.ordered_step(context, manifest,
                [{'id': 'bad', 'op': 'search', 'query': 'contract', 'repo': [peer], 'change_id': change}], api)
        self.assertFalse(result['complete'])
        self.assertEqual([], manifest['searches'])

    def compact_session(self):
        args = flow.parser().parse_args(['start', '--workspace', str(self.root), '--project', str(self.repo),
              '--base', 'main', '--head', 'feature', '--runtime-root', str(self.root / 'runtime'), '--draft-format', 'compact'])
        with redirect_stderr(io.StringIO()):
            started = flow.start(args)
        context = Path(started['context_dir'])
        self.addCleanup(review_session.cleanup_session, context)
        return Path(started['runtime_root']), started['request_id'], context

    def test_invalid_query_does_not_execute_git_in_either_batch_entrypoint(self):
        context, manifest = self.capture(policy='boundary-contracts/v2')
        with mock.patch.object(api, 'run_git_capture', side_effect=AssertionError('invalid query Git')):
            with self.assertRaises(ValueError):
                workspace.batch_search(context, manifest, [{'query': ''}], api)
            result = workspace.ordered_step(context, manifest, [{'id': 'empty', 'op': 'search', 'query': ''}], api)
        self.assertFalse(result['complete'])

    def test_new_contract_scope_is_resolved_after_registration(self):
        self.make_repo('unrelated')
        context, manifest = self.capture(policy='boundary-contracts/v2')
        self.draft(context, manifest)
        contract = copy.deepcopy(manifest['changed_contracts'][0])
        contract.pop('anchor_sha256', None)
        contract['search_scope'] = 'repo-local'
        manifest['changed_contracts'] = []; manifest['searches'] = []
        workspace.save(context, manifest)
        operations = [{'id': 'new-contract', 'op': 'register-contract', 'contract': contract},
                      {'id': 'scoped-search', 'op': 'search', 'query': contract['symbol'], 'contract': contract['id']}]
        with mock.patch.object(api, 'run_git_capture', wraps=api.run_git_capture) as git_calls:
            result = workspace.ordered_step(context, manifest, operations, api)
        self.assertTrue(result['complete'], result)
        selected = workspace.repository(manifest, manifest['selected_repo'])
        self.assertEqual({str(workspace.evidence_repository(context, selected))},
                         {str(call.args[0]) for call in git_calls.call_args_list if 'grep' in call.args[1]})

    def test_small_equal_blob_response_shares_body_and_keeps_source_receipts(self):
        root, identifier, context = self.compact_session()
        manifest = workspace.load(context)
        operations = [{'id': source, 'op': 'read', 'repo': manifest['selected_repo'], 'source': source, 'path': 'app.py'}
                      for source in ('head', 'merge-preview')]
        with redirect_stderr(io.StringIO()):
            result = flow.step(root, identifier, operations)
        response = flow.compact(result, context)
        self.assertEqual(1, len(response['contents']))
        self.assertEqual({'head', 'merge-preview'}, {row['result']['source'] for row in response['operations']})
        self.assertTrue(all(row['saved'] and row['result_returned'] for row in response['operations']))
        self.assertEqual(1, len({row['result']['content_id'] for row in response['operations']}))

    def test_atomic_author_retry_conflict_checkpoint_and_read_only_summary(self):
        root, identifier, context = self.compact_session()
        operation = {'id': 'author-one', 'op': 'author',
                     'expected_draft_sha256': coordination.file_digest(context / 'draft.json'),
                     'updates': {'summary': '已開始核對固定變更。'},
                     'checkpoint': {'next_action': 'Read initial evidence.', 'unread_evidence': ['initial-reads.json']}}
        with redirect_stderr(io.StringIO()):
            saved = flow.step(root, identifier, [operation])
        self.assertTrue(saved['complete'], saved)
        body = (context / 'draft.json').read_bytes()
        with redirect_stderr(io.StringIO()):
            retry = flow.step(root, identifier, [operation])
        self.assertTrue(retry['complete'], retry)
        self.assertEqual(body, (context / 'draft.json').read_bytes())
        checked, _ = flow.check(root, identifier)
        self.assertTrue(flow.status(root, identifier, summary=True)['check_current'])
        with mock.patch.object(workspace, 'load', side_effect=AssertionError('expanded status')), \
             mock.patch.object(flow, 'diagnostics', side_effect=AssertionError('status diagnostics')), \
             mock.patch.object(coordination, 'atomic', side_effect=AssertionError('status write')):
            summary = flow.status(root, identifier, summary=True)
        self.assertTrue(summary['checkpoint_current'])
        self.assertEqual(64, len(summary['status_revision']))
        self.assertEqual('Read initial evidence.', summary['execution_checkpoint']['next_action'])
        self.assertFalse(summary['review_complete'])
        stale = {**operation, 'id': 'author-two', 'updates': {'summary': 'stale'}}
        with redirect_stderr(io.StringIO()):
            conflict = flow.step(root, identifier, [stale])
        self.assertFalse(conflict['complete'])
        self.assertEqual(body, (context / 'draft.json').read_bytes())
        coordination.atomic(context / 'draft.json', {**coordination.read_json(context / 'draft.json'), 'summary': 'Manual edit'})
        summary = flow.status(root, identifier, summary=True)
        self.assertFalse(summary['check_current'])
        self.assertFalse(summary['checkpoint_current'])

    def test_author_recovers_after_draft_replace_interruption(self):
        _, _, context = self.compact_session()
        manifest = workspace.load(context)
        operation = {'id': 'recover', 'op': 'author', 'expected_draft_sha256': coordination.file_digest(context / 'draft.json'),
                     'updates': {'summary': 'Durable semantic progress.'}}
        original = coordination.atomic
        def interrupted(path, value):
            if path.name == 'draft.json':
                raise OSError('interrupted before draft replacement')
            return original(path, value)
        with mock.patch.object(coordination, 'atomic', interrupted), self.assertRaises(OSError):
            author.apply(context, manifest, operation)
        result = author.apply(context, manifest, operation)
        self.assertTrue(result['saved'])
        self.assertEqual(result['draft_sha256'], coordination.file_digest(context / 'draft.json'))
