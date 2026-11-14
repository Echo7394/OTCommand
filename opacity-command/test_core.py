import json
import tempfile
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from core import Provider, ApiError, NoRedirect, ssh_args, valid_url, save_config, load_config, DEFAULT

class CoreTests(unittest.TestCase):
    def test_ssh_no_shell_or_options_in_user(self):
        self.assertEqual(ssh_args('otadmin')[-3:], ['-l', 'otadmin', '216.226.81.232'])
        for user in ('-oProxyCommand=evil', 'root; touch /tmp/evil', 'a\nb', ''):
            with self.assertRaises(ValueError): ssh_args(user)

    def test_https_validation(self):
        for url in ('http://example.com', 'file:///etc/passwd', 'https://user:secret@example.com', 'https://a b'):
            with self.assertRaises(ValueError): valid_url(url)
        with self.assertRaises(ValueError): valid_url('https://example.com/path', origin=True)

    def test_redirect_refused(self):
        with self.assertRaises(ApiError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.com')

    def test_config_private_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            save_config(DEFAULT, path)
            self.assertEqual(load_config(path), DEFAULT)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_linode_pagination_and_power(self):
        calls = []
        def transport(url, method='GET', data=None, headers=None, **kw):
            calls.append((url, method, data, headers))
            return {'data': [{'id': len(calls)}], 'pages': 2} if method == 'GET' else {}
        p = Provider('linode', {}, 'secret', transport)
        self.assertEqual(len(p.list_resources()), 2)
        p.act({'id': 123}, 'reboot')
        self.assertEqual(calls[-1][:3], ('https://api.linode.com/v4/linode/instances/123/reboot', 'POST', {}))
        self.assertEqual(calls[-1][3]['Authorization'], 'Bearer secret')

    def test_proxmox_token_and_task(self):
        calls = []
        def transport(*a, **kw):
            calls.append((a, kw))
            return {'data': 'UPID:task'}
        p = Provider('proxmox', {'base': 'https://pve.example.com', 'token_id': 'ot@pve!app', 'ca': '/tmp/ca.pem'}, 'secret', transport)
        self.assertEqual(p.act({'node': 'pve', 'type': 'qemu', 'vmid': 100}, 'shutdown'), 'UPID:task')
        args, kwargs = calls[-1]
        self.assertTrue(args[0].endswith('/nodes/pve/qemu/100/status/shutdown'))
        self.assertEqual(args[3]['Authorization'], 'PVEAPIToken=ot@pve!app=secret')
        self.assertEqual(kwargs['ca'], '/tmp/ca.pem')

    def test_proxmox_suspend_and_resume(self):
        calls = []
        def transport(*args, **kw):
            calls.append((args, kw))
            return {'data': 'UPID:task'}
        p = Provider('proxmox', {'base': 'https://pve.example.com', 'token_id': 'ot@pve!app', 'ca': '/tmp/ca.pem'}, 'secret', transport)
        row = {'node': 'pve', 'type': 'qemu', 'vmid': 100}
        self.assertEqual(p.act(row, 'suspend'), 'UPID:task')
        self.assertTrue(calls[-1][0][0].endswith('/nodes/pve/qemu/100/status/suspend'))
        self.assertEqual(p.act(row, 'resume'), 'UPID:task')
        self.assertTrue(calls[-1][0][0].endswith('/nodes/pve/qemu/100/status/resume'))

    def test_dns_patch_preserves_unedited_fields(self):
        calls = []
        def transport(*args, **kw):
            calls.append(args)
            return {'success': True, 'result': {}}
        p = Provider('cloudflare', {'zone': 'zone'}, 'secret', transport)
        p.act({'id': 'record', 'ttl': 300, 'proxied': True}, 'content', '1.2.3.4')
        self.assertEqual(calls[0][1:3], ('PATCH', {'content': '1.2.3.4'}))
        self.assertTrue(calls[0][0].endswith('/zones/zone/dns_records/record'))

    def test_namecheap_xml_and_multilabel_tld(self):
        calls = []
        def transport(*args, **kw):
            calls.append((args, kw))
            return ET.fromstring('<ApiResponse xmlns="http://api.namecheap.com/xml.response" Status="OK"><CommandResponse><DomainDNSSetCustomResult Updated="true"/></CommandResponse></ApiResponse>')
        p = Provider('namecheap', {'user': 'ot', 'client_ip': '1.2.3.4'}, 'secret', transport)
        p.act({'Name': 'example.co.uk'}, 'nameservers', 'one.example.net,two.example.net')
        args, kwargs = calls[0]
        self.assertEqual(args[1], 'POST')
        self.assertNotIn('secret', args[0])
        self.assertEqual(args[2]['SLD'], 'example')
        self.assertEqual(args[2]['TLD'], 'co.uk')
        self.assertEqual(args[2]['Command'], 'namecheap.domains.dns.setCustom')
        self.assertTrue(kwargs['xml'])

    def test_hestia_list_and_returncode(self):
        calls = []
        def transport(*args, **kw):
            calls.append(args)
            return {'opacitytech.com': {'IP': '1.2.3.4'}} if args[2]['returncode'] == 'no' else 0
        p = Provider('hestia', {'base': 'https://h.example.com:8083', 'user': 'otadmin'}, 'access:secret', transport)
        row = p.list_resources()[0]
        p.act(row, 'rebuild')
        self.assertEqual(calls[0][2]['arg2'], 'json')
        self.assertEqual(calls[1][2]['hash'], 'access:secret')
        self.assertEqual(calls[1][2]['arg2'], 'opacitytech.com')
        self.assertEqual(calls[1][2]['returncode'], 'yes')

    def test_hestia_domain_actions(self):
        calls = []
        def transport(*args, **kw):
            calls.append(args)
            return 0
        p = Provider('hestia', {'base': 'https://h.example.com:8083', 'user': 'otadmin'}, 'access:secret', transport)
        row = {'name': 'old.example.com'}
        for action in ('suspend', 'unsuspend', 'restart_web', 'restart_proxy', 'restart_dns', 'restart_mail', 'restart_ftp'):
            p.act(row, action)
        p.act(row, 'edit', 'new.example.com')
        self.assertEqual([c[2]['cmd'] for c in calls], ['v-suspend-web-domain', 'v-unsuspend-web-domain', 'v-restart-web', 'v-restart-proxy', 'v-restart-dns', 'v-restart-mail', 'v-restart-ftp', 'v-change-web-domain-name'])
        with self.assertRaises(ValueError): p.act(row, 'edit', 'bad name')

    def test_hestia_enumerates_users_when_username_blank(self):
        def transport(url, method='GET', data=None, headers=None, **kw):
            cmd = data['cmd']
            if cmd == 'v-list-users':
                return {'alice': {}, 'bob': {}}
            if data['arg1'] == 'alice':
                return {'a.example.com': {'IP': '1.2.3.4'}}
            return {'b.example.com': {'IP': '1.2.3.5'}}
        p = Provider('hestia', {'base': 'https://h.example.com:8083', 'user': ''}, 'access:secret', transport)
        rows = p.list_resources()
        self.assertEqual([(r['name'], r['_hestia_user']) for r in rows], [('a.example.com', 'alice'), ('b.example.com', 'bob')])

    def test_provider_rejections(self):
        p = Provider('cloudflare', {'zone': 'z'}, 't', lambda *a, **k: {'success': False})
        with self.assertRaises(ApiError): p.list_resources()
        p = Provider('hestia', {'base': 'https://h.example.com', 'user': 'ot'}, 't', lambda *a, **k: 10)
        with self.assertRaises(ApiError): p.list_resources()
        with self.assertRaises(ApiError): p.act({'name': 'x.com'}, 'rebuild')
        with self.assertRaises(ApiError): Provider('linode', {}, '')

if __name__ == '__main__':
    unittest.main()
