# CPU frequency cap — temporary cooling-fault mitigation

**This is a stopgap on `manu`, not a permanent tuning.** Remove it once the CPU
cooler is repasted or reseated.

## Why

`manu` shut down twice on 2026-09-14 with the AMD firmware reset reason
`[0x00080a00]: internal CPU thermal limit was tripped`, and the same reason is
recorded for the 2026-09-05 crash. Under load the die sat at 84-88 °C against a
42 °C motherboard with the cooler fan already spinning at 2577 RPM — a ~46 °C
gradient across the interface means heat is not reaching the heatsink at all.
That is a mechanical fault (dried paste, a loose or dusty cooler), not a
configuration problem, and no software change fixes it.

Capping the clock cuts heat *generation* so the degraded cooler can keep up:

| | Uncapped (3100 MHz) | Capped (1550 MHz) |
| --- | --- | --- |
| Tctl under load ~4-6 | 84-90 °C, tripping | 54-69 °C |
| Outcome | two thermal shutdowns in one hour | stable |

## Why frequency and not power

There is no writable power cap on this CPU. The RAPL zone exists
(`/sys/class/powercap/intel-rapl:0`, `name=package-0`) but exposes **no**
`constraint_*_power_limit_uw` files and reads `enabled=0` — Zen 1 surfaces
energy counters only. PPT/TDC/EDC live in firmware and are not reachable from
Linux on this board.

`scaling_max_freq` is therefore the lever. Power still falls substantially,
since it scales with frequency and with voltage squared, and the lower P-state
drops both.

## Available P-states

`acpi-cpufreq` on this CPU offers exactly three:

```text
3100000  2250000  1550000
```

A request that is not one of these is **silently rounded down** — an earlier
`cpupower frequency-set --max 2200MHz` landed on 1550000, not 2250000. The
script rejects a non-available value rather than letting that happen quietly.

`1550000` is the installed default because it is the setting actually observed
holding the node stable. `2250000` is untested; if you want more throughput and
are willing to watch temperatures, change `Environment=CAP_KHZ=` in the unit and
`systemctl restart cpu-freq-cap`.

## Install

Copy the files over, then run the installer with a real terminal:

```bash
scp cpu/cpu-freq-cap cpu/cpu-freq-cap.service cpu/install-cpu-freq-cap.sh manu-lan:/tmp/
ssh -t manu-lan 'bash /tmp/install-cpu-freq-cap.sh'
```

Idempotent — re-running re-installs and restarts the unit.

**Do not pipe the installer over stdin.** `ssh -t host 'bash -s' < script` fails
twice over: the script arrives without its sibling files, and ssh will not
allocate a pty when stdin is a redirect ("Pseudo-terminal will not be allocated
because stdin is not a terminal"), so `sudo` has no terminal to prompt on and
the run dies at the first privileged step. `sudo` on `manu` needs a password.

## Operate

```bash
sudo cpu-freq-cap status     # per-CPU current / capped / hardware max
sudo systemctl status cpu-freq-cap
```

## Remove — do this when the cooler is fixed

```bash
ssh -t manu-lan 'sudo systemctl disable --now cpu-freq-cap'
```

`ExecStop` restores every CPU to `cpuinfo_max_freq`, so full clocks come back
immediately without a reboot.

## Temperature alerting

The cap keeps `manu` alive; it does not tell anyone when cooling degrades
again. `cpu/alerts.yaml` adds that, with **per-node thresholds**:

| node | CPU | Tjmax | p99 | 3d peak | warn | crit |
| ---- | --- | ----- | --- | ------- | ---- | ---- |
| manu | Ryzen 7 1700 | 95 | 67.9\* | 110 | 80 | 90 |
| timmy | Ryzen 7 7800X3D | 89 | 50.1 | 65.3 | 85 | 88 |
| wemby | Core i7-8750H | 100 | 80.0 | 96 | 92 | 97 |

\* under the 1550 MHz cap. Revisit manu's thresholds when the cooler is
repasted and the cap comes off.

`timmy` is an X3D part, which is why its numbers look inverted: AMD caps the
7800X3D at 89 °C and it is *designed* to run there under sustained load, so
89 °C is normal rather than a fault. Its thresholds sit just under that
ceiling. A generic 80/90 would page during ordinary heavy load **and** put the
critical above the point the chip already throttles itself at — noisy and
unreachable at the same time.

A single global threshold is not viable: `wemby` is a laptop whose p99 is 80 °C,
which is the same temperature that means "manu is about to trip". Any threshold
low enough to protect manu pre-cap would page continuously on wemby — there is
a unit test asserting exactly that.

Deploy:

```bash
cpu/deploy-cpu-alerts.sh            # or --dry-run
```

Both objects are required. The rule alone carries `alertgroup: hardware`, which
matches no route until `grafana/manifests/hardware-alert-routing.yaml` exists —
until then it is discarded by the default `null` receiver.

Run the rule tests with the pinned Prometheus image:

```bash
uv run --with pyyaml python cpu/tests/check-alerts.py --promtool /path/to/promtool
```

Two implementation notes worth keeping, both learned the hard way on
2026-09-14:

- **k10temp surfaces as a PCI path, not a driver name.** On AMD nodes the chip
  label is `pci0000:00_0000:00:18_3`. A selector containing `k10temp` matches
  nothing, which is why the data looked absent during the incident.
- **`instance` is the node-exporter pod IP and changes when that pod
  restarts.** manu had three distinct `instance` values across the incident
  window. The rules join through `kube_pod_info` to get a stable `node` label;
  anything keyed on `instance` loses the node across the very event it exists
  to catch.

## What this does not do

- It does not fix the cooler. The gradient is unchanged; the node simply
  generates less heat to shed.
- It does not protect against a trip under an unusually heavy load — it lowers
  the ceiling, it does not add a safety governor.
- It costs roughly half the CPU's peak throughput. On a node that also runs CI
  runners and a GPU inference pod, that is a real capacity reduction and the
  reason this should not outlive the hardware repair.
