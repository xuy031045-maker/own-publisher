import base64
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PIL import Image, PngImagePlugin

from browser import images_match_uploads, observe_xiaohongshu_uploads
from upload_evidence import UploadEvidence


def request(method, url, body=b'', status=200, response_body=''):
    item = Mock(method=method, url=url, post_data_buffer=body)
    item.response.return_value = Mock(status=status, text=Mock(return_value=response_body))
    return item


class UploadEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source_hash = 'a' * 64
        self.actual_hash = hashlib.sha256(b'transformed').hexdigest()
        self.proof = UploadEvidence([{'sha256': self.source_hash}])
        self.source = {'frame': Mock(url='https://creator.xiaohongshu.com/publish/publish')}

    def test_transformed_upload_requires_matching_successful_request(self):
        payload = {'mode': 'direct', 'id': 'spectrum/a', 'upload_sha': self.actual_hash,
                   'source_sha': self.source_hash}
        self.proof.receive(self.source, payload)
        self.assertNotIn('spectrum/a', self.proof)
        self.proof.finished(request('PUT', 'https://ros-upload-d4.xhscdn.com/spectrum/a', b'transformed'))
        self.assertEqual(self.proof['spectrum/a'], self.source_hash)

        other = UploadEvidence([{'sha256': self.source_hash}])
        other.finished(request('PUT', 'https://ros-upload-d4.xhscdn.com/spectrum/a', b'different'))
        other.receive(self.source, payload)
        self.assertNotIn('spectrum/a', other)
        other.receive({'frame': Mock(url='https://other.example/')}, payload)
        self.assertNotIn('spectrum/a', other)

    def test_multipart_requires_every_successful_part_and_completion(self):
        base = 'https://ros-upload-d4.xhscdn.com/spectrum/large'
        payload = {'mode': 'multipart', 'id': 'spectrum/large', 'upload_id': 'upload1',
                   'parts': [1, 2], 'upload_sha': self.actual_hash, 'source_sha': self.source_hash}
        self.proof.receive(self.source, payload)
        self.proof.finished(request('PUT', base + '?partNumber=1&uploadId=upload1', b'first'))
        xml = b'<CompleteMultipartUpload><Part><PartNumber>1</PartNumber></Part><Part><PartNumber>2</PartNumber></Part></CompleteMultipartUpload>'
        ok = '<CompleteMultipartUploadResult><ETag>ok</ETag></CompleteMultipartUploadResult>'
        self.proof.finished(request('POST', base + '?uploadId=upload1', xml, response_body=ok))
        self.assertNotIn('spectrum/large', self.proof)
        self.proof.finished(request('PUT', base + '?partNumber=2&uploadId=upload1', b'second', status=500))
        self.assertNotIn('spectrum/large', self.proof)
        self.proof.finished(request('PUT', base + '?partNumber=2&uploadId=upload1', b'second'))
        self.assertEqual(self.proof['spectrum/large'], self.source_hash)

    def test_conflicting_successful_upload_invalidates_evidence(self):
        payload = {'mode': 'direct', 'id': 'spectrum/a', 'upload_sha': self.actual_hash,
                   'source_sha': self.source_hash}
        self.proof.finished(request('PUT', 'https://ros-upload.xiaohongshu.com/spectrum/a', b'transformed'))
        self.proof.receive(self.source, payload)
        self.assertIn('spectrum/a', self.proof)
        self.proof.finished(request('PUT', 'https://ros-upload.xiaohongshu.com/spectrum/a', b'other'))
        self.assertNotIn('spectrum/a', self.proof)

    def test_real_edge_handles_transcoded_and_multipart_images_locally(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'preview.png'
            image = Image.new('RGB', (41, 29), (42, 100, 200))
            image.save(path)
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            info = PngImagePlugin.PngInfo()
            info.add_text('fixture', 'different metadata, same pixels')
            converted = io.BytesIO()
            image.save(converted, format='PNG', pnginfo=info)
            self.assertNotEqual(hashlib.sha256(converted.getvalue()).hexdigest(), original_hash)
            encoded = json.dumps(base64.b64encode(converted.getvalue()).decode())
            html = '''<input type="file" accept="image/*"><script>
              const upload = (method, url, body) => new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest(); xhr.open(method, url);
                xhr.onload = () => xhr.status === 200 ? resolve() : reject(new Error('upload failed'));
                xhr.onerror = reject; xhr.send(body);
              });
              document.querySelector('input').addEventListener('change', async event => {
                const file = event.target.files[0];
                const data = Uint8Array.from(atob(CONVERTED), c => c.charCodeAt(0));
                await upload('PUT', 'https://ros-upload-d4.xhscdn.com/spectrum/converted', new Blob([data], {type:'image/png'}));
                const split = Math.floor(file.size / 2);
                await Promise.all([
                  upload('PUT', 'https://ros-upload-d4.xhscdn.com/spectrum/multi?partNumber=1&uploadId=upload1', file.slice(0, split)),
                  upload('PUT', 'https://ros-upload-d4.xhscdn.com/spectrum/multi?partNumber=2&uploadId=upload1', file.slice(split))
                ]);
                await upload('POST', 'https://ros-upload-d4.xhscdn.com/spectrum/multi?uploadId=upload1',
                  '<CompleteMultipartUpload><Part><PartNumber>1</PartNumber></Part><Part><PartNumber>2</PartNumber></Part></CompleteMultipartUpload>');
                window.fixtureDone = true;
              });
            </script>'''.replace('CONVERTED', encoded)
            cors = {'Access-Control-Allow-Origin': 'https://creator.xiaohongshu.com',
                    'Access-Control-Allow-Methods': 'GET, PUT, POST, OPTIONS',
                    'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Credentials': 'true'}
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True)
                try:
                    page = browser.new_page()
                    page.route('**/*', lambda route: route.abort())
                    page.route('https://creator.xiaohongshu.com/**',
                               lambda route: route.fulfill(content_type='text/html', body=html))
                    def respond(route):
                        if route.request.method == 'OPTIONS':
                            route.fulfill(status=204, headers=cors, body='')
                        elif route.request.method == 'POST':
                            route.fulfill(status=200, headers=cors | {'Content-Type': 'application/xml'},
                                          body='<CompleteMultipartUploadResult><ETag>ok</ETag></CompleteMultipartUploadResult>')
                        else:
                            route.fulfill(status=200, headers=cors, body='ok')
                    page.route('https://ros-upload-d4.xhscdn.com/**', respond)
                    uploaded = observe_xiaohongshu_uploads(page, [{'sha256': original_hash}])
                    page.goto('https://creator.xiaohongshu.com/test')
                    page.locator('input').set_input_files(str(path))
                    page.wait_for_function("window.fixtureDone === true", timeout=10000)
                    for _ in range(60):
                        if {'spectrum/converted', 'spectrum/multi'} <= uploaded.keys():
                            break
                        page.wait_for_timeout(200)
                    self.assertEqual(uploaded.get('spectrum/converted'), original_hash)
                    self.assertEqual(uploaded.get('spectrum/multi'), original_hash)
                    expected = [{'sha256': original_hash}, {'sha256': original_hash}]
                    self.assertTrue(images_match_uploads([{'file_id': 'spectrum/converted'},
                                                         {'file_id': 'spectrum/multi'}], expected, uploaded))
                finally:
                    browser.close()


if __name__ == '__main__':
    unittest.main()
