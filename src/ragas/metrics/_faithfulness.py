from __future__ import annotations

import logging
import typing as t
from dataclasses import dataclass, field

import numpy as np

from ragas.llms.json_load import json_loader
from ragas.llms.prompt import Prompt
from ragas.metrics.base import EvaluationMode, MetricWithLLM

if t.TYPE_CHECKING:
    from langchain_core.callbacks import Callbacks
    from langchain_core.outputs import LLMResult

    from ragas.llms.prompt import PromptValue

logger = logging.getLogger(__name__)

LONG_FORM_ANSWER_PROMPT = Prompt(
    name="long_form_answer",
    instruction="Create one or more statements from each sentence in the given answer.",
    examples=[
        {
            "question": "Who was  Albert Einstein and what is he best known for?",
            "answer": "He was a German-born theoretical physicist, widely acknowledged to be one of the greatest and most influential physicists of all time. He was best known for developing the theory of relativity, he also made important contributions to the development of the theory of quantum mechanics.",
            "statements": {
                "statements": [
                    "Albert Einstein, a German-born theoretical physicist, is renowned for being one of the most influential physicists in history.",
                    "Albert Einstein was best known for his theory of relativity.",
                    "Einstein's contributions significantly advanced the field of quantum mechanics",
                    "Recognized globally, Einstein's work has profoundly impacted the scientific community",
                    "Einstein's groundbreaking theories continue to shape our understanding of physics today.",
                ]
            },
        },
        {
            "question": "Cadmium Chloride is slightly soluble in this chemical, it is also called what?",
            "answer": "alcohol",
            "statements": {"statements": ["Cadmium Chloride is slightly soluble in alcohol."]},
        },
        {
            "question": "Were Hitler and Benito Mussolini of the same nationality?",
            "answer": "Sorry, I can't provide answer to that question.",
            "statements": {"statements": []},
        },
    ],
    input_keys=["question", "answer"],
    output_key="statements",
    output_type="JSON",
)  # noqa: E501


NLI_STATEMENTS_MESSAGE = Prompt(
    name="nli_statements",
    instruction="Natural language inference. Use only 'Yes' (1), 'No' (0) and 'Null' (-1) as verdict.",
    examples=[
        {
            "context": """John is a student at XYZ University. He is pursuing a degree in Computer Science. He is enrolled in several courses this semester, including Data Structures, Algorithms, and Database Management. John is a diligent student and spends a significant amount of time studying and completing assignments. He often stays late in the library to work on his projects.""",
            "statements": """
            statement_1: John is majoring in Biology.
            statement_2: John is taking a course on Artificial Intelligence.
            statement_3: John is a dedicated student.
            statement_4: John has a part-time job.
            """,
            "answer": [
                {
                    "statement_1": "John is majoring in Biology.",
                    "reason": "John's major is explicitly mentioned as Computer Science. There is no information suggesting he is majoring in Biology.",
                    "verdict": "0",
                },
                {
                    "statement_2": "John is taking a course on Artificial Intelligence.",
                    "reason": "The context mentions the courses John is currently enrolled in, and Artificial Intelligence is not mentioned. Therefore, it cannot be deduced that John is taking a course on AI.",
                    "verdict": "0",
                },
                {
                    "statement_3": "John is a dedicated student.",
                    "reason": "The context states that he spends a significant amount of time studying and completing assignments. Additionally, it mentions that he often stays late in the library to work on his projects, which implies dedication.",
                    "verdict": "1",
                },
                {
                    "statement_4": "John has a part-time job.",
                    "reason": "There is no information given in the context about John having a part-time job.",
                    "verdict": "0",
                },
            ],
        },
        {
            "context": """Photosynthesis is a process used by plants, algae, and certain bacteria to convert light energy into chemical energy.""",
            "statements": """statement_1: Albert Einstein was a genius.""",
            "answer": {
                "statement_1": "Albert Einstein was a genius.",
                "reason": "The context and statement are unrelated",
                "verdict": "0",
            },
        },
        {
            "context": """Albert Einstein was a German-born theoretical physicist who is widely held to be one of the greatest and most influential scientists of all time.""",
            "statements": """statement_1: Nil""",
            "answer": {"statement_1": "Nil", "reason": "The statement is invalid", "verdict": "-1"},
        },
    ],
    input_keys=["context", "statements"],
    output_key="answer",
    output_type="JSON",
)  # noqa: E501


@dataclass
class Faithfulness(MetricWithLLM):
    name: str = "faithfulness"  # type: ignore
    evaluation_mode: EvaluationMode = EvaluationMode.qac  # type: ignore
    long_form_answer_prompt: Prompt = field(
        default_factory=lambda: LONG_FORM_ANSWER_PROMPT
    )
    nli_statements_message: Prompt = field(
        default_factory=lambda: NLI_STATEMENTS_MESSAGE
    )
    batch_size: int = 15

    def _create_answer_prompt(self, row: t.Dict) -> PromptValue:
        question, answer = row["question"], row["answer"]

        # extract statements from answer given the question
        prompt_value = LONG_FORM_ANSWER_PROMPT.format(question=question, answer=answer)
        return prompt_value

    def _create_nli_prompt(self, row: t.Dict, answer_result: LLMResult) -> PromptValue:
        assert self.llm is not None, "llm must be set to compute score"

        contexts = row["contexts"]
        # check if the statements are support in the contexts
        contexts_str: str = "\n".join(contexts)
        statements = json_loader.safe_load(
            answer_result.generations[0][0].text, self.llm
        ).get("statements", [])
        statements = statements if statements != [] else ["Nil"]
        statements_str: str = "\n".join(
            [f"statement_{i+1}: {st}" for i, st in enumerate(statements)]
        )
        prompt_value = NLI_STATEMENTS_MESSAGE.format(
            context=contexts_str, statements=statements_str
        )
        return prompt_value

    def _compute_score(self, result: LLMResult):
        assert self.llm is not None, "llm must be set to compute score"

        # check the verdicts and compute the score
        output = result.generations[0][0]
        verdict_score_map = {"1": 1, "0": 0, "null": np.nan}
        output = json_loader.safe_load(output.text, self.llm)
        output = output if isinstance(output, list) else [output]
        faithful_statements = sum(
            verdict_score_map.get(
                statement_with_validation.get("verdict", "").lower(), np.nan
            )
            for statement_with_validation in output
        )
        num_statements = len(output)
        if num_statements:
            score = faithful_statements / num_statements
        else:
            score = np.nan

        return score

    async def _ascore(self: t.Self, row: t.Dict, callbacks: Callbacks) -> float:
        """
        returns the NLI score for each (q, c, a) pair
        """
        assert self.llm is not None, "LLM is not set"
        p = self._create_answer_prompt(row)
        result = await self.llm.agenerate_text(p, callbacks=callbacks)

        p = self._create_nli_prompt(row, result)
        result = await self.llm.agenerate_text(p, callbacks=callbacks)

        return self._compute_score(result)

    def _score(self, row: t.Dict, callbacks: Callbacks) -> float:
        assert self.llm is not None, "LLM is not set"
        p = self._create_answer_prompt(row)
        result = self.llm.generate_text(p, callbacks=callbacks)

        p = self._create_nli_prompt(row, result)
        result = self.llm.generate_text(p, callbacks=callbacks)

        return self._compute_score(result)

    def adapt(self, language: str, cache_dir: t.Optional[str] = None) -> None:
        assert self.llm is not None, "LLM is not set"

        logger.info(f"Adapting Faithfulness metric to {language}")
        self.long_form_answer_prompt = self.long_form_answer_prompt.adapt(
            language, self.llm, cache_dir
        )
        self.nli_statements_message = self.nli_statements_message.adapt(
            language, self.llm, cache_dir
        )

    def save(self, cache_dir: t.Optional[str] = None) -> None:
        self.long_form_answer_prompt.save(cache_dir)
        self.nli_statements_message.save(cache_dir)


faithfulness = Faithfulness()
