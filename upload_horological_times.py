#!/usr/bin/env python3
"""Upload yyyy-mm-web.pdf files to Outline, with a native PDF preview per issue.

Linux setup (run in a terminal):
  python3 -m venv ~/.venvs/outline-upload
  ~/.venvs/outline-upload/bin/pip install requests playwright
  ~/.venvs/outline-upload/bin/playwright install chromium

Run, assuming this script is saved in ~/Downloads:
  ~/.venvs/outline-upload/bin/python ~/Downloads/upload_horological_times.py --dry-run
  ~/.venvs/outline-upload/bin/python ~/Downloads/upload_horological_times.py --limit 1
  ~/.venvs/outline-upload/bin/python ~/Downloads/upload_horological_times.py

Create an API key in Outline's Settings > API & Apps (wording varies by version).
The account/key needs collections.list, documents.list/info/create/update,
attachments.create/redirect, and files.create if using local file storage.
Paste the key into the hidden terminal prompt. It is not saved.
When Chromium opens, sign in to the SAME Outline workspace, then return to the
terminal and press Enter. This profile is reused on later runs.

Defaults: http://andyswatches.home:3333, ~/Downloads/awci, Collaboration.
The API uses that internal address; Chromium uses https://wiki.andyswatches.com
for its existing browser login. Override the latter with --browser-url.
Only the immediate folder is scanned. Missing months are reported, not invented.
Progress and issue links: ~/Downloads/awci/horological-times-upload.json
Keep that file for resuming and for building a page-level search index later.
Do not run two copies of the importer, even with different state-file paths.

Preview enabling selects the matching PDF attachment in a local Chromium
browser and clicks Outline's Show preview button. A server-side API
read verifies that the flag was saved before an issue is marked complete.
If an installed Outline version is incompatible, the script stops; uploaded
files and completed pages are retained. No existing pages are deleted.
"""

import argparse
import calendar
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlsplit


class UploadError(Exception):
    pass


def discover(folder):
    found = []
    ignored = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() != '.pdf':
            continue
        match = re.fullmatch(r'(\d{4})-(0[1-9]|1[0-2])-web\.pdf', path.name)
        if not match:
            ignored.append(path.name)
            continue
        with path.open('rb') as stream:
            if b'%PDF-' not in stream.read(1024):
                raise UploadError(f'Not a PDF: {path.name}')
        found.append((match[1], int(match[2]), path))
    return found, ignored


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def save_state(path, state):
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(state, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def walk(node):
    if isinstance(node, dict):
        yield node
        for child in node.get('content', []):
            yield from walk(child)


def pdf_node(doc, attachment_id):
    for node in walk(doc.get('data', {})):
        attrs = node.get('attrs', {})
        if node.get('type') == 'attachment' and (
            attrs.get('id') == attachment_id or
            f'id={attachment_id}' in (attrs.get('href') or '')
        ):
            return node
    return None


class Outline:
    def __init__(self, base, token):
        import requests
        self.requests = requests
        self.base = base.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}',
                                     'X-API-Version': '3'})
        self.last_call = 0

    def absolute(self, url):
        return urljoin(self.base + '/', url)

    def call(self, endpoint, **body):
        # Outline limits several write endpoints to 25 requests/minute.
        for attempt in range(8):
            time.sleep(max(0, 2.6 - (time.monotonic() - self.last_call)))
            self.last_call = time.monotonic()
            try:
                response = self.session.post(self.base + '/api/' + endpoint,
                                             json=body, timeout=(15, 180),
                                             allow_redirects=False)
            except self.requests.RequestException:
                raise UploadError(f'{endpoint}: connection failed or timed out. '
                                  'Rerun to resume; a write may have succeeded.') from None
            if response.status_code == 429:
                try:
                    delay = min(120, max(5, float(response.headers.get('Retry-After', 60))))
                except ValueError:
                    delay = 60
                print(f'Rate limit: waiting {delay:g} seconds.', flush=True)
                time.sleep(delay)
                continue
            try:
                result = response.json()
            except ValueError:
                raise UploadError(f'{endpoint}: HTTP {response.status_code}, non-JSON response. '
                                  'Check the URL and any reverse-proxy login wall.') from None
            if not response.ok or result.get('ok') is False or 'data' not in result:
                message = str(result.get('message') or result.get('error') or 'Request failed')[:300]
                raise UploadError(f'{endpoint}: HTTP {response.status_code}: {message}')
            return result
        raise UploadError(f'{endpoint}: rate limit persisted; rerun later.')

    def listing(self, endpoint, **body):
        offset = 0
        while True:
            result = self.call(endpoint, limit=100, offset=offset, **body)
            rows = result['data']
            if not isinstance(rows, list):
                raise UploadError(f'{endpoint}: unexpected response shape.')
            yield from rows
            offset += len(rows)
            total = result.get('pagination', {}).get('total')
            if not rows or (total is not None and offset >= total) or len(rows) < 100:
                return

    def info(self, document_id):
        data = self.call('documents.info', id=document_id)['data']
        # API versions >= 2 wrap documents.info as data.document, whereas
        # documents.create/update and older info responses return data directly.
        # Keep the inner document's own data field (ProseMirror content) intact.
        if isinstance(data, dict) and isinstance(data.get('document'), dict):
            data = data['document']
        if not isinstance(data, dict) or data.get('id') != document_id:
            raise UploadError('documents.info returned an unexpected document response.')
        return data

    def upload(self, path, document_id):
        result = self.call('attachments.create', name=path.name,
                           documentId=document_id, contentType='application/pdf',
                           size=path.stat().st_size)['data']
        mode = result.get('mode', 'post')
        target = self.absolute(result.get('uploadUrl') if mode == 'post' else result.get('url'))
        # Never send an Outline API key to an external object-storage service.
        same_origin = urlsplit(target)[:2] == urlsplit(self.base)[:2]
        client = self.session if same_origin else self.requests.Session()
        try:
            with path.open('rb') as stream:
                if mode == 'post':
                    response = client.post(target, data=result.get('form', {}),
                                           files={'file': (path.name, stream, 'application/pdf')},
                                           timeout=(15, 1800), allow_redirects=False)
                elif mode == 'put':
                    response = client.put(target, data=stream, headers=result.get('headers', {}),
                                          timeout=(15, 1800), allow_redirects=False)
                else:
                    raise UploadError(f'Unknown upload mode: {mode}')
        except self.requests.RequestException:
            raise UploadError('PDF transfer failed or timed out; rerun to resume.') from None
        finally:
            if not same_origin:
                client.close()
        if not response.ok or response.is_redirect:
            raise UploadError(f'PDF transfer: HTTP {response.status_code}. '
                              'Check Outline file-size limits, proxy limits and storage settings.')
        try:
            if response.json().get('success') is False:
                raise UploadError('Outline rejected the PDF transfer.')
        except ValueError:
            pass  # Object storage can return an empty body or XML on success.
        return result['attachment']['id']


class PreviewBrowser:
    def __init__(self, base, profile, browser_base=None):
        self.base = (browser_base or base).rstrip('/')
        self.profile = profile
        self.context = None
        self.runtime = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.context = self.runtime.chromium.launch_persistent_context(
            str(self.profile), headless=False, viewport={'width': 1400, 'height': 1000})
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(self.base, wait_until='domcontentloaded', timeout=60000)
        input('\nSign in to Outline in the opened Chromium window. '
              'When your workspace is visible, return here and press Enter: ')

    def enable(self, url, attachment_id):
        from playwright.sync_api import TimeoutError as BrowserTimeoutError
        timeout = getattr(self, 'timeout', 60000)
        stage = 'opening the browser and signing in'
        try:
            if self.context is None:
                self.start()
            stage = 'opening the issue in edit mode'
            # Stored document links retain the internal API origin. Use the
            # browser's configured origin so its session cookie applies.
            edit_path = urlsplit(url).path.rstrip('/') + '/edit'
            edit_url = urljoin(self.base + '/', edit_path)
            print('Opening PDF preview editor: ' + edit_url, flush=True)
            self.page.goto(edit_url, wait_until='domcontentloaded', timeout=timeout)
            stage = 'waiting for the editable document'
            editor = self.page.locator('.ProseMirror[contenteditable="true"]').first
            editor.wait_for(state='visible', timeout=timeout)
            # Use Outline's visible attachment toolbar. pmViewDesc does NOT
            # expose an EditorView and must not be used as an editor handle.
            embedded = editor.locator(f'embed[src*="id={attachment_id}"]')
            attachment = editor.locator(f'a[href*="id={attachment_id}"]')
            stage = 'waiting for the PDF attachment'
            editor.locator(f'a[href*="id={attachment_id}"], '
                           f'embed[src*="id={attachment_id}"]').first.wait_for(
                               state='attached', timeout=timeout)
            if embedded.count():
                return  # Already enabled, for example after an interrupted run.
            stage = 'selecting the PDF attachment'
            attachment.first.click(timeout=timeout)
            stage = 'finding the Show preview button'
            preview = self.page.get_by_role('button', name='Show preview', exact=True)
            preview.wait_for(state='visible', timeout=timeout)
            stage = 'enabling the PDF preview'
            preview.click(timeout=timeout)
            stage = 'waiting for the embedded PDF to appear'
            embedded.first.wait_for(state='attached', timeout=timeout)
        except BrowserTimeoutError:
            details = ''
            try:
                editors = self.page.locator('.ProseMirror').count()
                editable = self.page.locator('.ProseMirror[contenteditable="true"]').count()
                location = urlsplit(self.page.url)
                details = (f' Browser page: {location.scheme}://{location.netloc}{location.path}. '
                           f'Editors: {editors}; editable editors: {editable}.')
            except Exception:
                pass
            raise UploadError(f'Browser timed out while {stage}.{details} '
                              'The uploaded PDF and progress have been retained; rerun to resume.') from None

    def close(self):
        if self.context:
            self.context.close()
        if self.runtime:
            self.runtime.stop()


def run(args):
    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        raise UploadError(f'Folder does not exist: {folder}')
    files, ignored = discover(folder)
    print(f'Found {len(files)} issues in {folder}.')
    if ignored:
        print('Ignored PDFs with other filenames: ' + ', '.join(ignored))
    if not files:
        raise UploadError('No yyyy-mm-web.pdf files found.')
    for year in sorted({year for year, _, _ in files}):
        months = {month for y, month, _ in files if y == year}
        missing = [calendar.month_abbr[m] for m in range(1, 13) if m not in months]
        print(f'  {year}: {len(months)} issues' + ('; missing ' + ', '.join(missing) if missing else ''))
    print(f'Target: {args.url} → {args.collection} → Horological Times → year → issue')
    if args.dry_run:
        print('Dry run: no connection made and no changes written.')
        return
    if args.limit < 0:
        raise UploadError('--limit must be zero (all) or a positive number.')
    state_path = (args.state or folder / 'horological-times-upload.json').expanduser().resolve()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock = state_path.with_suffix('.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise UploadError('Another upload process is using this progress file.') from None
    target = {'url': args.url.rstrip('/'), 'collection': args.collection}
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'target': target, 'issues': {}}
    if state.get('target') != target:
        raise UploadError('Progress file belongs to a different Outline destination.')
    token = os.environ.get('OUTLINE_API_KEY') or getpass.getpass('Outline API key (hidden): ')
    if not token.strip():
        raise UploadError('No API key supplied.')
    api = Outline(target['url'], token.strip())
    browser = PreviewBrowser(target['url'], args.profile.expanduser().resolve(), args.browser_url)
    try:
        collections = [c for c in api.listing('collections.list') if c['name'] == args.collection]
        if len(collections) != 1:
            raise UploadError(f'Expected exactly one visible collection named {args.collection!r}; '
                              f'found {len(collections)}. No collection will be created.')
        collection_id = collections[0]['id']
        if state.get('collection_id', collection_id) != collection_id:
            raise UploadError('The destination collection changed since the previous run.')
        state['collection_id'] = collection_id
        docs = list(api.listing('documents.list', collectionId=collection_id,
                                sort='createdAt', direction='ASC'))

        def ensure_page(title, parent, text, expected_id=None):
            if expected_id:
                existing = api.info(expected_id)
                if (existing['title'] != title or existing.get('parentDocumentId') != parent
                        or existing.get('collectionId') != collection_id
                        or existing.get('archivedAt') or existing.get('deletedAt')):
                    raise UploadError(f'The recorded page {title!r} changed or moved; stopping for review.')
                return existing, False
            matches = [d for d in docs if d['title'] == title and
                       d.get('parentDocumentId') == parent and not d.get('archivedAt')]
            if len(matches) > 1:
                raise UploadError(f'Multiple pages named {title!r} under the same parent.')
            if matches:
                return matches[0], False
            doc = api.call('documents.create', title=title, text=text, publish=True,
                           collectionId=collection_id, parentDocumentId=parent,
                           fullWidth=True)['data']
            docs.append(doc)
            return doc, True

        root, _ = ensure_page('Horological Times', None,
                              'Horological Times archive, organised by year and month.', state.get('root_id'))
        state['root_id'] = root['id']
        state['root_url'] = api.absolute(root['url'])
        save_state(state_path, state)
        completed = skipped = 0
        years = {}
        for year, month, path in files:
            if args.limit and completed >= args.limit:
                break
            fingerprint = digest(path)
            entry = state['issues'].get(path.name, {})
            if entry.get('sha256') and entry['sha256'] != fingerprint:
                raise UploadError(f'{path.name} changed since the earlier upload; stopping for review.')
            title = f'{year}-{month:02d} — {calendar.month_name[month]}'
            marker = f'Archive importer source: {path.name}'
            if year not in years:
                years[year], _ = ensure_page(year, root['id'], f'Horological Times — {year}.',
                                             state.get('years', {}).get(year))
                state.setdefault('years', {})[year] = years[year]['id']
                save_state(state_path, state)
            doc, created = ensure_page(title, years[year]['id'], marker + '\n\nUpload pending.',
                                       entry.get('document_id'))
            if entry.get('document_id') and entry['document_id'] != doc['id']:
                raise UploadError(f'The page for {path.name} changed or moved; stopping for review.')
            current = api.info(doc['id'])
            if not isinstance(current.get('data'), dict):
                raise UploadError('This Outline version did not return structured document content '
                                  'with API version 3. No PDF has been uploaded for this issue. '
                                  'Report your Outline version before proceeding.')
            if not created and not entry:
                # A crash after page creation but before saving progress can be recovered.
                content_text = ' '.join(n.get('text', '') for n in walk(current.get('data', {})))
                content_text += ' ' + current.get('text', '')
                if marker not in content_text or any(n.get('type') == 'attachment' for n in walk(current.get('data', {}))):
                    raise UploadError(f'Existing issue page {title!r} is not an empty importer page. '
                                      'It has been left unchanged.')
            entry.update({'document_id': doc['id'], 'document_url': api.absolute(doc['url']),
                          'sha256': fingerprint, 'bytes': path.stat().st_size,
                          'year': year, 'month': month, 'source_path': str(path)})
            state['issues'][path.name] = entry
            save_state(state_path, state)
            node = pdf_node(current, entry.get('attachment_id', 'NO_ATTACHMENT'))
            if entry.get('complete') and node and node.get('attrs', {}).get('preview'):
                skipped += 1
                print(f'Skipping completed {path.name}', flush=True)
                continue
            print(f'Uploading {path.name} ({path.stat().st_size / 1048576:.1f} MiB)', flush=True)
            if not entry.get('attachment_id'):
                entry['attachment_id'] = api.upload(path, doc['id'])
                save_state(state_path, state)
            if not node:
                text = (marker + '\n\n' +
                        f'[{path.name} {path.stat().st_size}]'
                        f'(/api/attachments.redirect?id={entry["attachment_id"]})\n')
                api.call('documents.update', id=doc['id'], text=text, publish=True)
            browser.enable(entry['document_url'], entry['attachment_id'])
            # Verify the editor's collaborative save through the server API.
            for attempt in range(20):
                current = api.info(doc['id'])
                node = pdf_node(current, entry['attachment_id'])
                if node and node.get('attrs', {}).get('preview') is True:
                    break
                time.sleep(2)
            else:
                raise UploadError('Preview was not confirmed saved. Check Outline collaboration '
                                  'connectivity, then rerun to resume.')
            entry['complete'] = True
            entry['pdf_url'] = api.absolute('/api/attachments.redirect?id=' + entry['attachment_id'])
            save_state(state_path, state)
            completed += 1
            print('  Embedded and verified: ' + entry['document_url'], flush=True)
        print(f'\nFinished this run: {completed} issues embedded, {skipped} already complete.')
        print('Archive: ' + state['root_url'])
        print('Progress and future index links: ' + str(state_path))
        if args.limit:
            print('Run again without --limit to process the remaining issues.')
    finally:
        browser.close()
        api.session.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--folder', type=Path, default=Path('~/Downloads/awci'))
    parser.add_argument('--url', default='http://andyswatches.home:3333')
    parser.add_argument('--browser-url', default='https://wiki.andyswatches.com',
                        help='Outline address used by Chromium for login and PDF previews; independent of --url.')
    parser.add_argument('--collection', default='Collaboration')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--profile', type=Path, default=Path('~/.config/outline-archive-browser'))
    parser.add_argument('--dry-run', action='store_true', help='Validate filenames locally; make no changes.')
    parser.add_argument('--limit', type=int, default=0, help='Complete at most this many issues in this run; 0 means all.')
    args = parser.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        print('\nStopped. Rerun the same command to resume.', file=sys.stderr)
        return 130
    except (UploadError, OSError, ValueError, ImportError) as exc:
        print(f'\nStopped: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        # Browser errors can contain page internals; keep the summary compact.
        print(f'\nStopped ({type(exc).__name__}). Check the browser login and editor, '
              'then rerun. Completed issues are recorded in the progress file.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
