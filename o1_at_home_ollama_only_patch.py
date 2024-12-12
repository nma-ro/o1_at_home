"""
title: Think-Respond Chain Pipe, o1 at home
This version addresses this issue : https://github.com/latent-variable/o1_at_home/issues/4
author: latent-variable
github: https://github.com/latent-variable/o1_at_home
open-webui: https://openwebui.com/f/latentvariable/o1_at_home/
Blog post: https://o1-at-home.hashnode.dev/run-o1-at-home-privately-think-respond-pipe-tutorial-with-open-webui-ollama
version: 0.3.4
Descrition: Think-Respond pipe that has an internal reasoning steps and another for producing a final response based on the reasoning.
            Now supports openAI api along with ollama, you can mix and match models 

Instructions: 
To use the o1 at home pipe, follow these steps:

Add the Pipe Manifold:
Navigate to the Admin Panel and add the pipe to the list of available "Functions" using the '+'.
This is not a "pipeline", Ensure you are using Function tab. 
If you are copying the code you might need to give it name and descriprition 

Enable the Pipe Manifold:
After adding it, enable the pipe to make it active.

Customize Settings:
Use the configuration menu (accessed via the settings cog) to tailor the pipeline to your needs:
    Select Models: Choose your desired thinking models and response model.
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
import aiohttp

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
        THINKING_MODEL_API_URL: str = Field(
            default="http://host.docker.internal:11434/api/chat",
            description="Defualt API Ollama http://host.docker.internal:11434/api/chat or http://localhost:11434/api/chat",
        )
        THINKING_MODEL_API_KEY: Optional[str] = Field(
            default=None,
            description="API key for the thinking model, if required.",
        )
        RESPONDING_MODEL: str = Field(
            default="your_responding_model_id_here",
            description="Model used for producing the final response.",
        )
        RESPONDING_MODEL_API_URL: str = Field(
            default="http://host.docker.internal:11434/api/chat",
            description="Defualt API Ollama http://host.docker.internal:11434/api/chat or http://localhost:11434/api/chat",
        )
        RESPONDING_MODEL_API_KEY: Optional[str] = Field(
            default=None,
            description="API key for the responding model, if required.",
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

    def get_chunk_content(self, chunk_data)->str:
        """
        Process a chunk of data from the API stream.

        Args:
            chunk (bytes): The raw byte content received from the API stream.

        Yields:
            str: The extracted content from the chunk, if available.
        """
        try:
            if "message" in chunk_data:
                delta = chunk_data["message"]
                content = delta.get("content", "")
                if content:  # Only yield non-empty content
                    return content
            return ""
        except json.JSONDecodeError as e:
            logger.error(f'ChunkDecodeError: unable to parse "{str(chunk_data)[:100]}": {e}')
            return ""

    async def send_api_request(self, api_url: str, payload: dict, api_key: Optional[str] = None, thinking: bool = False) -> AsyncGenerator[str, None]:
        """
        Send a POST request to the specified API URL with the given payload and optional API key.

        Args:
            api_url (str): The API base URL.
            payload (dict): The payload to send in the request.
            api_key (Optional[str]): The API key for authentication, if required.

        Returns:
            dict: The response from the API.
        """
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        start_thought_time = time()
        async with aiohttp.ClientSession() as session:
            try:
                print(f"api_url: {api_url}, payload: {payload}, headers: {headers}")
                async with session.post(api_url, json=payload, headers=headers) as response:
                    response.raise_for_status()
                    # Check the Content-Type of the response
                    async for line in response.content:
                        pline = self.get_chunk_content(json.loads(line.decode('utf-8')))
                        
                        yield pline

                        if thinking:
                            current_time = (
                                time()
                            )

                            if (
                                current_time - start_thought_time
                            ) > self.valves.MAX_THINKING_TIME:
                                logger.info(
                                    f'Max thinking Time reached in stream_response of thinking model "'
                                )
                                self.max_thinking_time_reached = True
                                break
                    
            except Exception as e:
                logger.error(f"API Request Error: {e}")
                raise e


    def get_response(
        self,
        model: str,
        api_url: str,
        api_key: Optional[str],
        messages: List[Dict[str, str]],
        thinking: bool,
        stream: bool,
    ):
        """
        Generate a response from the specified API.

        Args:
            model (str): The model ID.
            api_url (str): The API base URL.
            api_key (Optional[str]): The API key for the API.
            messages (List[Dict[str, str]]): The input messages.
            stream (bool): Whether the response should be streamed.

        Returns:
            aiohttp.ClientResponse or dict: The response object or JSON response.
        """
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        return self.send_api_request(api_url, payload, api_key, thinking)

    async def stream_response(
        self,
        model: str,
        api_url: str,
        api_key: Optional[str],
        messages: List[Dict[str, str]],
        thinking: bool,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream response from the specified API.

        Args:
            model (str): The model ID.
            api_url (str): The API base URL.
            api_key (Optional[str]): The API key for the API.
            messages (List[Dict[str, str]]): The input messages.

        Yields:
            str: Chunks of response content.
        """
        try:
            async for part in self.get_response(model, api_url, api_key, messages, thinking, stream=True):
                yield part
        except Exception as e:
            logger.error(f"Stream Response Error: {e}")
            raise e

    async def run_thinking(
        self,
        k: int,
        n: int,
        model: str,
        api_url: str,
        api_key: Optional[str],
        messages: list,
        query: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> str:
        """
        Run the thinking step using the specified model, API, and API key.

        Args:
            model (str): The model ID.
            api_url (str): The API base URL.
            api_key (Optional[str]): The API key for the API.
            messages (list): The input messages.
            query (str): The user query.

        Returns:
            str: The reasoning result.
        """
        thinking_with = f"with {model} {k}/{n}" if n > 1 else f"with {model}"
        await __event_emitter__(
            {"type": "status", "data": {"description": f"Thinking {thinking_with}", "done": False}}
        )

        prompt = (
            "You are a reasoning model.\n"
            "Think carefully about the user's request and output your reasoning steps.\n"
            "Do not answer the user directly, just produce a hidden reasoning chain.\n"
            "First rephrase the user prompt, then answer using multiple thinking paths to give all possible answers.\n"
            f"User Query: {query}"
        )

        messages[-1] = {"role": "user", "content": prompt}
        reasoning = ""
        reasoning_tokens = 0
        thinking = True
        async for chunk in self.stream_response(model, api_url, api_key, messages, thinking, __event_emitter__):
            reasoning += chunk
            reasoning_tokens += 1

            await __event_emitter__(
                {
                    "type": "status",
                    "data": {"description": f"Thinking {thinking_with} ({reasoning_tokens} tokens)", "done": False},
                }
            )
        self.total_thinking_tokens += reasoning_tokens
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
            prompt += "Here is some other internal reasoning to guide your response:\n"
            prompt += f"<reasoning>{reasoning}<reasoning-end>\n"
        prompt += f"Use this reasoning to respond in a concise and helpful manner to the user's query: {query}"

        responding_messages = {
            "role": "user",
            "content": prompt
        }
        # Replace the last message
        messages[-1] = responding_messages

        response_text = "\n\n### Response:\n"
        await __event_emitter__(
            {"type": "message", "data": {"content": response_text, "role": "assistant"}}
        )

        thinking = False
        api_url = self.valves.RESPONDING_MODEL_API_URL
        api_key = self.valves.RESPONDING_MODEL_API_KEY

        async for chunk in self.stream_response(
            self.valves.RESPONDING_MODEL.strip(), api_url, api_key, messages, thinking, __event_emitter__
        ):
            response_text += chunk
            # Emit response chunks as assistant message
            await __event_emitter__(
                {"type": "message", "data": {"content": chunk, "role": "assistant"}}
            )

        await asyncio.sleep(0.2)
        return {"response": response_text.strip()}

    async def pipe(
            self,
            body: dict,
            __user__: dict,
            __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
            __task__=None,
        ) -> str:

            # Get relevant info
            self.__user__ = User(**__user__)
            messages = body["messages"]
            query = get_last_user_message(messages)

            print("Task:", __task__)

            if __task__ is None:  # Perform thinking and responding
                # Run the "thinking" step
                tik = time()
                models = self.valves.THINKING_MODEL.split(",")
                api_url = self.valves.THINKING_MODEL_API_URL
                api_key = self.valves.THINKING_MODEL_API_KEY

                reasonings = [
                    await self.run_thinking(
                        k=model_index + 1,
                        n=len(models),
                        model=models[model_index].strip(),
                        api_url=api_url,
                        api_key=api_key,
                        messages=messages,
                        query=query,
                        __event_emitter__=__event_emitter__,
                    )
                    for model_index in range(len(models))
                ]

                total_thought_duration = int(time() - tik)

                # Run the "responding" step using the reasoning
                await self.run_responding(messages, query, reasonings, __event_emitter__)

                if self.max_thinking_time_reached:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": f"Thought for {self.total_thinking_tokens} tokens in the max allowed time of {total_thought_duration} seconds",
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
                # Skip thinking and handle specific tasks, like tags or title generation
                api_url = self.valves.RESPONDING_MODEL_API_URL
                api_key = self.valves.RESPONDING_MODEL_API_KEY

                return await self.get_completion(
                    self.valves.RESPONDING_MODEL.strip(), api_url, api_key, messages, __event_emitter__
                )