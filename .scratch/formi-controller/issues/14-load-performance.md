# 14: Load performance on public_03

**What to build:** A measurement script runs public_03 and records ingest events per second, end-to-end decision lag, peak RSS and wall-clock time per simulated day. The controller is optimised until one simulated day takes under 10 wall-clock minutes on the target envelope (4 vCPU / 8 GB), with memory comfortably clear of the simulator's footprint. The numbers and a "what breaks first at 10×, and how we'd partition across workflows and tenants" analysis go into `reports/`.

Spec: user stories 5–6, 77.

**Blocked by:** 09, 10, 12

**Status:** ready-for-agent

- [ ] public_03 runs end to end through `./run.sh`
- [ ] Measured wall-clock per simulated day < 10 minutes, with the measurement method stated
- [ ] Peak RSS of the controller recorded and within budget
- [ ] Load report in `reports/` with numbers and the 10× analysis
