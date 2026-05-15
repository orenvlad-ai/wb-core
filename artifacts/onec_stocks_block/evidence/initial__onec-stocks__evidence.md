# onec_stocks_block initial evidence

Scope:
- bounded 1C/Soykasoft `/hs/soykasoft/stocks_wb` source path;
- parser and normalizer over fixture response;
- explicit stage-mapping boundary without aggregation;
- optional live smoke guarded by env names only.

Checks run:
- `python3 apps/onec_stocks_block_smoke.py` -> passed
- `python3 apps/onec_stocks_block_live_smoke.py` -> skipped because required live env was absent
- `python3 apps/stocks_block_smoke.py` -> passed
- `python3 apps/cogs_by_group_block_smoke.py` -> passed
- `python3 apps/cogs_by_group_block_rule_smoke.py` -> passed
- `python3 -m compileall -q packages/contracts/onec_stocks_block.py packages/application/onec_stocks_block.py packages/adapters/onec_stocks_block.py apps/onec_stocks_block_smoke.py apps/onec_stocks_block_live_smoke.py` -> passed
- `git diff --check` -> passed
- `git diff --cached --check` -> passed

Live smoke behavior:
- required env names are `ONEC_STOCKS_BASE_URL`, `ONEC_STOCKS_BASIC_USER`, `ONEC_STOCKS_BASIC_PASSWORD`, `ONEC_STOCKS_TOKEN`;
- when any required env is missing, the smoke prints a skip with env names only and exits without claiming live success;
- no base URL, login, password, token or response payload is written to this evidence artifact.

Stage semantics evidence:
- fixture stages are preserved as source strings;
- a synthetic previously unknown stage name is accepted by the parser smoke;
- canonical code mapping is optional and row-level only;
- normalization does not aggregate source stages by canonical code.
