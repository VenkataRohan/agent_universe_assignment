# 14: Load performance on public_03

**What to build:** A measurement script runs public_03 and records ingest events per second, end-to-end decision lag, peak RSS and wall-clock time per simulated day. The controller is optimised until one simulated day takes under 10 wall-clock minutes on the target envelope (4 vCPU / 8 GB), with memory comfortably clear of the simulator's footprint. The numbers and a "what breaks first at 10×, and how we'd partition across workflows and tenants" analysis go into `reports/`.

Spec: user stories 5–6, 77.

**Blocked by:** 09, 10, 12

**Status:** done

- [x] public_03 runs end to end through `./run.sh`
- [x] Measured wall-clock per simulated day < 10 minutes, with the measurement method stated
- [x] Peak RSS of the controller recorded and within budget
- [x] Load report in `reports/` with numbers and the 10× analysis

## Comments

Measured with `scripts/measure_load.py`; report in `reports/public_03_load.md`. Slowest simulated day 105 s of controller time (budget 600 s), controller peak RSS 450 MB, `./run.sh` exits 0. No optimisation was needed. Measured on a 10-core Mac, not 4 vCPU / 8 GB; the report says so and the controller is single-threaded, so re-take the wall clock on the real envelope before quoting it as met there.
