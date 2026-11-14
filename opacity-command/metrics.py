"""Proxmox node metrics collected without blocking the GTK main thread."""
import json
import math
import subprocess
from pathlib import Path

from core import ApiError, Provider, segment


POLL_SECONDS = 30
SERVERS = (
    {'key': 'he', 'label': 'HE', 'ip': '192.168.1.206', 'script': Path.home() / 'proxmox_he_ssh.sh'},
    {'key': 'li', 'label': 'LI', 'ip': '192.168.1.204', 'script': Path.home() / 'proxmox_li_ssh.sh'},
)


def clamp_fraction(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, number))


def memory_fraction(memory):
    if not isinstance(memory, dict):
        return None
    try:
        total = float(memory['total'])
        used = float(memory['used'])
    except (KeyError, TypeError, ValueError):
        return None
    return clamp_fraction(used / total) if total > 0 else None


def hottest_temperature(payload):
    """Return the hottest plausible CPU temperature from API or sensors JSON."""
    if isinstance(payload, list):
        values = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                value = float(item['temperature'])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value) and -20 <= value <= 150:
                values.append(value)
        return max(values) if values else None

    if not isinstance(payload, dict):
        return None
    cpu_values, other_values = [], []

    def walk(value, path=()):
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            child_path = path + (str(key),)
            if isinstance(child, dict):
                walk(child, child_path)
                continue
            if not str(key).lower().endswith(('_input', 'temperature')):
                continue
            try:
                reading = float(child)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(reading) or not -20 <= reading <= 150:
                continue
            name = ' '.join(child_path).lower()
            candidates = cpu_values if any(word in name for word in
                ('coretemp', 'k10temp', 'k8temp', 'zenpower', 'cpu', 'package', 'tctl', 'tdie')) else other_values
            candidates.append(reading)

    walk(payload)
    values = cpu_values or other_values
    return max(values) if values else None


def read_ssh_metrics(script, runner=subprocess.run):
    """Use a helper's non-interactive mode; never open a password prompt."""
    try:
        result = runner([str(script), '--metrics'], timeout=12, capture_output=True,
                        text=True, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0 or len(result.stdout) > 1024 * 1024:
        return {}
    prefix, marker, sensor_json = result.stdout.partition('OC_SENSORS\n')
    values = {}
    if marker:
        for line in prefix.splitlines():
            key, separator, raw = line.partition('\t')
            if not separator:
                continue
            if key == 'OC_CPU':
                values['cpu'] = clamp_fraction(raw)
            elif key == 'OC_MEM':
                values['memory'] = clamp_fraction(raw)
    else:
        sensor_json = result.stdout
    try:
        values['temperature'] = hottest_temperature(json.loads(sensor_json))
    except (json.JSONDecodeError, TypeError):
        values['temperature'] = None
    return values


def read_ssh_temperature(script, runner=subprocess.run):
    return read_ssh_metrics(script, runner).get('temperature')


def _cluster_nodes(provider):
    rows = provider.call('/cluster/status')
    if not isinstance(rows, list):
        raise ApiError('Proxmox returned an invalid cluster status response.')
    return [row for row in rows if isinstance(row, dict) and row.get('type') == 'node']


def _match_nodes(rows):
    """Match stable sidebar slots to cluster nodes by IP, then by HE/LI name."""
    remaining = list(rows)
    matched = {}
    for server in SERVERS:
        row = next((r for r in remaining if str(r.get('ip', '')).split('%', 1)[0] == server['ip']), None)
        if row is None:
            key = server['key']
            row = next((r for r in remaining if str(r.get('name') or r.get('node', '')).lower().startswith(key)), None)
        if row is not None:
            matched[server['key']] = row
            remaining.remove(row)
    if len(rows) == len(SERVERS):
        for server in SERVERS:
            if server['key'] not in matched and remaining:
                matched[server['key']] = remaining.pop(0)
    return matched


def collect(config, secret, runner=subprocess.run):
    """Collect two server snapshots. Missing sources leave only that value unset."""
    snapshots = {server['key']: {
        'label': server['label'], 'node': None, 'cpu': None, 'memory': None,
        'temperature': None, 'api_error': None,
    } for server in SERVERS}

    if secret:
        try:
            provider = Provider('proxmox', config, secret)
            matched = _match_nodes(_cluster_nodes(provider))
            for server in SERVERS:
                snapshot = snapshots[server['key']]
                row = matched.get(server['key'])
                if row is None:
                    snapshot['api_error'] = 'Node not found in Proxmox cluster.'
                    continue
                node = str(row.get('name') or row.get('node', '')).strip()
                if not node:
                    snapshot['api_error'] = 'Proxmox returned a node without a name.'
                    continue
                snapshot['node'] = node
                status = provider.call('/nodes/' + segment(node) + '/status')
                snapshot['cpu'] = clamp_fraction(status.get('cpu'))
                snapshot['memory'] = memory_fraction(status.get('memory'))
                try:
                    sensor_rows = provider.call('/nodes/' + segment(node) + '/hardware/sensors/temperature/cpu')
                    snapshot['temperature'] = hottest_temperature(sensor_rows)
                except ApiError:
                    pass
        except (ApiError, ValueError) as error:
            for snapshot in snapshots.values():
                snapshot['api_error'] = str(error)
    else:
        for snapshot in snapshots.values():
            snapshot['api_error'] = 'Configure the Proxmox API token to load CPU and memory.'

    for server in SERVERS:
        snapshot = snapshots[server['key']]
        if all(snapshot[metric] is not None for metric in ('temperature', 'cpu', 'memory')):
            continue
        ssh_values = read_ssh_metrics(server['script'], runner)
        for metric in ('temperature', 'cpu', 'memory'):
            if snapshot[metric] is None and ssh_values.get(metric) is not None:
                snapshot[metric] = ssh_values[metric]
    return snapshots
