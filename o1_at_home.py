"""
title: Think-Respond Chain Pipe, o1 at home
author: latent-variable
github: https://github.com/latent-variable/o1_at_home
version: 0.3.3
Descrition: Think-Respond pipe that has an internal reasoning steps and another for producing a final response based on the reasoning.
            Now supports openAI api along with ollama, you can mix and match models 

Instructions: 
To use the o1 at home pipe, follow these steps:

Add the Pipe Manifold:
Navigate to the Admin Panel and add the pipe to the list of available "Functions" using the '+'.
This is not a "pipeline", Ensure you are using Function tab. 
If you are copying the code you might need ot give it name and descriprition 

Enable the Pipe Manifold:
After adding it, enable the pipe to make it active.

Customize Settings:
Use the configuration menu (accessed via the settings cog) to tailor the pipeline to your needs:
    Select Models: Choose your desired thinking model and response model.
    Show Reasoning: Decide whether to display the reasoning process or keep it hidden.
    Set Thinking Time: Specify the maximum time allowed for the reasoning model to process.
Save and Apply:
Once configured, save your settings to apply the changes.
You should now have o1 at home in your dorp down. 

These steps ensure the pipe is set up correctly and functions according to your requirements.
"""

import json
from time import time
from pydantic import BaseModel, Field
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable, Awaitable, Any, AsyncGenerator
import asyncio
from open_webui.utils.misc import get_last_user_message
from open_webui.apps.openai import main as openai
from open_webui.apps.ollama import main as ollama
import logging

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.set_name("think_respond_chain_pipe")
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False


@dataclass
class User:
    id: str
    email: str
    name: str
    role: str


class Pipe:
    class Valves(BaseModel):
        THINKING_MODEL: str = Field(
            default="your_thinking_model_id_here",
            description="Model used for the internal reasoning step. Separate multiple models with a comma.",
        )
        USE_OPENAI_API_THINKING_MODEL: bool = Field(
            default=False,
            description="Off will use Ollama, On will use any OpenAI API",
        )
        RESPONDING_MODEL: str = Field(
            default="your_responding_model_id_here",
            description="Model used for producing the final response.",
        )
        USE_OPENAI_API_RESPONDING_MODEL: bool = Field(
            default=False,
            description="Off will use Ollama, On will use any OpenAI API",
        )
        ENABLE_SHOW_THINKING_TRACE: bool = Field(
            default=False,
            description="Toggle show thinking trace.",
        )

        MAX_THINKING_TIME: int = Field(
            default=120,
            description="Maximum time in seconds that each thinking model is allowed to run for.",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()
        self.total_thinking_tokens = 0
        self.max_thinking_time_reached = False
        self.__user__ = None
    

    def pipes(self):
        name = "o1-"
        for model in self.valves.THINKING_MODEL.split(","):
            name += model.strip().split(":")[0] + "-"
        name = name[:-1] + "-to-" + self.valves.RESPONDING_MODEL.strip().split(":")[0]
        return [{"name": name, "id": name}]

    def get_chunk_content(self, chunk: bytes):
        """
        Process a chunk of data from the API stream.

        Args:
            chunk (bytes): The raw byte content received from the API stream.
            api (str): The source API, either 'openai' or 'ollama'.

        Yields:
            str: The extracted content from the chunk, if available.
        """
        chunk_str = chunk.decode("utf-8").strip()

        # Split the chunk by double newlines (OpenAI separates multiple data entries with this)
        for part in chunk_str.split("\n\n"):
            part = part.strip()  # Remove extra whitespace
            if part.startswith("data: "):
                part = part[6:]  # Remove "data: " prefix for OpenAI chunks

            if not part or part == "[DONE]":
                continue  # Skip empty or end markers

            try:
                chunk_data = json.loads(part)
                if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                    delta = chunk_data["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:  # Only yield non-empty content
                        yield content
            except json.JSONDecodeError as e:
                logger.error(f'ChunkDecodeError: unable to parse "{part[:100]}": {e}')

    async def get_response(
        self, model: str, messages: List[Dict[str, str]], thinking: bool, stream: bool
    ):
        """
        Generate a response from the appropriate API based on the provided flags.

        Args:
            model (str): The model ID to use for the API request.
            messages (List[Dict[str, str]]): The list of messages for the API to process.
            thinking (bool): Whether this is the 'thinking' phase or the 'responding' phase.

        Returns:
            tuple: (response, api_source) where `response` is the API response object
                and `api_source` is a string ('openai' or 'ollama') indicating the API used.
        """
        # Determine which API to use based on the `thinking` flag and the corresponding valve
        use_openai_api = (
            self.valves.USE_OPENAI_API_THINKING_MODEL
            if thinking
            else self.valves.USE_OPENAI_API_RESPONDING_MODEL
        )

        # Select the appropriate API and identify the source
        if use_openai_api:
            generate_completion = openai.generate_chat_completion
        else:
            generate_completion = ollama.generate_openai_chat_completion

        # Generate response
        response = await generate_completion(
            {"model": model, "messages": messages, "stream": stream}, user=self.__user__
        )

        return response

    async def get_completion(
        self,
        model: str,
        messages: list,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ):
        response = None
        try:
            thinking = False
            stream = False
            response = await self.get_response(model, messages, thinking, stream)
            return response["choices"][0]["message"]["content"]
        except Exception as e:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"Error: ensure {model} is a valid model option {e}",
                        "done": True,
                    },
                }
            )
        finally:
            if response and hasattr(response, "close"):
                await response.close()

    async def stream_response(
        self,
        model: str,
        messages: List[Dict[str, str]],
        thinking: bool,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> AsyncGenerator[str, None]:

        start_thought_time = time()
        try:
            stream = True
            response = await self.get_response(model, messages, thinking, stream)
            while True:
                chunk = await response.body_iterator.read(1024)
                if not chunk:  # No more data
                    break
                for part in self.get_chunk_content(chunk):
                    yield part

                if thinking:
                    current_time = (
                        time()
                    )  # check to see if thought time has been exceded
                    if (
                        current_time - start_thought_time
                    ) > self.valves.MAX_THINKING_TIME:
                        logger.info(
                            f'Max thinking Time reached in stream_response of thinking model "'
                        )
                        self.max_thinking_time_reached = True
                        break

        except Exception as e:
            if thinking:
                api = 'openai' if self.valves.USE_OPENAI_API_THINKING_MODEL else 'Ollama'
                category = 'Thinking'
            else:
                api = 'OpenAI' if self.valves.USE_OPENAI_API_RESPONDING_MODEL else 'Ollama'
                category = 'Responding'
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": f"{category} Error: ensure {model} is a valid model option in the {api} api {e} ",
                        "done": True,
                    },
                }
            )

        finally:
            if response and hasattr(response, "close"):
                await response.close()

    async def run_thinking(
        self,
        k: int,
        n: int,
        model: str,
        messages: list,
        query: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> str:
        # Deep copy the messages to avoid changing the original
        messages = json.loads(json.dumps(messages))
        thinking_with = ""
        if n == 1:
            thinking_with = f"with {model}"
        else:
            thinking_with = f"with {model} {k}/{n}"
        await __event_emitter__(
            {"type": "status", "data": {"description": f"Thinking {thinking_with}", "done": False}}
        )

        prompt = "You are a reasoning model.\n"
        prompt += "Think carefully about the user's request and output your reasoning steps.\n"
        prompt += (
            "Do not answer the user directly, just produce a hidden reasoning chain.\n"
        )
        prompt += "First rephrase the user prompt, then answer using multiple thinking-path to give all possible answers.\n"
        prompt += f"User Query: {query}"

        # We will stream the reasoning steps. The reasoning prompt:
        thinking_messages = {
            "role": "user",
            "content": prompt,
        }
        # replace last message
        messages[-1] = thinking_messages

        reasoning = ""
        reasoning_tokens = 0
        thinking = True
        if self.valves.ENABLE_SHOW_THINKING_TRACE and n != 1:
            await __event_emitter__(
                {
                    "type": "message",
                    "data": {"content": "\n### Thinking with "+model+"\n", "role": "assistant-thinking"},
                }
            )
        async for chunk in self.stream_response(
            model.strip(), messages, thinking, __event_emitter__
        ):
            reasoning += chunk
            reasoning_tokens += 1
            if self.valves.ENABLE_SHOW_THINKING_TRACE:
                # Emit chunk as a "thinking" type message
                await __event_emitter__(
                    {
                        "type": "message",
                        "data": {"content": chunk, "role": "assistant-thinking"},
                    }
                )
            else:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": f"Thinking {thinking_with} ({reasoning_tokens} tokens)", "done": False}}
                    )

        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": f"Finished thinking {thinking_with}", "done": False},
            }
        )
        self.total_thinking_tokens += reasoning_tokens
        await asyncio.sleep(0.2)
        return reasoning.strip()

    async def run_responding(
        self,
        messages: list,
        query: str,
        reasonings: list,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        await __event_emitter__(
            {
                "type": "status",
                "data": {"description": "Formulating response...", "done": False},
            }
        )
        prompt = "Here is some internal reasoning to guide your response:\n"
        prompt += f"<reasoning>{reasonings[0]}<reasoning-end>\n"
        for reasoning in reasonings[1:]:
            prompt = "Here is some other internal reasoning to guide your response:\n"
            prompt += f"<reasoning>{reasoning}<reasoning-end>\n"
        prompt += f"Use this reasoning to respond in concise and helpful manner to the user's query: {query}"

        responding_messages = {
            "role": "user",
            "content": prompt
        }
        # replace last message
        messages[-1] = responding_messages

        response_text = "\n\n### Response:\n"
        await __event_emitter__(
            {"type": "message", "data": {"content": response_text, "role": "assistant"}}
        )

        thinking = False
        async for chunk in self.stream_response(
            self.valves.RESPONDING_MODEL.strip(), messages, thinking, __event_emitter__
        ):
            response_text += chunk
            # Emit response chunks as assistant message
            await __event_emitter__(
                {"type": "message", "data": {"content": chunk, "role": "assistant"}}
            )

        await asyncio.sleep(0.2)

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __task__=None,
    ) -> str:

        # Get relavant info
        self.__user__ = User(**__user__)
        messages = body["messages"]
        query = get_last_user_message(messages)

        if (
            __task__ == None
        ):  # only perform thinking when not a defined task like title generation
            # Run the "thinking" step
            # Clone the messages to avoid changing the original
            tik = time()
            models = self.valves.THINKING_MODEL.split(",")
            reasonings = [await self.run_thinking(model + 1, len(models), models[model], messages, query, __event_emitter__) for model in range(len(models))]
            total_thought_duration = int(time() - tik)

            # Run the "responding" step using the reasoning
            await self.run_responding(messages, query, reasonings, __event_emitter__)

            if self.max_thinking_time_reached:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Thought for {self.total_thinking_tokens} tokens in max allowed time of {total_thought_duration} seconds",
                            "done": True,
                        },
                    }
                )
            else:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"Thought for only {self.total_thinking_tokens} tokens in {total_thought_duration} seconds",
                            "done": True,
                        },
                    }
                )
            return ""
        else:
            # avoid thinking and just return a regular response or named task, like tags
            return await self.get_completion(
                self.valves.RESPONDING_MODEL.strip(), messages, __event_emitter__
            )
