"""Configuration and authenticated provider adapters. No third-party HTTP dependencies."""
import copy
import json
import os
import re
import ssl
import tempfile
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

SERVICES = [
    ('proxmox', 'Proxmox', 'Infrastructure', 'Virtual machines & containers', 'https://216.226.81.232:8006'),
    ('linode', 'Linode', 'Infrastructure', 'Cloud compute & server power', 'https://cloud.linode.com/linodes'),
    ('hestia', 'HestiaCP', 'Infrastructure', 'Websites & hosting accounts', 'https://216.226.81.232:8083'),
    ('cloudflare', 'Cloudflare', 'Domains & DNS', 'DNS records & edge protection', 'https://dash.cloudflare.com/054b5cb2ec72c001d86f8f43b6841bbe/domains/overview'),
    ('namecheap', 'Namecheap', 'Domains & DNS', 'Domain portfolio & nameservers', 'https://ap.www.namecheap.com/'),
    ('mail', 'Proton Mail', 'Workspace', 'Business inbox & conversations', 'https://mail.proton.me/'),
    ('meet', 'Proton Meet', 'Workspace', 'Private calls & meetings', 'https://meet.proton.me/'),
    ('drive', 'Proton Drive', 'Workspace', 'Files, documents & sharing', 'https://drive.proton.me/'),
]
FIELDS = {
    'proxmox': [('base', 'API origin', 'https://216.226.81.232:8006'), ('token_id', 'Token ID (user@realm!token)', ''), ('ca', 'Trusted CA PEM file (optional)', '')],
    'linode': [],
    'cloudflare': [('zone', 'Zone ID for opacitytech.com', '')],
    'namecheap': [('user', 'Namecheap API username', ''), ('client_ip', 'Allowlisted public IPv4 address', '')],
    'hestia': [('base', 'API origin', 'https://216.226.81.232:8083'), ('user', 'Hosting account username', ''), ('ca', 'Trusted CA PEM file (optional)', '')],
}
DEFAULT = {'urls': {s[0]: s[4] for s in SERVICES}, 'ssh_user': 'otadmin',
           'providers': {k: {f: v for f, _, v in fields} for k, fields in FIELDS.items()}}
CONFIG = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'opacity-command/config.json'


def valid_url(value, origin=False):
    p = urllib.parse.urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or any(c.isspace() for c in value):
        raise ValueError('Enter an HTTPS URL without credentials or spaces.')
    _ = p.port
    if origin and (p.path not in ('', '/') or p.query or p.fragment):
        raise ValueError('API origin must contain only https://hostname and optional port.')
    return value.rstrip('/') if origin else value


def load_config(path=CONFIG):
    c = copy.deepcopy(DEFAULT)
    if path.exists():
        raw = json.loads(path.read_text())
        c['urls'].update(raw.get('urls', {}))
        c['ssh_user'] = raw.get('ssh_user', '')
        for k in FIELDS:
            c['providers'][k].update(raw.get('providers', {}).get(k, {}))
    return c


def save_config(c, path=CONFIG):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(c, f, indent=2)
            f.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def ssh_args(user):
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*\$?', user):
        raise ValueError('Set a valid SSH username in Settings first.')
    return ['ssh', '-p', '22', '-o', 'ConnectTimeout=15', '-l', user, '216.226.81.232']


class ApiError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError('API redirect refused. Check the configured API origin.')


def request(url, method='GET', data=None, headers=None, ca='', form=False, xml=False):
    valid_url(url)
    headers = dict(headers or {})
    headers['Accept'] = 'application/xml' if xml else 'application/json'
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
    try:
        context = ssl.create_default_context(cafile=ca or None)
        if ca:
            # Older private CAs (including Proxmox) omit extensions required
            # by strict X.509 checks. Still verify the chain and hostname.
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        with opener.open(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=20) as r:
            body = r.read(8 * 1024 * 1024 + 1)
        if len(body) > 8 * 1024 * 1024:
            raise ApiError('API response exceeds 8 MB.')
        if xml:
            return ET.fromstring(body)
        return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        raise ApiError(f'HTTP {e.code}. Check API permissions, credentials and provider settings. For 429, wait before retrying.') from None
    except (urllib.error.URLError, ssl.SSLError, OSError):
        raise ApiError('Connection failed. Check the network, API origin and TLS certificate. For private certificates, set a trusted CA file and matching hostname.') from None
    except (ValueError, ET.ParseError):
        raise ApiError('The server returned an invalid response; verify the API endpoint.') from None


def segment(value):
    return urllib.parse.quote(str(value), safe='')


class Provider:
    def __init__(self, kind, config, token, transport=request):
        self.kind, self.c, self.token, self.request = kind, copy.deepcopy(config), token, transport
        if not token:
            raise ApiError('No API credential saved. Open Configure API first.')
        for key, label, _ in FIELDS[kind]:
            if key != 'ca' and not self.c.get(key) and not (self.kind == 'hestia' and key == 'user'):
                raise ApiError(f'Configure {label} first.')
        if 'base' in self.c:
            valid_url(self.c['base'], origin=True)

    def call(self, path, method='GET', data=None):
        if self.kind == 'proxmox':
            result = self.request(self.c['base'].rstrip('/') + '/api2/json' + path, method, data,
                {'Authorization': f"PVEAPIToken={self.c['token_id']}={self.token}"}, ca=self.c.get('ca', ''))
            return result['data']
        base = 'https://api.linode.com/v4' if self.kind == 'linode' else 'https://api.cloudflare.com/client/v4'
        result = self.request(base + path, method, data, {'Authorization': 'Bearer ' + self.token})
        if self.kind == 'cloudflare' and result.get('success') is not True:
            raise ApiError('Cloudflare rejected the request. Check zone and token permissions.')
        return result

    def nc(self, command, **params):
        root = self.request('https://api.namecheap.com/xml.response', 'POST', {
            'ApiUser': self.c['user'], 'UserName': self.c['user'], 'ApiKey': self.token,
            'ClientIp': self.c['client_ip'], 'Command': 'namecheap.' + command, **params}, form=True, xml=True)
        if root.get('Status') != 'OK':
            codes = [e.get('Number', '?') for e in root.iter() if e.tag.endswith('}Error')]
            raise ApiError('Namecheap rejected the request (codes: ' + ', '.join(codes) + '). Check API access and IP allowlist.')
        return root

    def hestia(self, command, *args, listing=False):
        data = {'hash': self.token, 'cmd': command, 'returncode': 'no' if listing else 'yes'}
        data.update({f'arg{i}': value for i, value in enumerate(args, 1)})
        result = self.request(self.c['base'].rstrip('/') + '/api/', 'POST', data, ca=self.c.get('ca', ''), form=True)
        if listing and not isinstance(result, dict):
            raise ApiError('Hestia listing failed. Check access-key command permissions and username.')
        if not listing and result != 0:
            raise ApiError('Hestia rejected the command. Check permissions and server logs.')
        return result

    def list_resources(self):
        k = self.kind
        if k == 'proxmox':
            return [r for r in self.call('/cluster/resources?type=vm') if r.get('type') in ('qemu', 'lxc')]
        if k in ('linode', 'cloudflare'):
            path = '/linode/instances' if k == 'linode' else '/zones/' + segment(self.c['zone']) + '/dns_records'
            rows, page = [], 1
            while True:
                result = self.call(path + f'?page={page}&' + ('page_size=100' if k == 'linode' else 'per_page=100'))
                rows.extend(result['data' if k == 'linode' else 'result'])
                pages = result.get('pages', 1) if k == 'linode' else result.get('result_info', {}).get('total_pages', 1)
                if page >= pages:
                    return rows
                page += 1
        if k == 'hestia':
            if self.c.get('user'):
                users = [self.c['user']]
            else:
                try:
                    users = list(self.hestia('v-list-users', 'json', listing=True).keys())
                except ApiError as e:
                    raise ApiError('Could not enumerate Hestia users. Grant the key v-list-users permission, or configure a specific hosting username.') from e
            rows = []
            for user in users:
                domains = self.hestia('v-list-web-domains', user, 'json', listing=True)
                rows.extend([dict(v, name=name, _hestia_user=user) for name, v in domains.items()])
            return rows
        rows, page = [], 1
        while True:
            root = self.nc('domains.getList', Page=page, PageSize=100)
            rows.extend(dict(e.attrib) for e in root.iter() if e.tag.endswith('}Domain'))
            total = next((int(e.text) for e in root.iter() if e.tag.endswith('}TotalItems')), len(rows))
            if len(rows) >= total:
                return rows
            page += 1
            if page > 1000:
                raise ApiError('Unexpected Namecheap pagination.')

    def act(self, row, action, value=None):
        if self.kind == 'proxmox' and action in ('start', 'shutdown', 'reboot', 'suspend', 'resume'):
            path = '/nodes/{}/{}/{}/status/{}'.format(segment(row['node']), segment(row['type']), segment(row['vmid']), action)
            return self.call(path, 'POST', {})
        if self.kind == 'linode' and action in ('boot', 'shutdown', 'reboot'):
            return self.call('/linode/instances/' + segment(row['id']) + '/' + action, 'POST', {})
        if self.kind == 'cloudflare' and action == 'content':
            if not value.strip():
                raise ValueError('DNS content cannot be empty.')
            # PATCH changes only content; TTL, proxy, priority and other fields are preserved.
            return self.call('/zones/' + segment(self.c['zone']) + '/dns_records/' + segment(row['id']), 'PATCH', {'content': value})
        if self.kind == 'namecheap' and action == 'nameservers':
            names = [x.strip().lower() for x in value.split(',')]
            if len(names) < 2 or any(not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', x) or '.' not in x for x in names):
                raise ValueError('Enter at least two nameserver hostnames, separated by commas.')
            sld, tld = row['Name'].split('.', 1)
            root = self.nc('domains.dns.setCustom', SLD=sld, TLD=tld, NameServers=','.join(names))
            if not any(e.get('Updated', '').lower() == 'true' for e in root.iter()):
                raise ApiError('Namecheap did not confirm the nameserver update.')
            return 'Nameservers updated'
        if self.kind == 'hestia' and action == 'rebuild':
            return self.hestia('v-rebuild-web-domain', self.c['user'], row['name'], 'yes')
        if self.kind == 'hestia' and action == 'suspend':
            return self.hestia('v-suspend-web-domain', row.get('_hestia_user', self.c['user']), row['name'], 'yes')
        if self.kind == 'hestia' and action == 'unsuspend':
            return self.hestia('v-unsuspend-web-domain', row.get('_hestia_user', self.c['user']), row['name'], 'yes')
        if self.kind == 'hestia' and action.startswith('restart_'):
            service = {'restart_web': 'v-restart-web', 'restart_proxy': 'v-restart-proxy', 'restart_dns': 'v-restart-dns', 'restart_mail': 'v-restart-mail', 'restart_ftp': 'v-restart-ftp'}.get(action)
            if service:
                return self.hestia(service)
        if self.kind == 'hestia' and action == 'edit':
            new_name = (value or '').strip().lower()
            if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', new_name):
                raise ValueError('Enter a valid new domain name.')
            return self.hestia('v-change-web-domain-name', row.get('_hestia_user', self.c['user']), row['name'], new_name, 'yes')
        raise ValueError('Unsupported operation.')
