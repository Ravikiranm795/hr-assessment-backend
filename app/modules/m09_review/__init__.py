"""M9 Review, decisions, scoring (spec 6/M9, 7.3 auto part). Cross-module interfaces:

- ``scoring.ScoringService.score_attempt`` / ``recompute`` (M8 submit hook runs ``score_attempt``)
- ``hooks.ai_scoring_hooks`` registry (M19 registers the AI scoring runner)
- ``service.ReviewService.erase_candidate_data`` (M17 retention purge)
"""
