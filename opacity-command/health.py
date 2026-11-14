"""Background TCP, HTTPS and provider status checks for the dashboard."""
import http.client
import json
import ssl
import subprocess
from urllib.parse import urlsplit
from core import valid_url

POLL_SECONDS = 300
CLOUDFLARE_STATUS_URL = 'https://www.cloudflarestatus.com/api/v2/summary.json'
CLOUDFLARE_COMPONENTS = {
    'dp8ppfycqxcs': 'Authoritative DNS',
    '8w536gxk7dvq': 'Recursive DNS',
    '5wnz34mhfhrk': 'CDN/Cache',
}


def cloudflare_summary(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('components'), list):
        return None, 'Cloudflare status feed is invalid.'
    components = {c.get('id'): c for c in payload['components'] if isinstance(c, dict)}
    details = []
    known = {'operational', 'degraded_performance', 'partial_outage', 'major_outage', 'under_maintenance'}
    states = []
    for component_id, name in CLOUDFLARE_COMPONENTS.items():
        state = components.get(component_id, {}).get('status')
        if state not in known:
            return None, f'Cloudflare status unavailable for {name}.'
        states.append(state)
        details.append(f'{name}: {state.replace("_", " ")}')
    return all(state == 'operational' for state in states), '\n'.join(details)


def check_cloudflare(connection_factory=http.client.HTTPSConnection):
    connection = None
    try:
        connection = connection_factory('www.cloudflarestatus.com', 443, timeout=10, context=ssl.create_default_context())
        connection.request('GET', '/api/v2/summary.json', headers={
            'Accept': 'application/json', 'Cache-Control': 'no-cache',
            'User-Agent': 'OpacityCommand/1.0 availability', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            return None, f'Cloudflare status feed returned HTTP {response.status}.'
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None, 'Cloudflare status feed exceeds the size limit.'
        return cloudflare_summary(json.loads(body))
    except (OSError, http.client.HTTPException, ValueError):
        return None, 'Could not read Cloudflare’s official status feed; service health is unknown.'
    finally:
        if connection is not None:
            connection.close()


def check_proxmox(runner=subprocess.run):
    command = ['nc', '-vz', '216.226.81.232', '8006']
    try:
        result = runner(command, timeout=10, capture_output=True, text=True, check=False)
        up = result.returncode == 0
        return up, 'TCP port 8006 accepts connections.' if up else 'TCP port 8006 is unreachable or refused the connection.'
    except subprocess.TimeoutExpired:
        return False, 'TCP port 8006 did not respond within 10 seconds.'
    except OSError:
        return None, 'Could not run nc; check that netcat is installed.'


def check_target(key, url, ca=''):
    if key == 'proxmox':
        return check_proxmox()
    return check_cloudflare() if key == 'cloudflare' else check_https(url, ca)



def targets(config):
    return {
        'proxmox': ('nc -vz 216.226.81.232 8006', ''),
        'website': ('https://opacitytech.com/', ''),
        'cloudflare': (CLOUDFLARE_STATUS_URL, ''),
    }


def check_https(url, ca='', connection_factory=http.client.HTTPSConnection):
    """Check one endpoint without credentials, redirects, or response-body downloads.

    Login/access restrictions prove the service is responding. A certificate
    error is reported as Down because a verified HTTPS connection failed.
    This is endpoint availability from this desktop, not global provider health.
    """
    connection = None
    try:
        valid_url(url)
        parsed = urlsplit(url)
        context = ssl.create_default_context(cafile=ca or None)
        connection = connection_factory(parsed.hostname, parsed.port or 443, timeout=10, context=context)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request('GET', path, headers={'User-Agent': 'OpacityCommand/1.0 availability', 'Connection': 'close'})
        response = connection.getresponse()
        code = response.status
        up = 200 <= code < 400 or code in (401, 403)
        return up, f'HTTP {code}' + (' (login/access restricted, service responding)' if code in (401, 403) else '')
    except ssl.SSLCertVerificationError:
        return False, 'HTTPS certificate verification failed.'
    except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException, ValueError):
        return False, 'HTTPS check failed (network, timeout, TLS, or endpoint configuration).'
    finally:
        if connection is not None:
            connection.close()
