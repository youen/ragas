from __future__ import annotations

import json
import logging
import os
import typing as t

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.prompt_values import PromptValue as BasePromptValue
from langchain_core.pydantic_v1 import BaseModel, root_validator

from ragas.llms import BaseRagasLLM
from ragas.llms.json_load import json_loader
from ragas.utils import get_cache_dir

Example = t.Dict[str, t.Any]


class PromptValue(BasePromptValue):
    prompt_str: str

    def to_messages(self) -> t.List[BaseMessage]:
        """Return prompt as a list of Messages."""
        return [HumanMessage(content=self.to_string())]

    def to_string(self) -> str:
        return self.prompt_str


class Prompt(BaseModel):
    """
    Prompt is a class that represents a prompt for the ragas metrics.
    
    Prompt is a class that represents a prompt for the ragas metrics.

    Attributes:
        name (str): The name of the prompt.
        instruction (str): The instruction for the prompt.
        examples (List[Dict[str, Any]]): List of example inputs and outputs for the prompt.
        input_keys (List[str]): List of input variable names.
        output_key (str): The output variable name.
        output_type (str): The type of the output (default: "json").
        language (str): The language of the prompt (default: "en").
    """

    name: str
    instruction: str
    examples: t.List[Example] = []
    input_keys: t.List[str]
    output_key: str
    output_type: str = "json"
    language = "en"

    @root_validator
    def validate_prompt(cls, values: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        """
        Validate the template string to ensure that it is in desired format.
        """
        if values.get("instruction") is None or values.get("instruction") == "":
            raise ValueError("instruction cannot be empty")
        if values.get("input_keys") is None or values.get("instruction") == []:
            raise ValueError("input_keys cannot be empty")
        if values.get("output_key") is None or values.get("output_key") == "":
            raise ValueError("output_key cannot be empty")

        if values.get("examples"):
            output_key = values["output_key"]
            for no, example in enumerate(values["examples"]):
                for inp_key in values["input_keys"]:
                    if inp_key not in example:
                        raise ValueError(
                            f"example {no+1} does not have the variable {inp_key} in the definition"
                        )
                if output_key not in example:
                    raise ValueError(
                        f"example {no+1} does not have the variable {output_key} in the definition"
                    )
                if values["output_type"].lower() == "json":
                    try:
                        if output_key in example:
                            if isinstance(example[output_key], str):
                                json.loads(example[output_key])
                    except ValueError as e:
                        raise ValueError(
                            f"{output_key} in example {no+1} is not in valid json format: {e}"
                        )

        return values

    def to_string(self) -> str:
        """
        Generate the prompt string from the variables.
        """
        prompt_str = self.instruction + "\n"

        if self.examples:
            # Format the examples to match the Langchain prompt template
            for example in self.examples:
                for key, value in example.items():
                    value = (
                        json.dumps(value, ensure_ascii=False).encode("utf8").decode()
                    )
                    value = (
                        value.replace("{", "{{").replace("}", "}}")
                        if self.output_type.lower() == "json"
                        else value
                    )
                    prompt_str += f"\n{key}: {value}"
                prompt_str += "\n"

        if self.input_keys:
            prompt_str += "".join(f"\n{key}: {{{key}}}" for key in self.input_keys)
        if self.output_key:
            prompt_str += f"\n{self.output_key}: \n"

        return prompt_str

    def get_example_str(self, example_no: int) -> str:
        """
        Get the example string from the example number.
        """
        if example_no >= len(self.examples):
            raise ValueError(f"example number {example_no} is out of range")
        example = self.examples[example_no]
        example_str = ""
        for key, value in example.items():
            value = json.dumps(value, ensure_ascii=False).encode("utf8").decode()
            value = (
                value.replace("{", "{{").replace("}", "}}")
                if self.output_type.lower() == "json"
                else value
            )
            example_str += f"\n{key}: {value}"
        return example_str

    def format(self, **kwargs: t.Any) -> PromptValue:
        """
        Format the Prompt object into a ChatPromptTemplate object to be used in metrics.
        """
        if set(self.input_keys) != set(kwargs.keys()):
            raise ValueError(
                f"Input variables {self.input_keys} do not match with the given parameters {list(kwargs.keys())}"
            )
        prompt = self.to_string()
        return PromptValue(prompt_str=prompt.format(**kwargs))

    def adapt(
        self, language: str, llm: BaseRagasLLM, cache_dir: t.Optional[str] = None
    ) -> Prompt:
        # TODO: Add callbacks
        cache_dir = cache_dir if cache_dir else get_cache_dir()
        if os.path.exists(os.path.join(cache_dir, language, f"{self.name}.json")):
            return self._load(language, self.name, cache_dir)

        prompts = []
        for example in self.examples:
            prompts.extend(
                [
                    str_translation.format(
                        translate_to=language, input=example.get(key)
                    )
                    for key in self.input_keys
                ]
            )
            prompts.append(
                json_translatation.format(
                    translate_to=language, input=example.get(self.output_key)
                )
                if self.output_type.lower() == "json"
                else str_translation.format(
                    translate_to=language, input=example.get(self.output_key)
                )
            )

        # NOTE: this is a slow loop, consider Executor to fasten this
        results = []
        for p in prompts:
            results.append(llm.generate_text(p).generations[0][0].text)
        per_example_items = len(self.input_keys) + 1
        grouped_results = [
            results[i : i + per_example_items]
            for i in range(0, len(results), per_example_items)
        ]
        assert len(grouped_results) == len(
            self.examples
        ), "examples and adapted examples must be of equal length"
        for i, example in enumerate(grouped_results):
            example_dict = {}
            example_dict.update(
                {k: v for k, v in zip(self.input_keys, example[: len(self.input_keys)])}
            )
            example_dict[self.output_key] = (
                json_loader.safe_load(example[-1], llm)
                if self.output_type.lower() == "json"
                else example[-1]
            )

            self.examples[i] = example_dict

        self.language = language

        # TODO:Validate the prompt after adaptation

        return self

    def save(self, cache_dir: t.Optional[str] = None) -> None:
        cache_dir = cache_dir if cache_dir else get_cache_dir()
        cache_dir = os.path.join(cache_dir, self.language)
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        cache_path = os.path.join(cache_dir, f"{self.name}.json")
        with open(cache_path, "w") as file:
            json.dump(self.dict(), file, indent=4)

    @classmethod
    def _load(cls, language: str, name: str, cache_dir: str) -> Prompt:
        logging.log(logging.INFO, f"Loading {name} from {cache_dir}")
        path = os.path.join(cache_dir, language, f"{name}.json")
        return cls(**json.load(open(path))["kwargs"])


str_translation = Prompt(
    name="str_translation",
    instruction="Language translation",
    examples=[
        {
            "translate_to": "hindi",
            "input": "Who was  Albert Einstein and what is he best known for?",
            "output": "अल्बर्ट आइंस्टीन कौन थे और वे किसके लिए सबसे ज्यादा प्रसिद्ध हैं?",
        },
    ],
    input_keys=["translate_to", "input"],
    output_key="output",
    output_type="str",
)

json_translatation = Prompt(
    name="json_translation",
    instruction="Translate values in given json to target language ",
    examples=[
        {
            "translate_to": "hindi",
            "input": """{"statements": [
            "Albert Einstein was born in Germany.",
            "Albert Einstein was best known for his theory of relativity."
        ]}""",
            "output": """{"statements": [
    "अल्बर्ट आइंस्टीन का जन्म जर्मनी में हुआ था।",
    "अल्बर्ट आइंस्टीन अपने सापेक्षता के सिद्धांत के लिए सबसे अधिक प्रसिद्ध थे।"
    ]}""",
        }
    ],
    input_keys=["translate_to", "input"],
    output_key="output",
    output_type="JSON",
)
