"""Host package of the Universal Plugin layer.

`contract.py` declares the `HostBridge` protocol (no host import). The REAPER
implementation lands in `reaper.py` (P3) and is the ONLY module inside
`plugins/` that may import the host API (08 §8).
"""
