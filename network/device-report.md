# Network Device Report — 192.168.1.0/24

| Field    | Value                                                                            |
| -------- | -------------------------------------------------------------------------------- |
| Router   | NETGEAR Nighthawk R7000 (`192.168.1.1`)                                          |
| Firmware | V1.0.11.100_10.2.100 (R7000_NA / us)                                             |
| Scanned  | 2026-07-02 ~11:03 CDT                                                            |
| Source   | Router admin UI (`/DEV_device.htm`, `/LAN_lan.htm`, `/debug.htm`) via Basic Auth |

### Router runtime

| Metric           | Value                                                                         |
| ---------------- | ----------------------------------------------------------------------------- |
| System uptime    | 58 days 06:11:28                                                              |
| CPU load         | CPU1 6.42% / CPU2 5.22% (Broadcom BCM4709A0, dual-core ARM Cortex-A9 @ 1 GHz) |
| RAM              | 105 MB / 248 MB (~42%)                                                        |
| Flash            | 8 MB / 128 MB (~6%)                                                           |
| Network sessions | 705 active / 65,792 max                                                       |
| Internet status  | Up (WAN via DHCP)                                                             |

## Attached devices — Wired

| #   | IP           | Name      | MAC               |
| --- | ------------ | --------- | ----------------- |
| 1   | 192.168.1.6  | pop       | C8:A3:62:C0:F4:1E |
| 2   | 192.168.1.9  | wemby     | 04:92:26:10:C7:74 |
| 3   | 192.168.1.10 | manu      | 60:45:CB:9E:E7:10 |
| 4   | 192.168.1.12 | TL-SG105E | B0:95:75:32:45:2E |
| 5   | 192.168.1.16 | workbook  | 98:FC:84:E4:BC:E0 |
| 6   | 192.168.1.19 | --        | 6C:6E:07:1B:F4:35 |

## Attached devices — Wireless

| #   | IP           | Name     | MAC               |
| --- | ------------ | -------- | ----------------- |
| 1   | 192.168.1.2  | pop      | FC:B2:14:D7:65:A8 |
| 2   | 192.168.1.4  | iPhone   | 6A:49:A5:89:94:AC |
| 3   | 192.168.1.21 | WORKBOOK | 72:0F:8D:2E:3E:8A |
| 4   | 192.168.1.17 | iPad     | 7A:B7:D8:A9:68:52 |
| 5   | 192.168.1.22 | Samsung  | D8:E0:E1:B7:C6:3F |
| 6   | 192.168.1.11 | TV       | C0:95:6D:88:83:78 |
| 7   | 192.168.1.8  | Speaker  | AC:BC:B5:E1:F6:4A |

## DHCP address reservations

| #   | IP           | Name     | MAC               |
| --- | ------------ | -------- | ----------------- |
| 1   | 192.168.1.9  | wemby    | 04:92:26:10:C7:74 |
| 2   | 192.168.1.10 | manu     | 60:45:CB:9E:E7:10 |
| 3   | 192.168.1.19 | timmy    | 10:FF:E0:B1:7E:04 |
| 4   | 192.168.1.6  | pop      | C8:A3:62:C0:F4:1E |
| 5   | 192.168.1.16 | workbook | 98:FC:84:E4:BC:E0 |

## Cross-reference notes

- **Multi-interface hosts** — `pop` appears on both wired (.6) and wireless (.2); `workbook` appears on wired (.16) and wireless (.21, named `WORKBOOK`).
- **192.168.1.19 mismatch** — the DHCP reservation names this address **timmy** (MAC `10:FF:E0:B1:7E:04`), but the currently attached device reports name `--` with a different MAC (`6C:6E:07:1B:F4:35`). Likely a secondary NIC, a bridge/vNIC, or a different device now occupying the reserved slot. Worth verifying directly on timmy.
- **TL-SG105E** (.12) — a TP-Link 5-port managed switch on the wired segment.
- **Unnamed/unknown hosts** — only `192.168.1.19` lacks a hostname on the attached list. All wireless hosts carry device-supplied names.
- **No DHCP reservation** for `TL-SG105E` (.12), the wireless mobile/AV clients (.2/.4/.8/.11/.17/.21/.22), or the mystery host at .19.
- **Cluster nodes confirmed present**: manu (.10), timmy (.19), wemby (.9) — matches the K3s topology in `HARDWARE.md`. `pop` (.6) and `workbook` (.16) are operator machines.

## Caveats

- The R7000 stock firmware returns HTTP **401 status with the full page body** when Basic Auth is supplied — content is parseable but the status code is misleading.
- Attached-device list reflects the router's ARP/DHCP lease view at scan time; transient or sleeping wireless clients may not appear.
- Stock firmware exposes no per-port link speed, SoC details, thermals, or Wi-Fi chipset info. Use telnet/SSH (if enabled) or third-party firmware for deeper hardware data.
