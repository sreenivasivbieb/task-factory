"""Reference-guided task generation (pipeline change proposal v2.2).

The factory no longer lets one model invent tasks. Stages P1-P10:

P1 profiler.py   understand the starter repo: capability map with file evidence, spot-checked
P2 scout.py      find the closest peer repos by comparing capability maps (not keywords)
P3 miner.py      mine peer PRs into reference bundles (PR, discussion, diff, tests, follow-ups, docs)
P4 novelty.py    drop features that exist in the starter commit or were merged upstream since
P5 drafting.py   drafter sub-agents turn candidates into task proposals (over-generate)
P6 drafting.py   deterministic checks, then independent critics; variation filter keeps distinct ones
P7 review.py     optional human review (off by default: the auto-approve policy approves)
P8 building.py   author (instruction + patch), test writer (tests from the instruction), package writer
P9 harbor.py     Harbor nop and oracle plus our gates (stability, mutation) decide validity
P10 benchmark.py background jobs run an AI under test: solved / partial / failed / infra error

Zone A (this package) has the network; Zones B and C (sandboxes) never do. State lives in MongoDB
(store.py); research results are saved as evidence snapshots (snapshot.py) so a run replays offline.
"""
