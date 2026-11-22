"""Test doubles for the LLM and the web."""
from __future__ import annotations

from outreach.drafting import _Critique, _Draft
from outreach.research import _Pick, _Picks, _Synthesis, _XAngle

GOOD_BODY = ("Hi Madhur,\n\nPraxis describes itself as a consulting and transformation firm working across strategy, "
             "digital and AI, and operational excellence for Indian businesses.\n\nAt KPMG I built an automated Python ETL "
             "pipeline for Sony Entertainment's raw BARC viewership data, replacing a manual process, so I know how much "
             "of strategy work is getting clean data in front of decision-makers.\n\nI'm looking for a remote internship "
             "in strategy or analytics this year. Is it worth a short conversation, or is there someone on your team "
             "better placed to ask?")


class FakeLLM:
    """Deterministic stand-in for outreach.llm.LLM. `draft_bodies` is consumed one per writer call."""

    def __init__(self, draft_bodies=None, critic_verdicts=None, facts=None, raise_on=None):
        self.provider = "fake"
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.draft_bodies = list(draft_bodies or [GOOD_BODY])
        self.critic_verdicts = list(critic_verdicts or ["pass"])
        self.facts = facts
        self.raise_on = raise_on or set()
        self.prompts: list[str] = []

    available = True

    def model(self, tier):
        return f"fake-{tier}"

    def describe(self):
        return "fake"

    def structured(self, tier, system, prompt, schema, max_tokens=4000, effort="medium"):
        self.calls += 1
        self.prompts.append(prompt)
        if schema.__name__ in self.raise_on:
            from outreach.llm import LLMError
            raise LLMError("server_error", "boom")
        if schema is _Picks:
            return _Picks(picks=self.facts if self.facts is not None else [])
        if schema is _Synthesis:
            return _Synthesis(company_summary="Consulting firm.", role_scope="Likely runs the firm.",
                              angles=[_XAngle(angle="Data-heavy consulting work.", fact_ids=["F1"], proof_point="kpmg_sony_etl")])
        if schema is _Draft:
            body = self.draft_bodies.pop(0) if len(self.draft_bodies) > 1 else self.draft_bodies[0]
            return _Draft(subject="praxis data work", body=body,
                          followup_1="One more thought: I also analysed 200+ SKUs of competitor pricing at Yokohama. "
                                     "Would that kind of work be useful to your team?",
                          followup_2="If this isn't a fit, no worries — I won't keep emailing. Is there someone else I should ask?",
                          fact_ids=["F1"], proof_point="kpmg_sony_etl", angle="data pipelines for strategy")
        if schema is _Critique:
            v = self.critic_verdicts.pop(0) if len(self.critic_verdicts) > 1 else self.critic_verdicts[0]
            return _Critique(unsupported_claims=[] if v == "pass" else ["claims a recent acquisition"],
                             style_issues=[], verdict=v)
        raise AssertionError(f"unexpected schema {schema}")


def pick(sentence_id, claim, about="company", kind="news"):
    return _Pick(sentence_id=sentence_id, claim=claim, about=about, kind=kind)
