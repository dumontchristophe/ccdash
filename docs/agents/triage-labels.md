# Triage labels

The skills speak in terms of five canonical triage roles. This file maps those
roles to the actual label strings used in this repo's issue tracker — the
defaults, each label string equal to its role name.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the
corresponding label string from this table.

## Labels outside the five roles

These aren't triage states, but skills apply them by name and they have to exist
in the tracker:

| Label  | Applied by | Meaning |
|---|---|---|
| `spec` | `/to-spec` | The issue body is a written spec, published alongside `ready-for-agent` |

The `wayfinder:*` family (`wayfinder:map`, `wayfinder:research`,
`wayfinder:prototype`, `wayfinder:grilling`, `wayfinder:task`) is deliberately
left out: `/wayfinder` has never run on this repo. Create them the day it does —
`docs/agents/issue-tracker.md` describes the operations.

## Availability

The six labels above all exist on `dumontchristophe/ccdash` — apply them
freely. Should a seventh ever be needed, create it first: `gh issue edit
--add-label` on a label that doesn't exist fails, it does not create it.

```sh
gh label create <name> --description "<what it means>"
```

Edit the right-hand column to match whatever vocabulary you actually use.
