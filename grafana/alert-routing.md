# Alert routing — how an alert reaches a human

The contract for anyone adding a Prometheus alert rule to this cluster. It exists
because between roughly February and September 2026 it did not, and 169 of 194
alert rules — 40 of them critical — reached nobody (BUG-1129).

## The short version

**Write the rule. Do not touch the routing.** Delivery is fail-open: the default
Alertmanager receiver is Slack, so a new rule is delivered by default. Only
_suppression_ needs a routing change.

Then prove it:

```bash
./grafana/check-alert-routing.sh
```

It evaluates every loaded rule's real label set against the live merged route
tree and fails if any rule reaches no receiver, or reaches two. Run it after
adding a rule, and after any change to routing or to an AlertmanagerConfig CR.

## What went wrong before

Delivery used to be an **allowlist** keyed on `alertgroup`, a label that is not a
Prometheus or kube-prometheus-stack convention — it was invented here. The root
route's receiver was `null`, and only `alertgroup=backup` and `alertgroup=storage`
had routes. Every other rule was born mute: the ~170 rules shipped by the chart,
and the GPU, Garage, Omnipendium and OpenViking rules written in this repo.

Nothing failed. Nothing logged. The rules simply evaluated and went nowhere, and
the resting state of the system was silence. manu crashed twice on 2026-09-14 and
`KubeNodeNotReady`, `KubeNodeUnreachable` and `KubeletDown` were all in the
unrouted set. What did arrive was `LonghornVolumeDegraded` — a second-order
consequence on one of the two routes that happened to work. The stack reported
the symptom and suppressed the cause.

`WembyOnBattery` is the cautionary detail: when someone needed one alert to
deliver, the route grew an `alertname` matcher instead of the rule gaining the
label. That made the one alert anybody had deliberately tested work, by a
mechanism that generalised to nothing, and removed the only signal that would
have exposed the gap.

## The route tree

Defined in `helm/kube-prometheus-stack-values.yaml` under `alertmanager.config.route`.

- **Default receiver: `slack-homelab`.** Anything not matched below is delivered.
- `Watchdog`, `InfoInhibitor` → `null`. Plumbing, not signal.
- `alertgroup =~ "backup|hardware|storage"` → `null`. Not a suppression: these
  are already delivered by the matching AlertmanagerConfig CR, which the operator
  merges _ahead_ of these routes with `continue: true`. Terminating them keeps
  them to one Slack message instead of two.
- `severity = "info"` → `null`. Recorded in Prometheus for dashboards, not paged.
- `severity = "critical"` → `slack-homelab` with `group_wait: 10s`,
  `repeat_interval: 30m` — faster and more persistent than the 1h default.

Adding an alertgroup CR means adding that group to the dedupe matcher. The
checker fails if you forget, because the alert then pages twice.

## Two traps that make a working setup look broken, or a broken one look fine

### `amtool config routes test` — pass each label as its own argument

amtool joins positional args into one matcher expression, so a label pair inside
one shell word becomes a single matcher whose value is the whole string, matching
nothing. The wrong answer is `null`, which reads exactly like "undelivered".

```bash
# WRONG — one argument. Parses as alertgroup="storage severity=warning".
amtool config routes test --config.file=$CFG "alertgroup=storage severity=warning"   # -> null

# RIGHT — two arguments.
amtool config routes test --config.file=$CFG alertgroup=storage severity=warning     # -> ...slack-homelab
```

A quoted loop variable reproduces the wrong form silently. amtool does warn
(`unexpected severity: expected a comma or close brace`) but prints `null` after
it, so the result is easy to read and the warning easy to skip.

### The `channel:` field was a lie for modern Slack webhooks — fixed by IMPR-1173

**Resolved 2026-09-15.** This section is retained because it documents the failure
class, but the transport it describes is no longer in use.

`slack_configs.channel` **used to be ignored**. A Slack incoming webhook is bound
to the channel chosen when the webhook was created, and the override only ever
worked for legacy webhooks. Alertmanager got HTTP 200 `ok` and counted a success
regardless of where the message actually landed.

That was not theoretical: on 2026-09-15 the config said `#cron-homelab` and every
alert was arriving in `#hermes-noise`, because the webhook had been rebound. The
routing metrics were perfect and the channel was silent.

**All four receivers now post to `https://slack.com/api/chat.postMessage` with a
bot token**, where `channel` is a request parameter. The three
`AlertmanagerConfig` CRs carry it in `httpConfig.authorization`; the base receiver
inherits it through the chart's global `slack_app_token_file`:

| Receiver                       | Transport                                                |
| ------------------------------ | -------------------------------------------------------- |
| base `slack-homelab`           | global `slack_app_token_file` -> `chat.postMessage`      |
| `backup-alert-routing`         | `apiURL` + `httpConfig.authorization` (Bearer)           |
| `hardware-alert-routing`       | same                                                     |
| `storage-alert-routing`        | same                                                     |

Both credentials live in Secret `alertmanager-slack-bot-token` (namespace
`grafana`): key `bot-token` (the `@newtbot` token) and key `api-url` (the endpoint
literal — the CRD's `apiURL` is a `SecretKeySelector`, not an inline URL).

`check-slack-transport.sh` now fails if any receiver is ever put back on a
`hooks.slack.com` webhook while still carrying a `channel` field, which is exactly
the state that let this run for six days:

```bash
./grafana/check-slack-transport.sh
```

**`alertmanager_notifications_total` increasing is still not evidence of
delivery.** It never was, and the new transport does not change that: the counter
proves a POST was attempted, not where it landed. `check-alert-routing.sh` proves
an alert _routes_ to a receiver; `check-slack-transport.sh` proves the receiver's
destination is _knowable_. Neither proves a message arrived. Only the channel
does.

To verify end to end:

```bash
kubectl -n grafana exec alertmanager-prom-alertmanager-0 -c alertmanager -- amtool \
  --alertmanager.url=http://localhost:9093 alert add \
  'alertname="RoutingProbe"' 'severity="critical"' 'namespace="grafana"' \
  --annotation='summary="synthetic routing probe, expecting this in #cron-homelab"'
```

then read the channel. Quote each label and annotation; the unquoted form hits
the same parser trap as above and the alert is silently not created. **Name the
expected channel inside the probe text** — a probe that does not say where it
should arrive asks "did you see it", not "where did you see it", and both answers
are "yes" for a mis-delivered alert.

## Node-down testing

Cordoning a node does **not** test `KubeNodeNotReady`. The rule explicitly
excludes cordoned nodes (`kube_node_spec_unschedulable == 0`) so that planned
maintenance does not page. Testing it for real means stopping `k3s-agent` on a
node and waiting out the 15m `for:` — disruptive, and it belongs in a planned
window rather than routine verification.

## k3s is not kubeadm

`kubeScheduler`, `kubeProxy` and `kubeControllerManager` are disabled in the
chart values. k3s runs all three inside the single server process and publishes
no per-component metrics endpoint, so the chart's Services sat at
`ENDPOINTS: <none>` for 180 days and their `*Down` alerts fired permanently.
They were invisible only because routing dropped them; with fail-open routing
they would have been the loudest thing in the channel. Do not re-enable them.
