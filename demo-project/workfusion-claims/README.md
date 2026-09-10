# WorkFusion Claims Automation — PROVISIONAL DEMO FIXTURE

> **Read this before drawing conclusions from it.**
>
> This project was **not exported from WorkFusion**. It was constructed to be
> WorkFusion-*shaped* — BPMN 2.0 process definitions (WorkFusion builds on Activiti,
> so the process elements are BPMN's), Groovy action scripts, and bot configuration —
> so that Aura's migration flow can be exercised end to end before a real export is
> available.
>
> Structural details may not match a real customer export. In particular the
> WorkFusion-specific task extensions (`botTask`, `ocrTask`, `mlTask` below) are an
> inference about how WorkFusion extends BPMN, not something verified against the
> product. If your export names them differently, the curated profile in
> `src/migration/profiles.py` needs correcting to match — and so does this fixture.
>
> **Replace this with a real export when one is available.** Nothing else in the
> migration feature depends on this fixture's shape: the generic path reads files and
> the knowledge graph rather than parsed WorkFusion facts.

## What it represents

A claims-processing automation for a mid-size insurer. Four processes, six bots, a
handful of Groovy actions, and the usual accumulation of a platform used for eight
years.

| Process | What it does | Notable |
|---|---|---|
| `claims-intake` | Receives a claim, OCRs the documents, validates it | **OCR task** — a platform feature, not code |
| `claims-adjudication` | Applies policy rules, decides pay/deny/refer | **Attended** — an adjuster reviews referrals |
| `payout-processing` | Calculates and issues payment | Timer-triggered, unattended |
| `fraud-screening` | Scores a claim against historical patterns | **ML task** — a platform feature |

## Why these four

Each is a different migration outcome, which is the point of a demo fixture:

- `payout-processing` should port cleanly — scheduled, unattended, ordinary logic.
- `claims-intake` contains an OCR task with **no Airflow equivalent**. It cannot be
  translated; something has to replace it.
- `claims-adjudication` has a **human in the loop**. Airflow is an unattended
  scheduler; this is a redesign, not a port.
- `fraud-screening` depends on WorkFusion's ML tasks, likewise a platform feature.

A migration report that comes back saying all four port cleanly is wrong, and this
fixture is arranged so that is visible.

## Component standards it declares

`requirements.txt` and `config/application.properties` reference Splunk, HashiCorp
Vault and Dynatrace — so Aura's capability inference has real evidence to find and
propose, rather than an empty form.
