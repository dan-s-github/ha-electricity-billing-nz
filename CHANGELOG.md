## v0.1.0rc0 (2026-11-24)

### Feat

- insert, edit and delete retailer rate versions
- price bill replays with EMI final prices
- add diagnostic sensors for the rates in force
- add a tool to replay a past bill from recorded data
- meter billing from grid power sensors
- warn when a billing meter reports too rarely
- estimate the bill for the current billing cycle
- show the bundled network rates during setup
- pre-select network and categories from the ICP's registry record
- change the GST setting in the options
- add Vector's 2025 pricing schedule (v2025.1)
- key entries by an optional ICP number
- implement landed price sensors, flows and services

### Fix

- keep a replaced meter's coverage across further restarts
- address Copilot review of PR #11
- show diagnostic rates without padded zeros
- keep a cycle's metering start when a meter is replaced
- add late energy to final periods instead of replacing them
- skip hidden and undecodable network files
- address Copilot's review of the billing cycle
- address Copilot's second review of the Phase 1 PR
- pick trading periods in UTC and report failed schedule tests
- address Copilot review of the Phase 1 PR
- place forecast rows stamped with their period's end
- find band changes in the repeated DST hour, and stop at the next version
