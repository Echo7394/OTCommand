import json
import unittest
from unittest.mock import Mock

from core import ApiError
from metrics import clamp_fraction, collect, hottest_temperature, memory_fraction, read_ssh_metrics, read_ssh_temperature


class FakeProvider:
    def __init__(self, kind, config, secret):
        self.calls = []

    def call(self, path):
        self.calls.append(path)
        if path == '/cluster/status':
            return [
                {'type': 'cluster', 'name': 'cluster'},
                {'type': 'node', 'name': 'lithium', 'ip': '192.168.1.204'},
                {'type': 'node', 'name': 'helium', 'ip': '192.168.1.206'},
            ]
        if path == '/nodes/helium/status':
            return {'cpu': 0.25, 'memory': {'used': 3, 'total': 4}}
        if path == '/nodes/lithium/status':
            return {'cpu': 0.5, 'memory': {'used': 1, 'total': 4}}
        if path.endswith('/hardware/sensors/temperature/cpu'):
            return [{'sensor': 'Package id 0', 'temperature': 48.5}, {'temperature': 51}]
        raise AssertionError(path)


class MetricsTests(unittest.TestCase):
    def test_fraction_normalization(self):
        self.assertEqual(clamp_fraction(-1), 0)
        self.assertEqual(clamp_fraction(1.5), 1)
        self.assertIsNone(clamp_fraction('bad'))
        self.assertEqual(memory_fraction({'used': 3, 'total': 4}), .75)
        self.assertIsNone(memory_fraction({'used': 3, 'total': 0}))

    def test_temperature_formats_and_cpu_preference(self):
        self.assertEqual(hottest_temperature([{'temperature': 41}, {'temperature': 47}]), 47)
        payload = {
            'nvme-pci': {'Composite': {'temp1_input': 70}},
            'coretemp-isa': {'Package id 0': {'temp1_input': 52}, 'Core 0': {'temp2_input': 55}},
        }
        self.assertEqual(hottest_temperature(payload), 55)
        self.assertIsNone(hottest_temperature({'fan': 1200}))

    def test_helper_is_noninteractive_and_bounded(self):
        stdout = 'OC_CPU\t0.125\nOC_MEM\t0.625\nOC_SENSORS\n' + json.dumps({
            'k10temp-pci': {'Tctl': {'temp1_input': 63.25}}
        })
        runner = Mock(return_value=Mock(returncode=0, stdout=stdout))
        self.assertEqual(read_ssh_metrics('/tmp/helper.sh', runner), {
            'cpu': .125, 'memory': .625, 'temperature': 63.25})
        runner.reset_mock()
        self.assertEqual(read_ssh_temperature('/tmp/helper.sh', runner), 63.25)
        runner.assert_called_once_with(['/tmp/helper.sh', '--metrics'], timeout=12,
                                       capture_output=True, text=True, check=False)

    def test_api_collects_both_nodes(self):
        runner = Mock(return_value=Mock(returncode=1, stdout=''))
        from unittest.mock import patch
        with patch('metrics.Provider', FakeProvider):
            rows = collect({'base': 'https://pve.test', 'token_id': 'u!t'}, 'secret', runner)
        self.assertEqual(rows['he']['node'], 'helium')
        self.assertEqual(rows['he']['cpu'], .25)
        self.assertEqual(rows['he']['memory'], .75)
        self.assertEqual(rows['he']['temperature'], 51)
        self.assertEqual(rows['li']['node'], 'lithium')

    def test_ssh_temperature_survives_missing_api_secret(self):
        runner = Mock(return_value=Mock(returncode=0, stdout=(
            'OC_CPU\t0.2\nOC_MEM\t0.3\nOC_SENSORS\n'
            '{"coretemp": {"Package": {"temp1_input": 44}}}')))
        rows = collect({}, None, runner)
        self.assertEqual(rows['he']['temperature'], 44)
        self.assertEqual(rows['he']['cpu'], .2)
        self.assertEqual(rows['he']['memory'], .3)
        self.assertIn('Configure', rows['he']['api_error'])
        self.assertEqual(runner.call_count, 2)


if __name__ == '__main__':
    unittest.main()
