# NSP Edge Business Runtime

## Windows: start Docker + Zeroconf together

On Windows / Docker Desktop, Zeroconf must run **natively on the Windows host** so mDNS can bind the real LAN interface. Odoo and PostgreSQL continue to run in Docker.

### Start everything

From PowerShell:

```powershell
.\start-nsp.ps1
```

or double-click / run:

```cmd
start-nsp.cmd
```

To rebuild the Docker image while starting:

```powershell
.\start-nsp.ps1 -Build
```

The launcher starts both components in the same operation:

```text
start-nsp.ps1
├─ docker compose up -d
│  ├─ web  :8069
│  └─ db
└─ native Windows Zeroconf
   ├─ mDNS _nsp._tcp.local. on the LAN adapter
   ├─ discovery HTTP :9000
   └─ Core API target 127.0.0.1:8069
```

Zeroconf starts immediately as a Windows process, waits for the Docker-published Core API port `127.0.0.1:8069`, then advertises `_nsp._tcp.local.`. If Odoo is still unavailable after 120 seconds, Zeroconf logs a warning and starts advertising anyway.

The first run creates a dedicated Python environment under `zeroconf/.venv` and installs `zeroconf/requirements.txt` automatically.

### Verify

```powershell
docker compose ps
Invoke-RestMethod http://127.0.0.1:9000/ip
Get-Content .\zeroconf\logs\nsp-ip-service.log -Wait
```

`/ip` should return the Windows LAN address, for example:

```json
{"ip":"192.168.1.4"}
```

The Zeroconf log should contain:

```text
CORE API READY host=127.0.0.1 port=8069
ADVERTISER START service=_nsp._tcp.local. ... ip=192.168.1.4 ...
mDNS MONITOR START interface_ip=192.168.1.4 ...
HTTP SERVER START listen=0.0.0.0:9000 ...
```

It must **not** advertise a Docker Desktop VM address such as `192.168.65.x`.

### Stop everything

```powershell
.\stop-nsp.ps1
```

or:

```cmd
stop-nsp.cmd
```

### Restart everything

```powershell
.\restart-nsp.ps1
```

With rebuild:

```powershell
.\restart-nsp.ps1 -Build
```

## Linux

For a native Linux Docker host, `zeroconf/docker-compose.linux.yml` remains available if you intentionally want Zeroconf in a host-network container. Do not use that pattern for Docker Desktop on Windows.
