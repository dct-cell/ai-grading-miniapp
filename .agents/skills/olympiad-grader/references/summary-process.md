# Compact summary grading contract

This contract applies only to `summary_report`. It keeps the same competition
rubric and mathematical checks as annotated review, but records only evidence
needed to justify each score and reason. Do not create line-level annotations,
`marking-scheme.json`, `proof-map.json`, `verification.json`, or
`score-audit.json`.

## Principles

- Reconstruct the student's actual route before scoring; accept valid alternative
  methods and never retrofit reasons to a desired total.
- Verify decisive hypotheses, cases, computations and conclusions. Finding one
  error does not end verification of independent score-bearing work.
- Treat a suspected typo as local only when context uniquely determines the
  correction, it adds no new idea or condition, and later work uses that reading.
- Record each root issue once and do not deduct again for downstream consequences.
- Missing work is `missing` and scores zero. Routine or merely stylistic omissions
  are not root issues.
- Use the selected IMO, CMO or League rubric unchanged. A verified problem-specific
  scoring standard takes precedence where the selected rubric permits it.
- Without a problem-specific League standard, 20 or 30 remains possible when
  multiple independent, substantive 10-point achievements are verified; treat
  those bands as uncommon and explain the evidence rather than counting steps.

## Six stages

At each stage start run `scripts/report_stage.py STAGE_ID`. Use this order and skip
the annotated-only `rubric`, `decomposing`, and `auditing` stages.

1. `preparing`: render and inspect every submitted page; identify page ownership.
2. `understanding`: identify each problem, target, student route, missing work and
   contextual interpretations; draft `output/internal/summary-analysis.json`.
3. `verifying`: keep only decisive evidence and independent root issues, verify
   them, then freeze `summary-analysis.json`.
4. `scoring`: apply the selected rubric, challenge the result once, and write
   `output/internal/summary-audit.json`.
5. `reporting`: copy the audited public fields into `output/grading.json` and build
   `output/report.pdf`.
6. `validating`: render and inspect the report, fix defects, reopen the PDF, then
   follow `SKILL.md` to write the manifest draft and run the trusted workspace
   validator. Use at most two correction rounds and stop editing after it passes.

Both internal files use this header:

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null
}
```

## `summary-analysis.json`

Cover every submitted page under at least one problem. Evidence is selective:
include only claims that decide credit, a cap, or a root issue. Do not include
line numbers, source quotes, formula positions, `bbox`, or `bboxes`.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "reference_use": {
    "status": "absent",
    "note": "No reference PDF was provided."
  },
  "problems": [
    {
      "id": "p1",
      "label": "第 1 题",
      "pages": [1],
      "submission_status": "answered",
      "target": "The exact mathematical target.",
      "student_route": "A faithful compact account of the submitted route.",
      "interpretations": [
        {"reading": "按上下文理解为 x\\ge 0", "score_relevant": false}
      ],
      "evidence": [
        {
          "id": "p1-a1",
          "claim": "A decisive submitted achievement or claim.",
          "verdict": "valid",
          "reason": "Why the claim is or is not mathematically established."
        }
      ],
      "root_issues": [
        {
          "id": "p1-e1",
          "description": "The first independent reason credit is withheld.",
          "repair_scope": "global",
          "evidence_ids": ["p1-a1"]
        }
      ]
    }
  ]
}
```

`reference_use.status` is `absent`, `used`, or `rejected` and its note is always
nonempty. `submission_status` is `answered`, `partial`, or `missing`; evidence
verdicts are `valid`, `invalid`, `unsupported`, or `ambiguous`; repair scope is
`local` or `global`. Keep at most three independent root issues. Every non-`valid`
decisive evidence item must appear in exactly one root issue's `evidence_ids`;
use an empty list only for genuinely missing work with no written evidence item.

## `summary-audit.json`

The audit contains one final decision per problem, not a checkpoint ledger. Use
`rubric_source: profile` with `rubric_reference: null` by default. Use `specific`
only with `rubric_reference: {"source": "reference_pdf" | "web",
"description": "..."}` identifying a verified problem-specific standard.
`reference_pdf` requires an adopted reference PDF; `web` requires enabled
public-source verification. Public issues are limited to three independent roots.

```json
{
  "analysis_version": 1,
  "grading_standard": "imo",
  "resolved_league_scope": null,
  "total_score": 6,
  "max_score": 7,
  "problems": [
    {
      "problem_id": "p1",
      "submission_status": "answered",
      "score": 6,
      "max_score": 7,
      "rubric_source": "profile",
      "rubric_reference": null,
      "credit_evidence_ids": ["p1-a1"],
      "verdict": "The decisive correct work and the reason for lost credit.",
      "issues": [
        {
          "issue_id": "p1-e1",
          "title": "Missing necessary condition",
          "reason": "The written argument uses the result before proving its hypothesis.",
          "deduction": 1
        }
      ],
      "review": {
        "typo_checked": true,
        "independent_credit_checked": true,
        "double_count_checked": true,
        "band_and_total_checked": true
      }
    }
  ]
}
```

Every credited evidence ID must name `valid` evidence from `summary-analysis`.
Every issue ID must name one root issue and appear at most once. Issue deductions
must total `max_score - score`; a full-score problem has no issues. Missing work
scores zero. Problem order, status, scores, verdicts, public issues and totals must
exactly match `output/grading.json` after internal `issue_id` fields are removed.
