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

## What this does not do

- It does not fix the cooler. The gradient is unchanged; the node simply
  generates less heat to shed.
- It does not protect against a trip under an unusually heavy load — it lowers
  the ceiling, it does not add a safety governor.
- It costs roughly half the CPU's peak throughput. On a node that also runs CI
  runners and a GPU inference pod, that is a real capacity reduction and the
  reason this should not outlive the hardware repair.
