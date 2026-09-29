# Point this PC at the home split-horizon resolver (svr03 dnsmasq) and drop the
# PersonalOS hosts-file band-aid. Run elevated:
#   powershell -ExecutionPolicy Bypass -File deploy\network\windows\use-home-dns.ps1
# Rollback (DNS back to DHCP/router, hosts file restored from the newest backup):
#   powershell -ExecutionPolicy Bypass -File deploy\network\windows\use-home-dns.ps1 -Rollback
#
# Uses netsh (the NetTCPIP CIM cmdlets are broken on this PC: "Class not registered").
param(
  [string]$Adapter = 'Ethernet 3',
  [switch]$Rollback
)
$ErrorActionPreference = 'Stop'
$hosts = "$env:WINDIR\System32\drivers\etc\hosts"
$svr03v4 = '192.168.1.108'
$svr03v6 = 'fd72:b840:a00e:8:da9e:f3ff:fe2e:18e'
$routerV4 = '192.168.1.1'
$routerV6 = 'fd72:b840:a00e:8::1'

if ($Rollback) {
  netsh interface ipv4 set dnsservers name="$Adapter" source=dhcp | Out-Null
  netsh interface ipv6 set dnsservers name="$Adapter" source=dhcp | Out-Null
  $bak = Get-ChildItem "$hosts.bak-*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
  if ($bak) { Copy-Item $bak.FullName $hosts -Force; "hosts restored from $($bak.Name)" }
  ipconfig /flushdns | Out-Null
  'DNS back to DHCP.'
  return
}

# 1. DNS: svr03 first, the router as fallback (the LAN keeps working if the container is down).
netsh interface ipv4 set dnsservers name="$Adapter" source=static address=$svr03v4 register=primary validate=no | Out-Null
netsh interface ipv4 add dnsservers name="$Adapter" address=$routerV4 index=2 validate=no | Out-Null
# IPv6 too, otherwise Windows keeps asking the router's IPv6 resolver first and gets the public answer.
netsh interface ipv6 set dnsservers name="$Adapter" source=static address=$svr03v6 register=primary validate=no | Out-Null
netsh interface ipv6 add dnsservers name="$Adapter" address=$routerV6 index=2 validate=no | Out-Null

# 2. hosts: back up, then drop only the obseum.cz / obseum.cloud lines.
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Copy-Item $hosts "$hosts.bak-$stamp"
$kept = Get-Content $hosts | Where-Object { $_ -notmatch '^\s*\d+\.\d+\.\d+\.\d+\s+\S+\.obseum\.(cz|cloud)\b' }
Set-Content -Path $hosts -Value $kept -Encoding ascii
ipconfig /flushdns | Out-Null

# 3. Check.
foreach ($n in 'personalos.obseum.cz', 'knowlage.obseum.cz', 'grafana.obseum.cloud', 'smart.obseum.cloud') {
  $ip = (Resolve-DnsName $n -Type A -ErrorAction SilentlyContinue | Where-Object Type -eq 'A' | Select-Object -First 1).IPAddress
  "{0,-24} -> {1}" -f $n, $ip
}
"hosts backup: $hosts.bak-$stamp"
