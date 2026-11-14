import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from platform_sync import candidate_status, collect_creator_notes, safe_thumbnail_url, synchronize
from publisher import add_account, batch_status, prepare_batch_data, save_account_identity, set_state
from xiaohongshu import BEIJING


class PlatformSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        root = Path(self.folder.name)
        self.images = []
        for color in ('red', 'blue'):
            path = root / (color + '.png')
            Image.new('RGB', (32, 20), color).save(path)
            self.images.append(path)
        add_account('xhs01', 'xiaohongshu')
        save_account_identity('xhs01', '账号', '1' * 64)
        self.batch = prepare_batch_data({'post': {'title': '核对作品', 'images': [str(x) for x in self.images]},
                                         'targets': ['xhs01']}, root)['batch']
        set_state(self.batch, 'xhs01', 'submitted')
        self.record = {'id': 'note123', 'display_title': '核对作品', 'tab_status': 1,
                       'visible_time': int(datetime.now(timezone.utc).timestamp()), 'schedule_post_time': 0,
                       'images_list': [{'url': 'http://test.xhscdn.com/red'}, {'url': 'http://test.xhscdn.com/blue'}]}

    def fake_page(self, records, later_records=None):
        page = Mock()
        page.url = 'https://creator.xiaohongshu.com/new/note-manager'
        callbacks = {}
        page.on.side_effect = lambda event, fn: callbacks.setdefault(event, []).append(fn)
        response = Mock(status=200, url='https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=0&page=0')
        response.json.return_value = {'success': True, 'data': {'notes': records, 'page': 7 if later_records is not None else -1}}
        page.goto.side_effect = lambda *a, **kw: [fn(response) for fn in callbacks['response']]
        if later_records is not None:
            following = Mock(status=200, url='https://creator.xiaohongshu.com/api/galaxy/v2/creator/note/user/posted?tab=0&page=7')
            following.json.return_value = {'success': True, 'data': {'notes': later_records, 'page': -1}}
            page.locator.return_value.first.scroll_into_view_if_needed.side_effect = lambda **kw: [fn(following) for fn in callbacks['response']]

        def image_response(url, **kwargs):
            name = 'red' if url.endswith('/red') else 'blue'
            body = (Path(self.folder.name) / (name + '.png')).read_bytes()
            result = Mock(ok=True, headers={'content-length': str(len(body))})
            result.body.return_value = body
            return result
        page.request.get.side_effect = image_response
        return page

    def run_sync(self, records, later_records=None):
        page = self.fake_page(records, later_records)
        @contextmanager
        def creator(*args, **kwargs):
            yield page
        with patch('platform_sync.creator_page', creator), \
             patch('platform_sync.observe_xiaohongshu_identity', return_value={'identity_hash': '1' * 64}), \
             patch('platform_sync.require_xiaohongshu_identity', return_value='1' * 64), \
             patch('platform_sync.check_challenge'):
            return synchronize(self.batch, 'xhs01')

    def test_matches_platform_id_and_published_status(self):
        self.assertEqual(self.run_sync([self.record]), {'note_id': 'note123', 'status': 'published'})
        task = batch_status(self.batch)['tasks'][0]
        self.assertEqual(task['platform_result']['note_id'], 'note123')
        self.assertEqual(task['platform_result']['status'], 'published')
        self.assertEqual(task['state'], 'submitted')

    def test_known_note_id_survives_reconciliation_and_status_changes(self):
        self.run_sync([self.record])
        with patch('platform_sync.thumbnail_matches', return_value=True):
            from platform_sync import match_records
            changed = dict(self.record, tab_status=2, visible_time=0)
            other = dict(changed, id='different-note')
            post = batch_status(self.batch, preview=True)['tasks'][0]['post']
            self.assertEqual(match_records(Mock(), [other, changed], post, '2000-01-01 00:00:00',
                                           known_note_id='note123'), {'note_id': 'note123', 'status': 'auditing'})
        page = Mock()
        records = collect_creator_notes(page, [{'requested': '0', 'next': 7, 'notes': [self.record]}],
                                        known_note_id='note123', timeout_seconds=0.1)
        self.assertEqual(records[0]['id'], 'note123')
        page.locator.assert_not_called()

    def test_ambiguous_or_wrong_order_does_not_save_a_match(self):
        with self.assertRaisesRegex(ValueError, '没有唯一匹配'):
            self.run_sync([self.record, dict(self.record, id='note456')])
        reversed_record = dict(self.record, images_list=list(reversed(self.record['images_list'])))
        with self.assertRaisesRegex(ValueError, '没有唯一匹配'):
            self.run_sync([reversed_record])
        self.assertNotIn('platform_result', batch_status(self.batch)['tasks'][0])

    def test_creator_pages_must_reach_a_terminal_cursor(self):
        page = Mock()
        captured = [{'requested': '0', 'next': 7, 'notes': [{'id': 'first'}]}]

        def scroll(**kwargs):
            captured.append({'requested': '7', 'next': -1, 'notes': [{'id': 'second'}]})

        page.locator.return_value.first.scroll_into_view_if_needed.side_effect = scroll
        records = collect_creator_notes(page, captured, timeout_seconds=0.1)
        self.assertEqual([row['id'] for row in records], ['first', 'second'])
        page.locator.return_value.first.scroll_into_view_if_needed.assert_called_once()
        with self.assertRaisesRegex(ValueError, '分页信息异常'):
            collect_creator_notes(page, [{'requested': '0', 'next': 0, 'notes': []}], timeout_seconds=0.1)
        with self.assertRaisesRegex(ValueError, '未能完整读取'):
            collect_creator_notes(Mock(), [{'requested': '0', 'next': 7, 'notes': []}], timeout_seconds=0.01)

    def test_creator_pages_do_not_hide_ambiguous_later_notes(self):
        with self.assertRaisesRegex(ValueError, '没有唯一匹配'):
            self.run_sync([self.record], [dict(self.record, id='note456')])
        self.assertNotIn('platform_result', batch_status(self.batch)['tasks'][0])
        self.assertEqual(self.run_sync([], [self.record]), {'note_id': 'note123', 'status': 'published'})

    def test_platform_time_must_be_observed_not_inferred(self):
        future = (datetime.now(BEIJING) + timedelta(hours=3)).replace(second=0, microsecond=0)
        post = {'title': '标题', 'images': [1], 'schedule': future.strftime('%Y-%m-%dT%H:%M')}
        row = {'id': 'id', 'display_title': '标题', 'images_list': [1], 'tab_status': 4,
               'schedule_post_time': int(future.timestamp() * 1000), 'visible_time': 0}
        self.assertEqual(candidate_status(row, post, ''), 'scheduled')
        row['tab_status'] = 2
        self.assertEqual(candidate_status(row, post, ''), 'auditing')
        row['tab_status'] = 3
        self.assertEqual(candidate_status(row, post, ''), 'rejected')
        row['tab_status'] = 1
        self.assertIsNone(candidate_status(row, post, ''))
        row.update(schedule_post_time=0, visible_time=int(future.timestamp()))
        self.assertIsNone(candidate_status(row, post, ''))
        past = future - timedelta(days=1)
        post['schedule'] = past.strftime('%Y-%m-%dT%H:%M')
        row['visible_time'] = int(past.timestamp())
        self.assertEqual(candidate_status(row, post, ''), 'published')
        row.update(tab_status=2)
        self.assertEqual(candidate_status(row, post, ''), 'auditing')

    def test_thumbnail_hosts_are_restricted(self):
        self.assertTrue(safe_thumbnail_url('http://test.xhscdn.com/a').startswith('https://'))
        for url in ('http://test.xhscdn.com.evil.test/a', 'http://127.0.0.1/a',
                    'https://name:secret@test.xhscdn.com/a', 'file:///test'):
            with self.assertRaises(ValueError):
                safe_thumbnail_url(url)


if __name__ == '__main__':
    unittest.main()
