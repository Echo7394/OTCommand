import copy
import ssl
import unittest
from unittest.mock import Mock
from core import DEFAULT
from health import POLL_SECONDS, targets, check_https, cloudflare_summary, check_cloudflare, CLOUDFLARE_COMPONENTS, CLOUDFLARE_STATUS_URL, check_proxmox, check_target

class HealthTests(unittest.TestCase):
    def probe(self, status=200, error=None):
        connection = Mock()
        if error:
            connection.getresponse.side_effect = error
        else:
            connection.getresponse.return_value.status = status
        factory = Mock(return_value=connection)
        result = check_https('https://example.com:8006/api2/json/version', connection_factory=factory)
        connection.close.assert_called_once()
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args, ('example.com', 8006))
        self.assertEqual(factory.call_args.kwargs['timeout'], 10)
        self.assertEqual(factory.call_args.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertNotIn('Authorization', connection.request.call_args.kwargs['headers'])
        return result

    def test_success_redirect_and_login(self):
        for status in (200, 204, 301, 302, 401, 403):
            with self.subTest(status=status):
                self.assertTrue(self.probe(status)[0])

    def test_http_failures(self):
        for status in (404, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertFalse(self.probe(status)[0])

    def test_network_failures(self):
        self.assertFalse(self.probe(error=TimeoutError())[0])
        self.assertFalse(self.probe(error=OSError())[0])
        up, detail = self.probe(error=ssl.SSLCertVerificationError())
        self.assertFalse(up)
        self.assertIn('certificate', detail)

    def test_target_settings_and_schedule(self):
        cfg = copy.deepcopy(DEFAULT)
        cfg['providers']['proxmox'].update(base='https://pve.example.com:8006/', ca='/tmp/private-ca.pem')
        self.assertEqual(targets(cfg)['proxmox'], ('nc -vz 216.226.81.232 8006', ''))
        self.assertEqual(targets(cfg)['website'][0], 'https://opacitytech.com/')
        self.assertEqual(targets(cfg)['cloudflare'][0], CLOUDFLARE_STATUS_URL)
        self.assertEqual(POLL_SECONDS, 300)

    def test_invalid_endpoint_does_not_connect(self):
        factory = Mock()
        self.assertFalse(check_https('http://example.com', connection_factory=factory)[0])
        factory.assert_not_called()

class ProxmoxTcpTests(unittest.TestCase):
    def test_command_and_exit_status(self):
        for code in (0, 1):
            runner = Mock(return_value=Mock(returncode=code))
            self.assertEqual(check_proxmox(runner)[0], code == 0)
            runner.assert_called_once_with(['nc', '-vz', '216.226.81.232', '8006'], timeout=10, capture_output=True, text=True, check=False)

    def test_timeout_and_missing_executable(self):
        import subprocess
        self.assertFalse(check_proxmox(Mock(side_effect=subprocess.TimeoutExpired('nc', 10)))[0])
        self.assertIsNone(check_proxmox(Mock(side_effect=FileNotFoundError()))[0])

    def test_dispatch_uses_tcp(self):
        from unittest.mock import patch
        with patch('health.check_proxmox', return_value=(True, 'TCP')) as tcp:
            self.assertEqual(check_target('proxmox', 'unused'), (True, 'TCP'))
            tcp.assert_called_once_with()

class CloudflareTests(unittest.TestCase):
    def payload(self):
        return {'components': [{'id': key, 'status': 'operational'} for key in CLOUDFLARE_COMPONENTS]}

    def test_operational_and_unrelated_outage(self):
        payload = self.payload()
        payload['components'].append({'id': 'unrelated', 'status': 'major_outage'})
        self.assertTrue(cloudflare_summary(payload)[0])

    def test_each_relevant_service_disruption(self):
        for index in range(3):
            for status in ('degraded_performance', 'partial_outage', 'major_outage', 'under_maintenance'):
                payload = self.payload()
                payload['components'][index]['status'] = status
                self.assertFalse(cloudflare_summary(payload)[0])

    def test_missing_and_invalid_feed_unknown(self):
        for payload in (None, {}, {'components': []}, {'components': [None]}):
            self.assertIsNone(cloudflare_summary(payload)[0])

    def test_network_and_http_failures_unknown(self):
        conn = Mock()
        conn.getresponse.side_effect = TimeoutError()
        self.assertIsNone(check_cloudflare(Mock(return_value=conn))[0])
        conn.close.assert_called_once()
        conn = Mock()
        conn.getresponse.return_value.status = 503
        self.assertIsNone(check_cloudflare(Mock(return_value=conn))[0])
        conn.close.assert_called_once()

    def test_successful_fetch(self):
        import json
        conn = Mock()
        conn.getresponse.return_value.status = 200
        conn.getresponse.return_value.read.return_value = json.dumps(self.payload()).encode()
        self.assertTrue(check_cloudflare(Mock(return_value=conn))[0])
        conn.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
