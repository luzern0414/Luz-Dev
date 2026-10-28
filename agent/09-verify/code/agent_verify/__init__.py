"""结果核验（SPEC §9）：规则检查用代码（正则 + 集合比对）；语义检查（承诺、范围）留给小模型，模型不可用时只做规则检查。"""
from .rules import (FORBIDDEN_PROMISES, POLICY_KEYWORDS, SUCCESS_WORDS, GroundTruth, build_ground_truth,
                    verify_answer)
from .template import template_answer

__all__ = ["FORBIDDEN_PROMISES", "POLICY_KEYWORDS", "SUCCESS_WORDS", "GroundTruth", "build_ground_truth",
           "verify_answer", "template_answer"]
