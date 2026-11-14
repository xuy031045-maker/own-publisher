"""Correlate successful creator uploads with locally selected image bytes."""
import hashlib
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree


def trusted_upload_url(value):
    url = urlparse(value)
    host = url.hostname or ''
    trusted = host == 'ros-upload.xiaohongshu.com' or bool(re.fullmatch(r'ros-upload-d[0-9]{1,2}\.xhscdn\.com', host))
    try:
        valid = url.scheme == 'https' and trusted and url.port in (None, 443)
    except ValueError:
        valid = False
    return url if valid else None


def valid_id(value):
    return isinstance(value, str) and 0 < len(value) <= 1024 and not any(char in value for char in '\x00?#')


def sha_of_body(request):
    try:
        body = request.post_data_buffer
        return hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else None
    except Exception:
        return None


def xml_parts(body):
    try:
        root = ElementTree.fromstring(body)
        if root.tag.rsplit('}', 1)[-1] != 'CompleteMultipartUpload':
            return ()
        numbers = tuple(int(node.text) for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'PartNumber')
        return numbers if numbers and numbers == tuple(range(1, len(numbers) + 1)) else ()
    except (ValueError, TypeError, ElementTree.ParseError):
        return ()


def successful_completion(response):
    try:
        root = ElementTree.fromstring(response.text())
        return root.tag.rsplit('}', 1)[-1] == 'CompleteMultipartUploadResult'
    except (ValueError, TypeError, ElementTree.ParseError):
        return False


class UploadEvidence(dict):
    def __init__(self, images=None):
        super().__init__()
        self.expected = None if images is None else {image['sha256'] for image in images}
        self.direct = {}
        self.probes = {}
        self.multipart = {}
        self.ambiguous = set()

    def receive(self, source, payload):
        """Accept only hash evidence from the creator page's own frame."""
        try:
            frame = source['frame']
            origin = urlparse(frame.url)
            if (origin.scheme, origin.netloc) != ('https', 'creator.xiaohongshu.com') or not isinstance(payload, dict):
                return
            identifier, mode = payload.get('id'), payload.get('mode')
            upload_hash, source_hash = payload.get('upload_sha'), payload.get('source_sha')
            if (not valid_id(identifier) or mode not in ('direct', 'multipart') or
                    not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
                            for value in (upload_hash, source_hash)) or
                    self.expected is None or source_hash not in self.expected):
                return
            if mode == 'direct':
                key = ('direct', identifier)
            else:
                upload_id, parts = payload.get('upload_id'), payload.get('parts')
                if (not valid_id(upload_id) or not isinstance(parts, list) or not parts or len(parts) > 10000
                        or any(type(number) is not int for number in parts)
                        or parts != list(range(1, len(parts) + 1))):
                    return
                key = ('multipart', identifier, upload_id)
            candidate = {'upload_sha': upload_hash, 'source_sha': source_hash,
                         'parts': tuple(payload.get('parts', []))}
            if key in self.probes and self.probes[key] != candidate:
                self.ambiguous.add(identifier)
                self.pop(identifier, None)
                return
            self.probes[key] = candidate
            self.refresh(identifier)
        except Exception:
            return

    def finished(self, request):
        try:
            url = trusted_upload_url(request.url)
            if not url:
                return
            identifier = unquote(url.path.lstrip('/'))
            if not valid_id(identifier):
                return
            response = request.response()
            if not response or not 200 <= response.status < 300:
                return
            if request.method == 'PUT' and not url.query:
                digest = sha_of_body(request)
                if identifier in self.direct:
                    previous = self.direct[identifier]
                    if previous is None or digest is None or previous != digest:
                        self.ambiguous.add(identifier)
                self.direct[identifier] = digest
            else:
                query = parse_qs(url.query, keep_blank_values=True)
                upload_id = query.get('uploadId', [None])
                if len(upload_id) != 1 or not valid_id(upload_id[0]):
                    return
                key = (identifier, upload_id[0])
                group = self.multipart.setdefault(key, {'parts': {}, 'manifest': None})
                if request.method == 'PUT' and set(query) == {'uploadId', 'partNumber'}:
                    numbers = query['partNumber']
                    if len(numbers) != 1 or not numbers[0].isdigit() or not 1 <= int(numbers[0]) <= 10000:
                        return
                    number, digest = int(numbers[0]), sha_of_body(request)
                    if number in group['parts']:
                        previous = group['parts'][number]
                        if previous is None or digest is None or previous != digest:
                            self.ambiguous.add(identifier)
                    group['parts'][number] = digest
                elif request.method == 'POST' and set(query) == {'uploadId'}:
                    manifest = xml_parts(request.post_data_buffer)
                    if not manifest or not successful_completion(response):
                        return
                    if group['manifest'] is not None and group['manifest'] != manifest:
                        self.ambiguous.add(identifier)
                    group['manifest'] = manifest
                else:
                    return
            self.refresh(identifier)
        except Exception:
            return

    def refresh(self, identifier):
        if identifier in self.ambiguous:
            self.pop(identifier, None)
            return
        possibilities = set()
        if identifier in self.direct:
            digest = self.direct[identifier]
            probe = self.probes.get(('direct', identifier))
            if digest and (self.expected is None or digest in self.expected):
                possibilities.add(digest)
            if probe and (digest is None or digest == probe['upload_sha']):
                possibilities.add(probe['source_sha'])
        for (file_id, upload_id), group in self.multipart.items():
            if file_id != identifier:
                continue
            probe = self.probes.get(('multipart', file_id, upload_id))
            manifest = group['manifest']
            if probe and manifest and probe['parts'] == manifest and set(group['parts']) == set(manifest):
                possibilities.add(probe['source_sha'])
        if len(possibilities) == 1:
            self[identifier] = possibilities.pop()
        else:
            self.pop(identifier, None)

    def install(self, page):
        if self.expected is None:
            return
        binding = '__own_publisher_upload_' + secrets.token_hex(12)
        page.expose_binding(binding, self.receive)
        script = (Path(__file__).resolve().parent / 'upload_probe.js').read_text(encoding='utf-8')
        page.add_init_script(script=script.replace('__OWN_PUBLISHER_BINDING__', json.dumps(binding)))
