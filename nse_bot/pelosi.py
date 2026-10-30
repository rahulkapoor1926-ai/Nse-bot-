"""Assessment of the upstream 'Pelosi tracker' for Indian index options.

Upstream reads US House Periodic Transaction Reports. India has no equivalent
public, timely disclosure of legislators' share trades. The nearest public
datasets concern single stocks, not the NIFTY/BANKNIFTY index options this
bot trades:
  - SEBI (Prohibition of Insider Trading) disclosures by company insiders
  - SEBI SAST substantial-acquisition disclosures
  - NSE bulk and block deal reports
None has evidence of predictive value for intraday index option trades, so this
module reports NOT_APPLICABLE and has no code path into scanner, entry, risk or
the gate. A test checks that no trading module imports it.
"""

CANDIDATES = [
    {"name": "Legislators' trade disclosures", "exists_in_india": False,
     "why": "Members of Parliament file asset declarations, not periodic trade reports."},
    {"name": "SEBI PIT insider disclosures", "exists_in_india": True,
     "why": "Company-level insider trades; relevant to single stocks, not index options."},
    {"name": "SEBI SAST disclosures", "exists_in_india": True,
     "why": "Stake changes in individual companies; no index-level signal."},
    {"name": "NSE bulk/block deals", "exists_in_india": True,
     "why": "End-of-day single-stock deals; no tested link to intraday index option moves."},
]


def assess():
    return {"status": "NOT_APPLICABLE", "affects_trading": False, "candidates": CANDIDATES,
            "conclusion": "No Indian equivalent is used. Revisit only with evidence from a "
                          "backtest that passes the 10,000-example gate."}
