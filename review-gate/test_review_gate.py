import pathlib
import sqlite3
import tempfile
import unittest
import unittest.mock

import review_gate as gate

OWNER_LOGIN = 'example-owner'
TEST_CONFIG = {
    'github_owner': OWNER_LOGIN, 'owner_id': 1000,
    'greptile_repos': ['iLEAPP', 'ALEAPP', 'RLEAPP', 'VLEAPP'],
    'mac_review_repos': ['iLEAPP', 'ALEAPP', 'RLEAPP', 'VLEAPP', 'DLEAPP'],
    'owner_mac_review_repos': ['DLEAPP'],
    'app_review_repos': ['GLEAPP', 'MLEAPP', 'exampleprobe'],
    'checks_only_repos': ['LAVA'],
    'holds': {'LAVA#99999': 'example hold'},
}
gate.configure(TEST_CONFIG)


class Configured(unittest.TestCase):
    def setUp(self):
        gate.configure(TEST_CONFIG)

HEAD = 'a' * 40
OTHER = 12345


def pr(repo='ALEAPP', author=gate.OWNER_ID, **changes):
    return {'repo': repo, 'number': 1, 'head': HEAD, 'author_id': author, 'state': 'open', 'draft': False,
            'mergeable': True, **changes}


def run(name='test', status='completed', conclusion='success', run_id=1):
    return {'id': run_id, 'name': name, 'status': status, 'conclusion': conclusion, 'app': {'slug': 'x'}}


GREPTILE_RUN = run(gate.GREPTILE_CHECK, run_id=2)


def thread(level='P2', replies=0, resolved=False, author='greptile-apps', outdated=False, resolved_by=OWNER_LOGIN):
    body = f'<img alt="{level}" src="x"> finding' if level else 'finding'
    comments = [{'author': {'login': author}, 'body': body}]
    comments += [{'author': {'login': OWNER_LOGIN}, 'body': 'answered'}] * replies
    return {'isResolved': resolved, 'isOutdated': outdated, 'resolvedBy': {'login': resolved_by} if resolved else None,
            'path': 'a.py', 'line': 1, 'comments': {'nodes': comments}}


class Routes(Configured):
    def test_routes(self):
        self.assertEqual(gate.route_for('ALEAPP', gate.OWNER_ID), 'greptile')
        self.assertEqual(gate.route_for('DLEAPP', gate.OWNER_ID), 'mac-private')
        self.assertEqual(gate.route_for('DLEAPP', OTHER), 'mac-contributor')
        self.assertEqual(gate.route_for('iLEAPP', OTHER), 'mac-contributor')
        for repo in ('RLEAPP', 'VLEAPP'):
            self.assertEqual(gate.route_for(repo, gate.OWNER_ID), 'greptile')
        self.assertEqual(gate.route_for('LAVA', gate.OWNER_ID), 'checks-only')
        self.assertEqual(gate.route_for('LAVA', OTHER), 'contributor-no-review')

    def test_app_repos_need_the_mac_review(self):
        self.assertEqual(gate.APP_REVIEW_LIVE, {'GLEAPP', 'MLEAPP', 'exampleprobe'})
        for repo in sorted(gate.APP_REVIEW_LIVE):
            self.assertIn(repo, gate.KNOWN_REPOS)
            self.assertEqual(gate.route_for(repo, gate.OWNER_ID), 'mac-private')
            self.assertEqual(gate.route_for(repo, OTHER), 'mac-contributor')
            self.assertTrue(gate.decide(pr(repo), [run()], [], [], False, HEAD)[1])
            self.assertTrue(gate.decide(pr(repo), [run()], [], [], True, None)[1])
            self.assertEqual(gate.decide(pr(repo), [run()], [], [], True, HEAD)[1], [])
            self.assertTrue(gate.decide(pr(repo, OTHER), [run()], [], [], True, HEAD)[1])
            self.assertEqual(gate.decide(pr(repo, OTHER), [run()], [], [], True, HEAD, HEAD)[1], [])

    def test_owner_pr_without_a_reviewer_needs_passing_checks(self):
        self.assertEqual(gate.decide(pr('LAVA'), [run()], [], [], False)[1], [])
        self.assertTrue(gate.decide(pr('LAVA'), [run(conclusion='failure')], [], [], False)[1])
        self.assertTrue(gate.decide(pr('LAVA'), [], [], [], False)[1])

    def test_a_held_pr_needs_the_owners_approval_at_this_commit(self):
        held = pr('LAVA', number=99999)
        self.assertTrue(any('On hold' in p for p in gate.decide(held, [run()], [], [], False)[1]))
        self.assertTrue(gate.decide(held, [run()], [], [], False, None, 'b' * 40)[1])
        self.assertEqual(gate.decide(held, [run()], [], [], False, None, HEAD)[1], [])

    def test_contributor_pr_without_a_reviewer_needs_the_owners_approval(self):
        self.assertTrue(gate.decide(pr('LAVA', OTHER), [run()], [], [], False)[1])
        self.assertTrue(gate.decide(pr('LAVA', OTHER), [run()], [], [], False, None, 'b' * 40)[1])
        self.assertEqual(gate.decide(pr('LAVA', OTHER), [run()], [], [], False, None, HEAD)[1], [])


class Greptile(Configured):
    def problems(self, runs=(run(), GREPTILE_RUN), threads=(), **changes):
        return gate.decide(pr(**changes), list(runs), [], list(threads), False)[1]

    def test_clean_review_passes(self):
        self.assertEqual(self.problems(), [])

    def test_green_check_with_open_finding_blocks(self):
        self.assertTrue(self.problems(threads=[thread()]))

    def test_answered_p2_passes(self):
        self.assertEqual(self.problems(threads=[thread(replies=1)]), [])

    def test_answered_p0_and_p1_still_block(self):
        for level in ('P0', 'P1'):
            self.assertTrue(self.problems(threads=[thread(level, replies=1)]), level)

    def test_p0_p1_resolved_by_greptile_passes(self):
        for level in ('P0', 'P1'):
            self.assertEqual(self.problems(threads=[thread(level, resolved=True, resolved_by='greptile-apps[bot]')]), [])

    def test_p0_p1_or_unknown_resolved_by_anyone_else_blocks(self):
        for level in ('P0', 'P1', None):
            for shape in ({}, {'replies': 1}, {'outdated': True}, {'replies': 1, 'outdated': True}):
                problems = self.problems(threads=[thread(level, resolved=True, **shape)])
                self.assertTrue(any('owner' in p for p in problems), (level, shape))

    def test_resolved_with_no_resolver_recorded_blocks(self):
        finding = thread('P1', resolved=True)
        finding['resolvedBy'] = None
        self.assertTrue(self.problems(threads=[finding]))

    def test_his_approval_at_this_commit_clears_a_resolved_p1(self):
        finding = thread('P1', resolved=True)
        self.assertEqual(gate.decide(pr(), [run(), GREPTILE_RUN], [], [finding], False, None, HEAD)[1], [])
        self.assertTrue(gate.decide(pr(), [run(), GREPTILE_RUN], [], [finding], False, None, 'b' * 40)[1])

    def test_his_approval_does_not_clear_an_open_p1(self):
        self.assertTrue(gate.decide(pr(), [run(), GREPTILE_RUN], [], [thread('P1')], False, None, HEAD)[1])

    def test_resolved_p2_needs_a_fix_or_reply(self):
        self.assertTrue(self.problems(threads=[thread(resolved=True)]))
        self.assertEqual(self.problems(threads=[thread(resolved=True, replies=1)]), [])
        self.assertEqual(self.problems(threads=[thread(resolved=True, outdated=True)]), [])
        self.assertEqual(self.problems(threads=[thread(resolved=True, resolved_by='greptile-apps[bot]')]), [])

    def test_resolved_thread_from_a_person_on_his_pr_is_not_a_greptile_finding(self):
        self.assertEqual(self.problems(threads=[thread(None, resolved=True, author=OWNER_LOGIN)]), [])

    def test_unparsed_severity_blocks_even_with_a_reply(self):
        self.assertTrue(self.problems(threads=[thread(None, replies=1)]))

    def test_missing_review_at_head_blocks(self):
        self.assertTrue(any('@greptileai' in p for p in self.problems(runs=[run()])))

    def test_running_review_blocks(self):
        self.assertTrue(self.problems(runs=[run(), run(gate.GREPTILE_CHECK, 'in_progress', None, 2)]))

    def test_rleapp_and_vleapp_need_a_greptile_review(self):
        for repo in ('RLEAPP', 'VLEAPP'):
            problems = gate.decide(pr(repo), [run()], [], [], False)[1]
            self.assertTrue(any('@greptileai' in problem for problem in problems), repo)

    def test_failed_and_running_checks_block(self):
        self.assertTrue(self.problems(runs=[run(conclusion='failure'), GREPTILE_RUN]))
        self.assertTrue(self.problems(runs=[run(status='in_progress', conclusion=None), GREPTILE_RUN]))

    def test_rerun_to_green_uses_the_latest_run(self):
        self.assertEqual(self.problems(runs=[run(conclusion='failure', run_id=1), run(run_id=3), GREPTILE_RUN]), [])

    def test_no_checks_blocks(self):
        self.assertTrue(self.problems(runs=[]))

    def test_closed_draft_and_conflict_block(self):
        for changes in ({'state': 'merged'}, {'draft': True}, {'mergeable': False}):
            self.assertTrue(self.problems(**changes), changes)


class MacReview(Configured):
    def test_needs_finished_review_and_read_report(self):
        for repo, author, approval in (('DLEAPP', gate.OWNER_ID, None), ('VLEAPP', OTHER, HEAD)):
            self.assertTrue(gate.decide(pr(repo, author), [run()], [], [], False, HEAD, approval)[1])
            self.assertTrue(gate.decide(pr(repo, author), [run()], [], [], True, None, approval)[1])
            self.assertTrue(gate.decide(pr(repo, author), [run()], [], [], True, 'b' * 40, approval)[1])
            self.assertEqual(gate.decide(pr(repo, author), [run()], [], [], True, HEAD, approval)[1], [])

    def test_contributor_merge_needs_the_owners_approval_at_this_commit(self):
        for approval in (None, 'b' * 40):
            problems = gate.decide(pr('VLEAPP', OTHER), [run()], [], [], True, HEAD, approval)[1]
            self.assertTrue(any('owner' in problem for problem in problems))

    def test_contributor_thread_without_reply_blocks(self):
        open_thread = thread(None, author=OWNER_LOGIN)
        self.assertTrue(gate.decide(pr('VLEAPP', OTHER), [run()], [], [open_thread], True, HEAD)[1])

    def test_resolved_thread_needs_a_fix_or_reply(self):
        for shape, blocked in (({}, True), ({'replies': 1}, False), ({'outdated': True}, False)):
            finding = thread(None, resolved=True, author=OWNER_LOGIN, **shape)
            problems = gate.decide(pr('VLEAPP', OTHER), [run()], [], [finding], True, HEAD, HEAD)[1]
            self.assertEqual(bool(problems), blocked, shape)

    def test_store_is_keyed_on_pr_and_head(self):
        with tempfile.TemporaryDirectory() as folder:
            store = pathlib.Path(folder) / 'a#b' / 'reviews.sqlite3'
            store.parent.mkdir()
            con = sqlite3.connect(store)
            con.execute('CREATE TABLE reviews (pr INTEGER, head TEXT, result TEXT)')
            con.execute('INSERT INTO reviews VALUES (1, ?, ?)', (HEAD, 'done'))
            con.commit()
            con.close()
            self.assertTrue(gate.mac_review_done('DLEAPP', 1, HEAD, store))
            self.assertFalse(gate.mac_review_done('DLEAPP', 1, 'b' * 40, store))
            self.assertFalse(gate.mac_review_done('DLEAPP', 2, HEAD, store))

    def test_missing_store_is_could_not_check(self):
        with self.assertRaises(gate.CouldNotCheck):
            gate.mac_review_done('DLEAPP', 1, HEAD, pathlib.Path('/nonexistent/reviews.sqlite3'))


if __name__ == '__main__':
    unittest.main()
