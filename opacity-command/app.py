#!/usr/bin/python3
"""Opacity Command — native GTK 4 hosting dashboard."""
import copy
import json
import subprocess
import sys
import threading
from pathlib import Path
from health import POLL_SECONDS as HEALTH_POLL_SECONDS, targets, check_target
from metrics import POLL_SECONDS as METRICS_POLL_SECONDS, SERVERS as METRIC_SERVERS, collect as collect_metrics
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Secret', '1')
from gi.repository import Gtk, Gdk, Gio, GLib, Pango, Secret
from core import SERVICES, FIELDS, Provider, ApiError, load_config, save_config, valid_url, ssh_args

ROOT = Path(__file__).resolve().parent
SCHEMA = Secret.Schema.new('com.opacitytech.Command', Secret.SchemaFlags.NONE, {'provider': Secret.SchemaAttributeType.STRING, 'origin': Secret.SchemaAttributeType.STRING})

def secret_attrs(kind, config):
    return {'provider': kind, 'origin': config.get('base', kind).rstrip('/')}

def secret_get(kind, config):
    return Secret.password_lookup_sync(SCHEMA, secret_attrs(kind, config), None)

def secret_set(kind, config, value):
    if not Secret.password_store_sync(SCHEMA, secret_attrs(kind, config), Secret.COLLECTION_DEFAULT, 'Opacity Command · ' + kind, value, None):
        raise ApiError('The desktop keyring could not save the credential.')

def box(vertical=True, spacing=12, css=None):
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if css:
        b.add_css_class(css)
    return b

def label(text, css=None):
    w = Gtk.Label(label=text, xalign=0)
    w.set_wrap(True)
    if css:
        w.add_css_class(css)
    return w

def sidebar_label(text, css=None):
    w = label(text, css)
    w.set_xalign(.5)
    w.set_justify(Gtk.Justification.CENTER)
    return w

def button(text, fn, css=None):
    w = Gtk.Button(label=text)
    if css:
        w.add_css_class(css)
    w.connect('clicked', lambda *_: fn())
    return w

def clear(container):
    while container.get_first_child():
        container.remove(container.get_first_child())

class App(Gtk.Application):
    def __init__(self, smoke=False):
        super().__init__(application_id='com.opacitytech.Command', flags=Gio.ApplicationFlags.NON_UNIQUE if smoke else Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.smoke = smoke
        self.window = None
        self.header_error = None
        self.config_error = None
        self.health_timer = None
        self.health_pending = 0
        self.health_stopped = False
        self.metrics_timer = None
        self.metrics_pending = False
        self.connect('shutdown', self.stop_polling)
        try:
            self.config = load_config()
        except (ValueError, OSError, TypeError, AttributeError) as e:
            from core import DEFAULT
            self.config = copy.deepcopy(DEFAULT)
            self.config_error = 'Configuration could not be loaded. Original file has not been changed. Review it before saving Settings.'
        self.category = 'All services'
        self.query = ''

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        Gtk.Settings.get_default().set_property('gtk-application-prefer-dark-theme', True)
        css = Gtk.CssProvider()
        css.load_from_path(str(ROOT / 'style.css'))
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window = Gtk.ApplicationWindow(application=self, title='Opacity Command')
        self.window.set_default_size(1180, 820)
        self.window.set_icon_name('opacity-command-cyan')
        header = Gtk.HeaderBar()
        header.set_title_widget(label('OPACITY / COMMAND', 'eyebrow'))
        self.header_error = label('', 'title-error')
        self.header_error.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.header_error.set_lines(2)
        self.header_error.set_ellipsize(Pango.EllipsizeMode.END)
        self.header_error.set_width_chars(18)
        self.header_error.set_max_width_chars(54)
        self.header_error.set_tooltip_text(None)
        self.header_error.set_visible(False)
        header.pack_start(self.header_error)
        header.pack_end(button('Settings', self.settings))
        self.window.set_titlebar(header)
        shell = box(False, 0)
        self.window.set_child(shell)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                         vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        side_scroll.add_css_class('sidebar-scroll')
        side_scroll.set_size_request(220, -1)
        side = box(spacing=6, css='sidebar')
        side.set_vexpand(True)
        side_scroll.set_child(side)
        logo = Gtk.Image.new_from_file(str(ROOT / 'opacity-command.svg'))
        logo.set_pixel_size(100)
        logo.set_halign(Gtk.Align.CENTER)
        logo.set_tooltip_text('Opacity Tech')
        side.append(logo)
        side.append(sidebar_label('OPACITY\nTECH', 'brand'))
        side.append(sidebar_label('HOSTING DASHBOARD', 'eyebrow'))
        side.append(Gtk.Separator())
        self.nav = []
        for category in ['All services', 'Infrastructure', 'Domains & DNS', 'Workspace']:
            b = button(category, lambda c=category: self.filter_category(c), 'nav')
            self.nav.append((category, b))
            side.append(b)
        side.append(Gtk.Separator())
        side.append(sidebar_label('PROXMOX METRICS', 'eyebrow'))
        self.metric_widgets = {}
        for server in METRIC_SERVERS:
            panel = box(spacing=3, css='metric-server')
            title = sidebar_label(server['label'] + '  /  ' + server['ip'], 'metric-server-title')
            panel.append(title)
            gauges = {}
            for key, caption in [('temperature', 'TEMP'), ('cpu', 'CPU'), ('memory', 'MEM')]:
                panel.append(sidebar_label(caption, 'metric-name'))
                gauge = Gtk.ProgressBar(fraction=0)
                gauge.add_css_class('metric-bar')
                gauge.set_accessible_role(Gtk.AccessibleRole.METER)
                panel.append(gauge)
                value = sidebar_label('—', 'metric-value')
                panel.append(value)
                gauges[key] = (gauge, value)
            self.metric_widgets[server['key']] = {'panel': panel, 'title': title, 'gauges': gauges}
            side.append(panel)
        self.metrics_state = sidebar_label('Loading • every 30s', 'metric-state')
        side.append(self.metrics_state)
        stretch = box()
        stretch.set_vexpand(True)
        side.append(stretch)
        side.append(button('↗  opacitytech.com', lambda: self.open_url('https://opacitytech.com')))
        side.append(sidebar_label('LOCAL DESKTOP\nNative GTK 4', 'muted'))
        shell.append(side_scroll)
        main = box(css='main')
        main.set_hexpand(True)
        shell.append(main)
        main.append(label('OPERATIONS / CONTROL CENTER', 'eyebrow'))
        uptime = box(spacing=6, css='uptime-panel')
        self.health_labels = {}
        for key, title in [('proxmox', 'Proxmox'), ('website', 'opacitytech.com'), ('cloudflare', 'Cloudflare DNS / Proxy')]:
            row = box(False, 12)
            title_label = label(title, 'uptime-name')
            title_label.set_hexpand(True)
            row.append(title_label)
            state = label('ldng', 'uptime-state')
            state.add_css_class('health-loading')
            row.append(state)
            self.health_labels[key] = state
            uptime.append(row)
        main.append(uptime)
        main.append(label('Opacity Technologies Central Management', 'muted'))
        host = box(False, css='host-strip')
        hostinfo = box(spacing=4)
        hostinfo.set_hexpand(True)
        hostinfo.append(label('PRIMARY HOST', 'eyebrow'))
        hostinfo.append(label('216.226.81.232', 'host-ip'))
        hostinfo.append(label('Proxmox :8006   /   Hestia :8083   /   SSH :22', 'muted'))
        host.append(hostinfo)
        host.append(button('>_  SSH terminal', self.ssh, 'accent'))
        main.append(host)
        self.search = Gtk.SearchEntry(placeholder_text='Search services…  Ctrl+K')
        self.search.connect('search-changed', self.search_changed)
        main.append(self.search)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=12, row_spacing=12, min_children_per_line=1, max_children_per_line=3)
        self.flow.set_valign(Gtk.Align.START)
        scroll.set_child(self.flow)
        main.append(scroll)
        self.status = label('Ready • Open a service or connect its API to load live resources.', 'status')
        main.append(self.status)
        for name, callback, accel in [('search', lambda: self.search.grab_focus(), '<Control>k'), ('settings', self.settings, '<Control>comma'), ('quit', self.quit, '<Control>q')]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda a, p, fn=callback: fn())
            self.add_action(action)
            self.set_accels_for_action('app.' + name, [accel])
        self.filter_category('All services')
        self.window.present()
        if self.config_error:
            self.show_error(self.config_error, self.status)
        if self.smoke:
            GLib.timeout_add(600, self.smoke_check)
        else:
            self.poll_health()
            self.health_timer = GLib.timeout_add_seconds(HEALTH_POLL_SECONDS, self.poll_health)
            self.poll_metrics()
            self.metrics_timer = GLib.timeout_add_seconds(METRICS_POLL_SECONDS, self.poll_metrics)

    def show_error(self, message, status=None):
        """Show an actionable error in the main title bar and its local status."""
        message = ' '.join(str(message).split())
        if status is not None:
            status.set_text(message)
        if not self.header_error:
            return
        self.header_error.set_text('ERROR / ' + message)
        self.header_error.set_tooltip_text(message)
        self.header_error.set_visible(True)

    def clear_error(self):
        if not self.header_error:
            return
        self.header_error.set_visible(False)
        self.header_error.set_text('')
        self.header_error.set_tooltip_text(None)

    def stop_polling(self, *_):
        self.health_stopped = True
        if self.health_timer is not None:
            GLib.source_remove(self.health_timer)
            self.health_timer = None
        if self.metrics_timer is not None:
            GLib.source_remove(self.metrics_timer)
            self.metrics_timer = None

    def poll_health(self):
        if self.health_stopped:
            return False
        if self.health_pending:
            return True
        endpoints = targets(copy.deepcopy(self.config))
        self.health_pending = len(endpoints)
        for key, (url, ca) in endpoints.items():
            def worker(k=key, endpoint=url, certificate=ca):
                up, detail = check_target(k, endpoint, certificate)
                GLib.idle_add(self.health_result, k, up, detail, endpoint)
            threading.Thread(target=worker, daemon=True).start()
        return True

    def health_result(self, key, up, detail, endpoint):
        self.health_pending = max(0, self.health_pending - 1)
        if self.health_stopped:
            return False
        state = self.health_labels[key]
        state.set_text('Unknown' if up is None else ('Up' if up else 'Down'))
        for css in ('health-loading', 'health-up', 'health-down'):
            state.remove_css_class(css)
        state.add_css_class('health-loading' if up is None else ('health-up' if up else 'health-down'))
        checked = GLib.DateTime.new_now_local().format('%H:%M:%S')
        scope = ('Cloudflare-reported DNS and CDN/proxy service health; independent of your domains.'
                 if key == 'cloudflare' else 'HTTPS availability from this desktop; not a global outage report.')
        if key == 'proxmox':
            scope = 'TCP reachability of the Proxmox port from this desktop.'
        state.set_tooltip_text(f'{endpoint}\n{detail}\nChecked {checked} • Refreshes every 5 minutes.\n{scope}')
        return False

    def poll_metrics(self):
        if self.health_stopped:
            return False
        if self.metrics_pending:
            return True
        self.metrics_pending = True
        config = copy.deepcopy(self.config['providers']['proxmox'])
        def worker():
            try:
                secret = secret_get('proxmox', config)
                snapshots = collect_metrics(config, secret)
                GLib.idle_add(self.metrics_result, snapshots)
            except (GLib.Error, OSError, ValueError) as error:
                GLib.idle_add(self.metrics_failed, str(error))
        threading.Thread(target=worker, daemon=True).start()
        return True

    def metrics_failed(self, detail):
        self.metrics_pending = False
        if self.health_stopped:
            return False
        self.metrics_state.set_text('Metrics unavailable • retrying')
        self.metrics_state.set_tooltip_text(detail or 'Metrics could not be loaded.')
        return False

    def metrics_result(self, snapshots):
        self.metrics_pending = False
        if self.health_stopped:
            return False
        available = 0
        for key, widgets in self.metric_widgets.items():
            snapshot = snapshots.get(key, {})
            server = next(item for item in METRIC_SERVERS if item['key'] == key)
            node = snapshot.get('node')
            widgets['title'].set_text(server['label'] + (f'  /  {node}' if node else '  /  ' + server['ip']))
            for metric, (gauge, value) in widgets['gauges'].items():
                reading = snapshot.get(metric)
                for css_class in ('metric-active', 'metric-warning'):
                    gauge.remove_css_class(css_class)
                if reading is None:
                    gauge.set_fraction(0)
                    value.set_text('—')
                    continue
                available += 1
                fraction = min(1.0, max(0.0, float(reading) / 100 if metric == 'temperature' else float(reading)))
                gauge.set_fraction(fraction)
                value.set_text(f'{float(reading):.1f}°C' if metric == 'temperature' else f'{float(reading) * 100:.0f}%')
                gauge.add_css_class('metric-warning' if fraction >= (.80 if metric == 'temperature' else .90) else 'metric-active')
            details = []
            if snapshot.get('api_error'):
                details.append(snapshot['api_error'])
            if snapshot.get('temperature') is None:
                details.append('Temperature needs the second-hop root SSH key and lm-sensors.')
            details.append('Refreshes every 30 seconds while Opacity Command is running.')
            widgets['panel'].set_tooltip_text('\n'.join(details))
        checked = GLib.DateTime.new_now_local().format('%H:%M:%S')
        self.metrics_state.set_text(('Updated ' if available else 'Unavailable ') + checked + ' • 30s')
        self.metrics_state.set_tooltip_text('CPU and memory use the Proxmox API with SSH fallback. Temperature uses the sensor API when available, then SSH.')
        return False

    def smoke_check(self):
        self.show_error('A short smoke-test error')
        assert self.header_error.get_visible()
        assert self.header_error.get_lines() == 2
        self.clear_error()
        assert not self.header_error.get_visible()
        assert all(w.get_text() == 'ldng' for w in self.health_labels.values())
        self.health_result('proxmox', True, 'HTTP 200', 'https://example.com')
        self.health_result('website', False, 'timeout', 'https://example.com')
        assert self.health_labels['proxmox'].get_text() == 'Up'
        assert self.health_labels['website'].get_text() == 'Down'
        self.metrics_result({
            'he': {'node': 'helium', 'temperature': 51.5, 'cpu': .24, 'memory': .63, 'api_error': None},
            'li': {'node': 'lithium', 'temperature': None, 'cpu': .10, 'memory': .45, 'api_error': None},
        })
        assert self.metric_widgets['he']['gauges']['cpu'][1].get_text() == '24%'
        assert self.metric_widgets['li']['gauges']['temperature'][1].get_text() == '—'
        self.filter_category('Workspace')
        assert self.flow.get_first_child() is not None
        self.query = 'no-such-service'
        self.cards()
        self.query = ''
        self.filter_category('All services')
        self.settings()
        fixtures = {
            'proxmox': {'name': 'Test VM', 'node': 'pve', 'type': 'qemu', 'vmid': 100, 'status': 'running'},
            'linode': {'label': 'Test Linode', 'id': 1, 'status': 'running', 'ipv4': ['192.0.2.1']},
            'cloudflare': {'name': 'example.com', 'id': 'record', 'type': 'A', 'content': '192.0.2.1'},
            'namecheap': {'Name': 'example.com', 'Expires': '2027-01-01'},
            'hestia': {'name': 'example.com', 'IP': '192.0.2.1'},
        }
        for kind in FIELDS:
            self.manage(kind)
            self.configure(kind)
            sample = fixtures[kind]
            holder = box()
            self.resource_row(kind, sample, None, holder, label('test'), button('test', lambda: None))
            assert holder.get_first_child() is not None
        print('GTK smoke: search, settings, five management panels, credential forms and resource rows passed', flush=True)
        GLib.timeout_add(800, lambda: (self.quit(), False)[1])
        return False

    def search_changed(self, entry):
        self.query = entry.get_text().lower().strip()
        self.cards()

    def filter_category(self, category):
        self.category = category
        for name, b in self.nav:
            (b.add_css_class if name == category else b.remove_css_class)('selected')
        self.cards()

    def cards(self):
        clear(self.flow)
        found = 0
        for kind, title, category, desc, _ in SERVICES:
            if self.category != 'All services' and category != self.category:
                continue
            if self.query and self.query not in f'{title} {category} {desc}'.lower():
                continue
            found += 1
            card = box(css='card')
            card.set_size_request(240, 210)
            card.append(label(category.upper(), 'eyebrow'))
            card.append(label(title, 'card-title'))
            card.append(label(desc, 'muted'))
            card.append(label('API + WEB' if kind in FIELDS else 'WEB WORKSPACE', 'badge'))
            actions = box(False, 6)
            actions.set_vexpand(True)
            actions.set_valign(Gtk.Align.END)
            actions.append(button('Open web ↗', lambda k=kind: self.open_url(self.config['urls'][k])))
            if kind in FIELDS:
                actions.append(button('Manage', lambda k=kind: self.manage(k), 'accent'))
            card.append(actions)
            self.flow.append(card)
        if not found:
            self.flow.append(label('No matching services. Try another search.', 'muted'))

    def open_url(self, url):
        try:
            valid_url(url)
            launcher = Gtk.UriLauncher.new(url)
            def done(obj, result):
                try:
                    obj.launch_finish(result)
                    self.clear_error()
                    self.status.set_text('Opened web dashboard in your default browser.')
                except GLib.Error:
                    self.show_error('Could not open the browser. Check your default browser association.', self.status)
            launcher.launch(self.window, None, done)
        except ValueError as e:
            self.show_error(e, self.status)

    def ssh(self):
        try:
            args = ssh_args(self.config['ssh_user'])
            subprocess.Popen(['foot', '--title=Opacity SSH', *args], start_new_session=True)
            self.clear_error()
            self.status.set_text('SSH terminal opened for ' + self.config['ssh_user'] + '@216.226.81.232:22')
        except (ValueError, OSError) as e:
            self.show_error(e, self.status)
            if not self.config['ssh_user']:
                self.settings()

    def dialog(self, title, width=650, height=640):
        win = Gtk.Window(title=title, transient_for=self.window, modal=True, default_width=width, default_height=height)
        self.add_window(win)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = box(css='dialog-content')
        sc.set_child(content)
        win.set_child(sc)
        content.append(label(title, 'card-title'))
        return win, content

    def settings(self):
        win, content = self.dialog('Dashboard settings')
        content.append(label('Service links open with your default browser and its existing login sessions.', 'muted'))
        entries = {}
        for key, title, value in [('ssh_user', 'SSH username', self.config['ssh_user'])] + [(k, title + ' URL', self.config['urls'][k]) for k, title, *_ in SERVICES]:
            content.append(label(title, 'eyebrow'))
            e = Gtk.Entry(text=value, hexpand=True)
            entries[key] = e
            content.append(e)
        error = label('', 'error')
        content.append(error)
        def save():
            try:
                c = copy.deepcopy(self.config)
                user = entries['ssh_user'].get_text().strip()
                if user:
                    ssh_args(user)
                c['ssh_user'] = user
                for k, *_ in SERVICES:
                    c['urls'][k] = valid_url(entries[k].get_text().strip())
                save_config(c)
                self.config = c
                self.clear_error()
                self.status.set_text('Settings saved.')
                win.close()
            except (ValueError, OSError) as e:
                self.show_error(e, error)
        content.append(button('Save settings', save, 'accent'))
        win.present()

    def background(self, fn, done, status, controls):
        for w in controls:
            w.set_sensitive(False)
        self.clear_error()
        status.set_text('Working…')
        def worker():
            try:
                value, error = fn(), None
            except Exception as e:
                value = None
                error = str(e) if isinstance(e, (ApiError, ValueError)) else 'Operation failed. Check network, desktop keyring and configuration.'
            def finish():
                for w in controls:
                    w.set_sensitive(True)
                if error:
                    self.show_error(error, status)
                else:
                    self.clear_error()
                    done(value)
                return False
            GLib.idle_add(finish)
        threading.Thread(target=worker, daemon=True).start()

    def configure(self, kind):
        win, content = self.dialog('Connect ' + dict((s[0], s[1]) for s in SERVICES)[kind])
        content.append(label('Credentials stay in your desktop keyring. A blank secret keeps the existing credential for this API origin.', 'muted'))
        entries = {}
        for key, title, _ in FIELDS[kind]:
            content.append(label(title, 'eyebrow'))
            e = Gtk.Entry(text=self.config['providers'][kind].get(key, ''))
            content.append(e)
            entries[key] = e
        content.append(label('Access key:secret key' if kind == 'hestia' else 'API token secret / API key', 'eyebrow'))
        token = Gtk.PasswordEntry(show_peek_icon=True)
        content.append(token)
        helptext = {
            'proxmox': 'Token needs resource audit and VM power permissions. For a private certificate, use a matching DNS hostname and a trusted CA PEM file.',
            'linode': 'Personal access token with Linodes read/write permission.',
            'cloudflare': 'API token scoped to opacitytech.com with Zone DNS Edit. Copy Zone ID from the zone overview.',
            'namecheap': 'Enable production API access and allowlist your public IPv4 in Namecheap. Nameserver changes affect DNS delegation.',
            'hestia': 'Access key permissions: v-list-users (for all users), v-list-web-domains and the requested actions. Leave the hosting username blank to discover all users, or enter one username. Allowlist your public IP in Hestia. Enter ACCESS_KEY:SECRET_KEY.'}
        content.append(label(helptext[kind], 'muted'))
        status = label('', 'status')
        content.append(status)
        def save():
            cfg = {k: e.get_text().strip() for k, e in entries.items()}
            secret = token.get_text().strip()
            def work():
                if 'base' in cfg:
                    cfg['base'] = valid_url(cfg['base'], origin=True)
                if secret:
                    secret_set(kind, cfg, secret)
                c = copy.deepcopy(self.config)
                c['providers'][kind] = cfg
                save_config(c)
                return c
            def done(c):
                self.config = c
                token.set_text('')
                status.set_text('Saved. Close this window and click Load resources to test the connection.')
            self.background(work, done, status, [savebutton, forget])
        savebutton = button('Save connection', save, 'accent')
        content.append(savebutton)
        def remove():
            cfg = copy.deepcopy(self.config['providers'][kind])
            self.background(lambda: Secret.password_clear_sync(SCHEMA, secret_attrs(kind, cfg), None), lambda _: status.set_text('Saved credential removed for the configured origin.'), status, [savebutton, forget])
        forget = button('Forget saved credential', remove)
        content.append(forget)
        content.append(button('Close', win.close))
        win.present()

    def manage(self, kind):
        title = next(s[1] for s in SERVICES if s[0] == kind)
        win, content = self.dialog(title + ' / native controls', 850, 680)
        content.append(label('Load live resources, select a target, then choose an action.', 'muted'))
        controls = box(False)
        configure = button('Configure API', lambda: self.configure(kind))
        controls.append(configure)
        content.append(controls)
        status = label('Not connected • Configure an API credential, then load resources.', 'status')
        content.append(status)
        rowsbox = box(spacing=8)
        content.append(rowsbox)
        def load():
            cfg = copy.deepcopy(self.config['providers'][kind])
            clear(rowsbox)
            def work():
                provider = Provider(kind, cfg, secret_get(kind, cfg))
                return provider, provider.list_resources()
            def done(result):
                provider, rows = result
                status.set_text(f'{len(rows)} resources loaded • Snapshot at ' + GLib.DateTime.new_now_local().format('%H:%M:%S'))
                for row in rows:
                    self.resource_row(kind, row, provider, rowsbox, status, loadbutton)
                if not rows:
                    rowsbox.append(label('No resources returned. Check account scope and permissions.', 'muted'))
            self.background(work, done, status, [loadbutton, configure])
        loadbutton = button('Load resources', load, 'accent')
        controls.append(loadbutton)
        win.present()

    def resource_row(self, kind, row, provider, parent, status, loadbutton):
        name = str(row.get('label') or row.get('name') or row.get('Name') or row.get('vmid') or row.get('id'))
        details = {
            'proxmox': f"{row.get('type', '')} {row.get('vmid', '')} · {row.get('node', '')} · {row.get('status', '')}",
            'linode': f"{row.get('region', '')} · {row.get('status', '')} · {', '.join(row.get('ipv4', []))}",
            'cloudflare': f"{row.get('type', '')} · {row.get('content', '')} · TTL {row.get('ttl', '')}",
            'namecheap': f"Expires {row.get('Expires', '')} · Auto-renew {row.get('AutoRenew', '')}",
            'hestia': f"User {row.get('_hestia_user', self.config['providers']['hestia'].get('user', ''))} · IP {row.get('IP', '')} · SSL {row.get('SSL', '')} · Suspended {row.get('SUSPENDED', '')}"}
        panel = box(css='resource')
        panel.append(label(name, 'resource-title'))
        panel.append(label(details[kind], 'muted'))
        actions = box(False, 6)
        panel.append(actions)
        parent.append(panel)
        options = {'proxmox': [('Start', 'start'), ('Shut down', 'shutdown'), ('Reboot', 'reboot'), ('Suspend', 'suspend'), ('Resume', 'resume')], 'linode': [('Boot', 'boot'), ('Shut down', 'shutdown'), ('Reboot', 'reboot')], 'cloudflare': [('Edit content', 'content')], 'namecheap': [('Set nameservers', 'nameservers')], 'hestia': [('Suspend', 'suspend'), ('Unsuspend', 'unsuspend'), ('Restart web', 'restart_web'), ('Restart proxy', 'restart_proxy'), ('Restart DNS', 'restart_dns'), ('Restart mail', 'restart_mail'), ('Restart FTP', 'restart_ftp'), ('Edit domain', 'edit'), ('Rebuild website', 'rebuild')]}
        for text, action in options[kind]:
            # Structured DNS record types need a dedicated editor, available in the web dashboard.
            if kind == 'cloudflare' and row.get('type') not in ('A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'PTR'):
                continue
            actions.append(button(text, lambda a=action, t=text: self.confirm(provider, row, name, a, t, parent, status, loadbutton)))

    def confirm(self, provider, row, name, action, title, parent, status, loadbutton):
        win, content = self.dialog(title, 600, 360)
        content.append(label('Target: ' + name, 'resource-title'))
        value = None
        if action in ('content', 'nameservers', 'edit'):
            prompt = 'DNS content' if action == 'content' else 'Nameservers (comma-separated)' if action == 'nameservers' else 'New domain name'
            content.append(label(prompt, 'eyebrow'))
            value = Gtk.Entry(text=str(row.get('content', '')) if action == 'content' else str(row.get('name', '')) if action == 'edit' else '')
            content.append(value)
        explanations = {'content': 'This changes this record’s content. Other record fields are preserved.', 'nameservers': 'This replaces domain nameservers and can interrupt website and mail routing. Existing Namecheap forwarding will no longer apply.', 'suspend': 'This suspends the selected website and redirects visitors to Hestia’s suspension page.', 'unsuspend': 'This restores the selected website after suspension.', 'restart_web': 'This reloads Hestia’s web service for all hosted websites.', 'restart_proxy': 'This reloads Hestia’s proxy service.', 'restart_dns': 'This reloads Hestia’s DNS service.', 'restart_mail': 'This reloads Hestia’s mail service.', 'restart_ftp': 'This reloads Hestia’s FTP service.', 'edit': 'This changes the selected website’s domain name and reloads its web configuration.', 'rebuild': 'This regenerates the website configuration and reloads its web service.'}
        content.append(label(explanations.get(action, 'This sends a power command to the selected server. Shutdown or reboot interrupts hosted services.'), 'muted'))
        content.append(label('Review the target and change before applying.', 'muted'))
        buttons = box(False)
        buttons.append(button('Cancel', win.close))
        def apply():
            v = value.get_text() if value else None
            win.close()
            def done(result):
                clear(parent)
                message = 'Request accepted. Reload resources to verify the resulting state.'
                if provider.kind == 'proxmox' and isinstance(result, str):
                    message += '\nTask: ' + result
                status.set_text(message)
            self.background(lambda: provider.act(row, action, v), done, status, [parent, loadbutton])
        buttons.append(button('Apply ' + title.lower(), apply, 'accent'))
        content.append(buttons)
        win.present()

if __name__ == '__main__':
    smoke = '--smoke-test' in sys.argv
    raise SystemExit(App(smoke=smoke).run([sys.argv[0]]))
