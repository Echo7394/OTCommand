# Opacity Command

A small native GTK 4 dashboard for Opacity Tech, built for this Omarchy desktop.

Launch **Opacity Command** from the application launcher, or run:

```sh
~/.local/bin/opacity-command
```

Python, PyGObject, GTK 4, libsecret and Foot were already installed. No Electron,
web server, background daemon or package downloads are required. The launcher
selects GTK's Cairo renderer for this machine's older Intel GPU. Web dashboards
use your default browser and existing browser sessions. The native app makes
authenticated API calls only when requested; resource views are timestamped snapshots.
The headline checks Proxmox with `nc -vz 216.226.81.232 8006`, checks
opacitytech.com over HTTPS, and reads
Cloudflare's official status feed on startup and every five minutes. Background
checks show `ldng` initially, then `Up` or `Down`. Hover for details and check time.
Proxmox is Up when nc exits successfully, Down on connection failure or a
ten-second timeout, and Unknown if nc cannot run. The website check accepts
login responses (401/403) as reachable; certificate, network and other HTTP
errors count as Down.

The sidebar shows temperature, CPU, and memory gauges for the HE and LI Proxmox
nodes. CPU and memory prefer the authenticated Proxmox node-status API and can
fall back to the SSH helpers.
Temperature first tries the optional hardware-sensor API and falls back to the
matching `~/proxmox_he_ssh.sh` or `~/proxmox_li_ssh.sh` helper. Gauges refresh
at startup and every 30 seconds while the app runs. The SSH fallback is
non-interactive and requires key authentication from `otadmin@216.226.81.232`
to each internal root account; temperature also requires `lm-sensors` on the
nodes. A missing API token, SSH key, or sensor command leaves only the
unavailable reading blank and does not block the interface.

**Cloudflare DNS / Proxy** monitors the Authoritative DNS, Recursive DNS and
CDN/Cache components at https://www.cloudflarestatus.com/api/v2/summary.json.
All three must report operational for Up. Degradation, outages and maintenance
count as Down; unavailable or incomplete status data shows Unknown. This uses
Cloudflare's own reported health, may lag incidents, and does not depend on
preview.opacitytech.com or any other customer hostname. DNS record-editing
availability (DNS Updates) and unrelated products do not affect this indicator.


## Included controls

| Service | Native controls | Web access |
| --- | --- | --- |
| Proxmox | List VMs/containers; start, graceful shutdown, reboot | Configured IP on port 8006 |
| Linode | List instances; boot, shutdown, reboot | Your Linode instances page |
| HestiaCP | List one hosting user's websites; rebuild website config and reload web service | Configured IP on port 8083 |
| Cloudflare | List zone DNS records; edit content for A, AAAA, CNAME, TXT, MX, NS, PTR | Your supplied account page |
| Namecheap | List domains/expiry/auto-renew; replace custom nameservers | Your supplied account page |
| Proton Mail / Meet / Drive | — | Their signed-in web apps |
| SSH | Foot terminal running SSH as otadmin to 216.226.81.232:22 | — |

Other service operations remain accessible through **Open web**. Structured DNS
record types such as SRV use the Cloudflare web editor. DNS editing uses PATCH
so unrelated properties (TTL, proxy, priority) remain unchanged. This version
does not implement provisioning, deletion, billing, Proton APIs, or automatic
refresh of authenticated resource lists. Power commands report acceptance; reload to check status. Proxmox task
IDs are shown for follow-up in its web task log.

## Connect your accounts

Choose **Manage → Configure API** on each card. Paste credentials there, not in
chat. A blank credential preserves the saved secret for that provider/origin.
The **Forget saved credential** button removes it from the keyring.

- **Proxmox:** API token ID (`user@realm!token`), secret, and API origin. Grant
  audit rights for intended resources and `VM.PowerMgmt` for power commands;
  effective user and token permissions must allow these operations.
- **Linode:** a personal access token with Linodes read/write access.
- **Cloudflare:** a token scoped to opacitytech.com with Zone DNS Edit, plus its
  **Zone ID**. The ID in your supplied account URL is an account ID, not a Zone ID.
- **Namecheap:** API username, API key, and your allowlisted public IPv4 address.
  Enable production API access under Profile → Tools → Business & Dev Tools.
- **Hestia:** hosting username and `ACCESS_KEY:SECRET_KEY`. Allow
  `v-list-web-domains` and `v-rebuild-web-domain`, and allowlist this desktop's
  public IP in Hestia's API settings. The SSH username is configured separately.

Then click **Load resources** to verify the connection. All credentials use the
system Secret Service keyring. If it is unavailable or locked, the app reports
an error rather than writing secrets to its configuration file. API secrets are
bound to the configured origin, so changing an origin requires saving a
credential for that origin. Old-origin credentials can be removed by restoring
that origin and choosing Forget, or using the system keyring manager.

HTTPS certificate validation is always enabled. If your panels use private
certificates, configure their matching DNS hostname and a trusted CA PEM file.
A browser certificate exception does not configure API trust. No certificate
checks or SSH host-key checks are bypassed. SSH uses your existing agent, keys,
config and known_hosts; passwords, if needed, are prompted by SSH in Foot.

Every native write action has an explicit target/change review in the GUI.
Changing nameservers can interrupt website/mail routing and disables Namecheap
forwarding that depends on its DNS. Requests are never automatically retried.
If a request times out, check current state before sending another command.

## Settings and files

- `Ctrl+K`: search services
- `Ctrl+,`: settings (service URLs and SSH username)
- `Ctrl+Q`: quit
- Source: `/home/cspires/Projects/opacity-command`
- Configuration: `~/.config/opacity-command/config.json` (mode 0600, no API secrets)
- Launcher: `~/.local/bin/opacity-command`
- Desktop entry: `~/.local/share/applications/com.opacitytech.Command.desktop`
- Icon: `~/.local/share/icons/hicolor/scalable/apps/opacity-command.svg`

## Validation

```sh
cd ~/Projects/opacity-command
python3 -m unittest -v
./opacity-command --smoke-test
```

Adapter tests use simulated responses to check authentication, pagination,
request payloads, XML parsing, errors, redirect refusal, URL validation, config,
metric parsing and SSH argument handling. The GTK smoke test opens windows briefly
without network calls or credentials. Live account authentication and production
writes have **not** been tested; account credentials are still required.

## Provider references

- [GTK Application](https://docs.gtk.org/gtk4/class.Application.html)
- [Proxmox API](https://pve.proxmox.com/wiki/Proxmox_VE_API)
- [Linode reboot API](https://techdocs.akamai.com/linode-api/reference/post-reboot-linode-instance)
- [Cloudflare DNS PATCH](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/edit/)
- [Namecheap API setup](https://www.namecheap.com/support/api/intro/)
- [Namecheap nameservers](https://www.namecheap.com/support/api/methods/domains-dns/set-custom/)
- [Namecheap official SDK / POST transport](https://github.com/namecheap/go-namecheap-sdk)
- [Hestia access-key API](https://hestiacp.com/docs/server-administration/rest-api)
- [Proton Meet web app](https://proton.me/support/meet-web-app)
