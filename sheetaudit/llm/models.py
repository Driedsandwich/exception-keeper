"""Catalog values supplied in Order 03 (official sources fetched 2026-10-24).

https://docs.tokenfactory.nebius.com/quickstart.md
https://tokenfactory.nebius.com/model-catalog.md
Prices are USD per million tokens; no cached discount is assumed without usage.
"""
BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
PRICES = {
    NANO: {"input": 0.06, "output": 0.24},
    SUPER: {"input": 0.30, "cached_input": 0.06, "output": 0.90},
    ULTRA: {"input": 1.00, "cached_input": 0.12, "output": 3.00},
}
