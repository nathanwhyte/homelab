# NVIDIA GPU configuration

## Power cap on manu — temporary cooling-fault mitigation

**Stopgap, not tuning.** Remove it with `cpu-freq-cap` once manu's CPU cooler is
repasted — see `cpu/README.md` for the fault.

```bash
scp gpu/nvidia/nvidia-power-cap.service gpu/nvidia/install-nvidia-power-cap.sh manu-lan:/tmp/
ssh -t manu-lan 'bash /tmp/install-nvidia-power-cap.sh'
```

Caps the GTX 1080 at **150 W** (default 198 W, card range 90-238 W).

### Why it costs almost nothing

Measured over 3 days on the device-level DCGM series:

| Metric | Value |
| ------ | ----- |
| Time above 50 W | **0.52%** |
| Time above 5% utilization (1m samples) | 0% |
| Average power | 11.3 W |
| Idle power | 10.3 W |
| Peak power | 219.9 W |

The card is an embedding server that sits parked between compendium syncs. Its
average draw is 1 W above idle. Capping it affects only the tail of each burst.

### Why cap it at all, then

The bursts are **correlated with CPU load**. A sync drives the embedder and CI
at the same moment, so the GPU's ~210 W arrives exactly when the CPU is already
hot. This clips the worst coincident spikes; it does not meaningfully change any
average.

Note this is the *smaller* of the two heat sources by integrated energy — the
ARC runners were the sustained load, and they were moved off manu separately.
Peak power says the GPU dominates; energy over time says the opposite. Both
numbers are true and they point different ways.

### Two implementation notes

- **Persistence mode is enabled first, deliberately.** Without it the driver
  unloads when no client holds the device and the power limit resets with it,
  so the cap would silently lapse the first time the embedder idled out.
- **A 219.9 W reading against a 198 W limit is not an overshoot bug.** The limit
  is enforced as a rolling average; brief instantaneous excursions above it are
  expected and DCGM samples them.

### Boot ordering

Both cap units order **`Before=k3s-agent.service`**, not `After=multi-user.target`.
The latter is wrong twice over on this host, and an earlier revision used it:

- `k3s-agent` is itself `Before=multi-user.target` and `WantedBy=multi-user.target`,
  so workloads start *before* that target is reached — the cap would land after
  the embedder was already running.
- `k3s-agent` is `Type=notify` with `TimeoutStartUSec=infinity`, so a stalled
  k3s start blocks `multi-user.target` indefinitely, deferring the thermal
  mitigation on exactly the node that cannot afford it. manu took 43 minutes to
  reach Ready during the 2026-09-14 incident.

`cpu-freq-cap.service` carried the same defect and is fixed in the same change.

### Reinstalling

Both installers `systemctl restart` after `enable`, then read the value back
and fail if it does not match the unit's configured target. `enable --now` does
not restart an already-active oneshot, so without this a rerun after changing
the wattage — or after the limit drifted — would skip the apply and still
report success.

### Remove

```bash
ssh -t manu-lan 'sudo systemctl disable --now nvidia-power-cap cpu-freq-cap'
```

`ExecStop` restores the 198 W default, so no reboot is needed.
