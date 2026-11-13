"""Isolated tests: temporary database, fake contacts, no external email."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import server

class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        server.DATA = Path(cls.temp.name)
        server.LIVE = False
        cls.env = patch.dict(server.os.environ, {'ADMIN_PASSWORD': 'unit-test-password-only-12345'}, clear=True)
        cls.env.start()
        server.init()
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        server.BASE = f'http://127.0.0.1:{cls.http.server_port}'
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.env.stop()
        cls.temp.cleanup()

    def request(self, path, payload=None, token=None, cookie=None, origin=None):
        headers = {'Content-Type': 'application/json', 'Origin': origin or server.BASE}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if cookie:
            headers['Cookie'] = cookie
        req = Request(server.BASE+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as e:
            response = e
        raw = response.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode()
        return response.status, body, response.headers

    def test_international_pages(self):
        for page, language in [('/', 'en'), ('/en.html','en'), ('/zh.html','zh-CN')]:
            status, body, headers = self.request(page)
            self.assertEqual(status, 200)
            self.assertIn(f'<html lang="{language}">', body)
            self.assertIn('id="main-content"', body)
            self.assertIn('id="international"', body)
            self.assertIn('id="medical-travel"', body)
            self.assertIn('宜府（亞太）有限公司', body)
            self.assertIn('Beneficial to Family Limited', body)
            self.assertIn('rel="canonical"', body)
            self.assertIn('https://linksinocare.com/en.html', body)
            self.assertIn('application/ld+json', body)
            self.assertNotIn('Tongji Hospital', body)
            self.assertNotIn('Shanghai East Hospital', body)
            self.assertNotIn('资源介绍不代表医院背书', body)
            self.assertNotIn('独立医疗协调服务开发版本', body)
            self.assertNotIn('不承诺签证、预约或疗效。', body)
            self.assertIn('mailto:yeis@163.com', body)
            self.assertIn('iatatravelcentre.com', body)
            faq = server.re.findall(r'<summary>(.*?)</summary>', body)[:5]
            self.assertEqual(len(faq), 5)
            if language == 'zh-CN':
                self.assertEqual(faq[3:], ['能保证约到指定专家吗？','我能顺利获得中国的签证吗？'])
            self.assertIn('href="https://wa.me/85257495657"', body)
            self.assertIn('rel="noopener noreferrer"', body)
            self.assertIn('<h1>', body)
            self.assertIn('noindex', headers['X-Robots-Tag'])
            self.assertIn('geolocation=()', headers['Permissions-Policy'])
            self.assertNotIn('googleapis', headers['Content-Security-Policy'])
        for asset in ['/international.js', '/favicon.svg', '/robots.txt']:
            self.assertEqual(self.request(asset)[0], 200)
        self.assertEqual(self.request('/build-pages.mjs')[0], 404)

    def test_responsive_family_images(self):
        for asset in ['hero-family.jpg', 'hero-family-small.jpg', 'family-shanghai.jpg', 'family-shanghai-small.jpg']:
            with urlopen(server.BASE + '/' + asset, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), 'image/jpeg')
                self.assertTrue(response.read().startswith(b'\xff\xd8'))
        for page in ['/en.html', '/zh.html']:
            _, body, _ = self.request(page)
            self.assertIn('hero-family-small.jpg 720w', body)
            self.assertIn('loading="lazy"', body)
            self.assertIn('family-shanghai.jpg', body)

    def test_search_visibility_boundaries(self):
        status, body, _ = self.request('/sitemap.xml')
        self.assertEqual(status, 200)
        self.assertNotIn('portal', body)
        self.assertIn('https://linksinocare.com/zh.html', body)
        with patch.object(server, 'BASE', 'https://linksinocare.com'):
            # Call the actual local server while simulating the production base URL.
            for path, expected in [('/en.html', 'index, follow'), ('/portal.html', 'noindex, nofollow'), ('/admin.html', 'noindex, nofollow')]:
                response = urlopen(f'http://127.0.0.1:{self.http.server_port}'+path)
                self.assertEqual(response.headers['X-Robots-Tag'], expected)
                if path == '/en.html':
                    html = response.read().decode()
                    block = server.re.search(r'<script type="application/ld\+json">(.*?)</script>', html).group(1)
                    graph = json.loads(block)['@graph']
                    self.assertEqual(graph[0]['legalName'], 'Beneficial to Family Limited')
                    self.assertIn('sha256-', response.headers['Content-Security-Policy'])

    def test_workflow_and_access(self):
        status, _, _ = self.request('/api/admin/tickets')
        self.assertEqual(status, 401)
        self.assertEqual(self.request('/private-data/admin-password.txt')[0], 404)
        self.assertEqual(self.request('/server.py')[0], 404)
        self.assertEqual(self.request('/api/admin/login', {'password': server.PASSWORD}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/admin/login', {'password': 'wrong'})[0], 401)
        status, _, headers = self.request('/api/admin/login', {'password': server.PASSWORD})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        cookie = headers['Set-Cookie'].split(';')[0]
        payload = dict(name='Fictional Test', email='test@example.com', country='Test region', language='zh', service='specialist', consent=True)
        self.assertEqual(self.request('/api/tickets', {**payload, 'consent': False})[0], 400)
        status, result, _ = self.request('/api/tickets', payload)
        self.assertEqual(status, 201)
        self.assertEqual(result['notification'], 'not_configured')
        ref, key = result['id'], result['token']
        endpoint = '/api/tickets/'+ref
        self.assertEqual(self.request(endpoint)[0], 403)
        self.assertEqual(self.request(endpoint, token='wrong')[0], 403)
        status, ticket, _ = self.request(endpoint, token=key)
        self.assertEqual(status, 200)
        self.assertNotIn('token_hash', ticket)
        self.assertEqual(self.request(endpoint+'/messages', {'body': 'Please arrange a call.'}, token=key)[0], 200)
        admin = '/api/admin/tickets/'+ref
        status, ticket, _ = self.request(admin, cookie=cookie)
        self.assertEqual(ticket['messages'][0]['body'], 'Please arrange a call.')
        self.assertEqual(self.request(admin+'/messages', {'body': 'What time is convenient?'}, cookie=cookie)[0], 200)
        _, ticket, _ = self.request(endpoint, token=key)
        self.assertEqual(ticket['messages'][-1]['role'], 'admin')
        self.assertEqual(ticket['status'], 'waiting_customer')
        self.assertEqual(self.request(admin+'/status', {'status': 'closed'}, cookie=cookie)[0], 200)
        self.assertEqual(self.request(endpoint+'/messages', {'body': 'Cannot send while closed'}, token=key)[0], 409)
        self.assertEqual(self.request(admin+'/status', {'status': 'reviewing'}, cookie=cookie)[0], 200)
        self.assertEqual(self.request(endpoint+'/messages', {'body': 'x'*2001}, token=key)[0], 400)
        with server.db() as conn:
            self.assertEqual(conn.execute('SELECT token_hash FROM tickets WHERE id=?', (ref,)).fetchone()[0], server.digest(key))
        self.assertEqual(self.request('/api/admin/logout', {}, cookie=cookie)[0], 200)
        self.assertEqual(self.request('/api/admin/tickets', cookie=cookie)[0], 401)

    def test_email_acceptance_and_failure(self):
        cfg = dict(SMTP_HOST='smtp.example.com', SMTP_USER='test', SMTP_PASSWORD='not-real', MAIL_FROM='test@example.com', ADMIN_EMAIL='admin@example.com')
        with patch.dict(server.os.environ, cfg), patch('server.smtplib.SMTP') as smtp:
            self.assertEqual(server.notify('BF-0000000000', 'admin', 'client@example.com'), 'accepted')
            message = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
            self.assertEqual(message['To'], 'admin@example.com')
            self.assertNotIn('token', message.get_content())
            smtp.side_effect = OSError('Simulated failure')
            self.assertEqual(server.notify('BF-0000000000', 'customer', 'client@example.com'), 'failed')

if __name__ == '__main__':
    unittest.main(verbosity=2)
