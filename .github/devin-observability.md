<!--
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on an
"AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
KIND, either express or implied.  See the License for the
specific language governing permissions and limitations
under the License.
-->

# Devin issue automation: observability

The `devin:ready` automation picks up labeled issues and opens PRs (it never
merges; the `master` ruleset requires green CI plus one human approval). This
page defines how its behaviour is observed.

**Surface:** the "Devin status board" comment, refreshed daily (and on demand
via *Actions → Devin status board → Run workflow*) by
`.github/workflows/devin-report.yml`, which runs `scripts/devin_report.py`.
The raw markdown and a JSON dump are also attached to each run as an artifact
and shown in the run summary.

## Setup

| Where | Name | Value |
|---|---|---|
| Repository secret | `DEVIN_API_KEY` | Devin v3 service-user key with `ViewOrgSessions` |
| Repository secret | `DEVIN_ORG_ID` | `org-…` |
| Repository variable | `DEVIN_AUTOMATION_ID` | `auto-…` (the `devin:ready` automation) |
| Repository variable | `DEVIN_BOARD_ISSUE` | optional; issue number hosting the board (auto-created and labeled `devin:status-board` otherwise) |
| Repository variable | `DEVIN_REPORT_LOOKBACK_DAYS` | optional; only issues updated within this window are scanned (default 90) |

Any explicitly selected board issue (variable or manual input) must carry the
`devin:status-board` label, otherwise the run fails instead of posting.

Without the Devin secrets the report still runs on GitHub data only; health and
ACU columns show `unknown`/`0`.

## Questions and how they are answered

### Is the automation active and healthy?

| Value | Meaning |
|---|---|
| `healthy` | Automation `enabled` in Devin and every recent `devin:ready` event produced a session |
| `DEGRADED` | Enabled, but at least one issue is `dispatch-missed` (see below) |
| `DISABLED` | Automation is switched off |
| `unknown (DEVIN_API_KEY not configured)` | Devin secrets/automation id not configured |
| `unknown (Devin API error)` | The Devin session API could not be reached; board built from GitHub evidence only (see the note for the HTTP detail) |
| `unknown (automation record not readable)` | Sessions were fetched but the automation record is not visible to the service user (personal `run_as: creator` automations), so enabled state could not be verified |

The Devin API exposes `last_invocation` (`succeeded | failed | skipped` and
timestamp) but no run history, so health is inferred from enabled state plus
observed sessions. Dispatch failures also email the automation owner.

### Which issues completed, failed, or remain blocked?

One row per issue whose timeline contains a `labeled devin:ready` event (so rows
survive label clean-up). Status precedence:

| Status | Definition |
|---|---|
| `blocked` | Any `blocked-*` label present (agent must not act) |
| `merged` | Devin's PR is merged — remediation accepted by a human (labels no longer matter) |
| `done` | `devin:done`, PR open, CI not failing — agent finished, awaiting review |
| `failed-ci` | `devin:done` but the PR's latest check runs failed |
| `in-progress` | `devin:in-progress` with a session `working`/`resumed`, or within 6 h of labeling |
| `in-progress (session data unavailable)` | `devin:in-progress` but the Devin session API was unreachable, so `stalled` cannot be inferred |
| `stalled` | `devin:in-progress` but session `blocked`/`expired` or older than 6 h with no PR |
| `finished-no-label` | Sessions exist but no `devin:*` outcome label — agent stopped at the gate or crashed; read the session |
| `dispatch-missed` | `devin:ready` for >15 min and no session — automation did not fire (bot-added labels do not trigger it) |
| `ready (session data unavailable)` | `devin:ready` but the Devin session API was unreachable, so `dispatch-missed` cannot be inferred |
| `queued` | `devin:ready` added <15 min ago |
| `closed` | Issue closed without a merged Devin PR (abandoned or done by hand) |
| `unlabeled` | Open issue whose `devin:*` labels were removed; kept for history |

### Did the agent finish, and did the remediation pass?

* *Agent finished* = Devin session status is terminal (`finished`, `blocked`,
  `expired`) — shown by the session link in **Evidence**.
* *Remediation passed* = `CI` column `success` **and** `Approved` `yes`
  (a human reviewed) **and/or** status `merged`. CI alone is necessary, not
  sufficient: the ruleset makes a human approval the acceptance signal.

### Portfolio progress, controlled replays, observed consumption

* **Progress**: `terminal/tracked`, where terminal = `done + merged + failed-ci`;
  full breakdown per status.
* **Controlled replays**: a replay is a human removing and re-adding
  `devin:ready` on the same issue (the only supported re-trigger, since the
  Devin bot cannot fire its own automation). The `Replays` column counts
  `labeled devin:ready` events minus one; `Sessions` shows how many Devin
  sessions were matched. Before replaying, remove `devin:in-progress`/`devin:done`
  so the gate lets the new session claim the issue.
* **Consumption**: `ACUs` per issue is the sum of `acus_consumed` over matched
  sessions; the portfolio total covers every session spawned by the automation.

### Which evidence supports each status?

Each row's **Evidence** column links, in order: the issue (labels + timeline),
every matched Devin session, the PR, and the PR checks page. The workflow run
that produced the board is linked at the bottom of the comment. Status is a
pure function of these sources (`_classify` in `scripts/devin_report.py`), so
any status can be reproduced by opening the links.

## PR-centric snapshots and gap dispatch (`scripts/devin_observability.py`)

The status board answers "which issues are done?". The observability collector
answers "is every Devin PR being looked after?" and feeds a Postgres database
that Superset itself can explore.

**Cadence.** The `observability` job of `devin-report.yml` runs every 6 hours
(and with every daily board run). It always uploads `devin-obs-snapshot.json`
as an artifact and, when `DEVIN_OBS_DATABASE_URL` is set, upserts it into the
`devin_obs` schema (`snapshots`, `pull_requests`, `pull_request_history`,
`check_runs`, `sessions`, `automations`, `findings`, `dispatches`).

**Findings.** For every open, non-draft PR the collector derives
`ci-failed-unattended`, `review-unaddressed` and `changes-requested-unaddressed`
when the condition is older than `DEVIN_OBS_GRACE_HOURS` (default 2) and no
Devin commit, non-dispatch progress comment or session has touched the PR since.
These are exactly the cases the event-driven automations ("pick up `devin:ready`", "address PR
feedback & failed CI") may have missed (dropped webhook, rate limit, session
failure); anything they already handle produces no finding.

**Dispatch.** `dispatch` creates one Devin session per finding via
`POST /v3/organizations/{org}/sessions` (tag `devin-obs`, `max_acu_limit`),
records it in `dispatches`, and leaves a hidden `<!-- devin-obs:dispatch … -->`
marker comment on the PR so later runs (and the recurring Devin automation
"6-hourly observability review") treat the finding as handled. Sessions are told
to fix on the same branch and never merge, approve, enable auto-merge or push
to `master`; humans still merge.

**Task-spec issues.** `.github/ISSUE_TEMPLATE/devin-task.yml` is a structured
spec (goal, scope, acceptance criteria, constraints, size). Opening an issue
with it starts an implementation session directly, without the `devin:ready`
label; the resulting `devin/*` PR is covered by the same feedback/CI
automation and by this collector.

Additional configuration: `DEVIN_OBS_DATABASE_URL` (repository secret),
`DEVIN_OBS_IGNORE_CHECKS` (informational checks, default `actions-timeline`),
`DEVIN_OBS_LOOKBACK_DAYS` (default 60), `DEVIN_CREATE_AS_USER_ID` (optional,
requires `ImpersonateOrgSessions`).

## Limitations

* Sessions are matched to issues by `#<n>` in the session title or by sharing
  the PR URL Devin posts on the issue; sessions that did neither are counted in
  the portfolio total only.
* Scheduled workflows on forks are paused by GitHub after 60 days without
  commits; use *Run workflow* to refresh manually.
* Check-run state reflects the PR head at report time, not at the moment
  `devin:done` was set.
