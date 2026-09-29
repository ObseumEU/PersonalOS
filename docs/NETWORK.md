# Home / office network and VPN

How David's devices reach the home services (PersonalOS, knowlage, nexus, grafana,
Home Assistant, ...) at home and away, why it used to be flaky, and how it works now.
Config lives in `deploy/network/`. Last reviewed 2026-09-29.

## Topology

```
                         Internet
                            |
   phone (4G/5G, away)      |        Hetzner VPS 23.88.52.230  ("Trading" in ~/.ssh/config)
   WireGuard 10.66.66.5 ----+------> wg0 10.66.66.1/24, UDP 24454  = the VPN HUB
                            |          - dnsmasq on 10.66.66.1:53 (DNS for VPN clients)
                            |          - DNAT tcp 80/443 (+25565, 8100, 43117) -> 10.66.66.4
                            |          - route 192.168.1.0/24 -> peer svr03
                            |                ^
                            |                | WireGuard, dialled OUT from home (keepalive 25 s)
   Starlink (IPv4 CGNAT, public 150.228.35.73 shared/changing; IPv6 2a0d:3344:781b:4e08::/64)
                            |
             Starlink router 192.168.1.1  (DHCP + DNS; no custom-DNS setting)
                            |
   LAN 192.168.1.0/24 ------+-------------------------------------------------
     .108 svr03   Caddy for *.obseum.cz / *.obseum.cloud, wireguard-client (wg0 10.66.66.4),
                  lan-dns (dnsmasq, split horizon)  <- new
     .186 AGENT   observability, minecraft
     .56  Home Assistant (homeassistant.local, :8123)
     .211 David's PC
```

**The VPN** is plain WireGuard in a hub-and-spoke: the hub is the Hetzner VPS, svr03 is a
spoke that dials out (`/opt/server/tunnel`, linuxserver/wireguard, host network) and
announces the LAN (`AllowedIPs = 10.66.66.4/32, 192.168.1.0/24` on the hub). Phones and
laptops are spokes too (`/opt/server/tunnel/clients/*.conf` on svr03). It exists because
Starlink puts IPv4 behind CGNAT: nothing at home can be reached from outside directly, so
both the public websites (VPS DNAT -> tunnel -> svr03 Caddy) and the VPN go through the VPS.

**Caddy access rule** on every internal site: `10.66.66.1` -> 403 (that is what *all*
public traffic looks like after the VPS masquerades it), `192.168.0.0/16`, `10.66.66.0/24`,
docker ranges -> allowed, everything else 403.

**192.168.68.x** is dead: the old TP-Link Deco mesh LAN (Deco default 192.168.68.0/22)
before the Starlink router. Nothing answers there any more. Leftovers: svr03 still carries
`192.168.68.169/32` on enp1s0 (harmless), ~/.ssh/config entries svr01/svr02/svr04/printer,
the PC's `host.docker.internal 192.168.68.70` (written by Docker Desktop, it rewrites it
itself), the old HA target 192.168.68.64.

## Why it was flaky

1. **Public DNS always points to the VPS.** At home without the VPN,
   `personalos.obseum.cz` -> 23.88.52.230 -> hairpin Starlink -> Hetzner -> tunnel -> svr03,
   and Caddy sees 10.66.66.1 -> **403** on every internal site. That is why HA "needed the
   VPN at home". (`curl --resolve personalos.obseum.cz:443:23.88.52.230` from the PC -> 403.)
2. **The VPN's DNS knew only 3 names.** The hub dnsmasq rewrote nexus, nexus-api and
   langfuse to 192.168.1.108; personalos, knowlage, grafana, litellm, logi still resolved to
   the VPS, left the split tunnel (23.88.52.230 is not in AllowedIPs) and got 403. So a
   different subset broke away from home.
3. **smart.obseum.cloud has no public DNS record at all**, so HA by name only worked where
   a hosts file said so; its Caddy cert is `tls internal` (untrusted) because ACME could not
   validate a name that does not exist.
4. **The router cannot hand out a custom DNS** (Starlink), so LAN split DNS could not be
   done centrally; the PC got a hosts-file band-aid instead.
5. **IPv6 DNS**: the router also advertises `fd72:b840:a00e:8::1` as a resolver and Windows
   asks it first, so an IPv4-only DNS change on a client is not enough.
6. **MTU**: the phone used the default 1420; some mobile networks (IPv6/464XLAT) need less,
   which shows up as pages that start loading and then hang. Now 1280.
7. The phone's VPN was not always on (last hub handshake a day before this review).

Not problems: hairpin NAT on the router (never used; the hairpin is via the VPS), IPv6
leaks of the internal sites (no AAAA records exist), the tunnel itself (healthy).

## Design

**Split-horizon DNS** - one list of internal names, `deploy/network/dns/obseum-internal.conf`,
served by two resolvers:

| Where the client is | Resolver | Internal names resolve to |
|---|---|---|
| on the VPN (anywhere, incl. at home) | hub dnsmasq 10.66.66.1 | 192.168.1.108 via the tunnel |
| on the LAN, device pointed at svr03 | `lan-dns` on svr03 192.168.1.108 / fd72:b840:a00e:8:da9e:f3ff:fe2e:18e (fallback: router) | 192.168.1.108 directly |
| outside, no VPN | public (Namecheap) | 23.88.52.230 -> 403, as intended |

Only the LAN-only sites are in the list; public sites keep their public answer.

**VPN: keep the existing WireGuard hub**, do not add Tailscale. The hub already solves
CGNAT, has 200+ days uptime and needs no third-party account; the broken part was DNS.
The phone runs a **split tunnel** (`AllowedIPs = 10.66.66.0/24, 192.168.1.0/24`,
`DNS = 10.66.66.1`, `MTU = 1280`, keepalive 25) **always on, at home too**: at home the
LAN traffic takes the tunnel (via Hetzner, ~50-70 ms) but everything behaves identically in
both places, and the rest of the internet goes direct. Home Assistant is reachable as
`http://192.168.1.56:8123` from anywhere, which is the most robust URL for the HA app.

Trade-off: with the VPN always on, the phone's internal access depends on the VPS. If the
VPS is down, the public sites are down anyway; switch the tunnel off to get plain internet
DNS back.

## What is deployed

- svr03 `/opt/server/network-dns`: `lan-dns` container (alpine + dnsmasq, host network,
  binds only 192.168.1.108 and svr03's ULA). Rollback: `docker compose down` there.
- VPN hub `/etc/dnsmasq.d/nexus-vpn.conf` + `obseum-internal.conf`. Backup of the previous
  state: `/root/network-backup-20260929162428/` on the VPS. Rollback: copy it back,
  `systemctl restart dnsmasq`.
- Phone config `phone-home.conf` / `phone-home.png` (same keys as `mobile_ha`, new
  AllowedIPs/MTU) in `/opt/server/tunnel/clients/` on svr03. Private keys never leave
  svr03 except into the QR image.
- `deploy/network/deploy.sh` re-pushes the DNS config to both resolvers.
- `deploy/network/windows/use-home-dns.ps1` points a Windows PC at `lan-dns` (IPv4 + IPv6,
  router as fallback) and removes the obseum lines from the hosts file (with backup,
  `-Rollback` undoes both).

## Setup steps for devices

**Android phone**
1. WireGuard app -> + -> Scan from QR code -> `phone-home.png` (replace the old
   `mobile_ha` tunnel; same key, so only one of the two may be active).
2. Android Settings -> Network & internet -> VPN -> gear next to WireGuard ->
   **Always-on VPN: on**, **Block connections without VPN: off**.
3. Settings -> Private DNS -> **Automatic** or **Off** (a custom "strict" Private DNS
   overrides the VPN's DNS and brings back the public answers).
4. HA companion app: server URL `http://192.168.1.56:8123` (works at home and away).

**Windows PC**: `powershell -ExecutionPolicy Bypass -File deploy\network\windows\use-home-dns.ps1`
in an elevated terminal.

**Other home devices** (laptops, family phones): either the WireGuard client (new peer on
the hub + a client file like `phone-home.conf`), or set DNS manually to 192.168.1.108 with
192.168.1.1 as secondary. A LAN-wide fix without per-device setup needs a router that can
set the DHCP DNS (Starlink in bypass mode + own router), not done.

## Open items

- `smart.obseum.cloud`: add a public A record `smart -> 23.88.52.230` at Namecheap, then
  drop `tls internal` from its Caddy block so it gets a Let's Encrypt cert (still LAN/VPN
  only via the 10.66.66.1 rule).
- `eplus.obseum.cz /bridge/*` allows `10.66.66.0/24`, which includes 10.66.66.1 = every
  public client; the `85.10.192.243` entry can never match behind the VPS masquerade. Review.
