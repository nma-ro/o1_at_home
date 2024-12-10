"""
title: Think-Respond Chain Pipe, o1 at home
author: latent-variable
github: https://github.com/latent-variable/o1_at_home
version: 0.3.1
Descrition: Think-Respond pipeline that has an internal reasoning steps and another for producing a final response based on the reasoning.
            Now supports openAI api along with ollama, you can mix and match models 

Instructions: 
To use the o1 at home pipeline, follow these steps:

Add the Pipe Manifold:
Navigate to the Admin Panel and add the pipeline to the list of available "Functions" using the '+'.
This is not a "pipeline", Ensure you are using Function tab. 
If you are copying the code you might need ot give it name and descriprition 

Enable the Pipe Manifold:
After adding it, enable the pipeline to make it active.

Customize Settings:
Use the configuration menu (accessed via the settings cog) to tailor the pipeline to your needs:
    Select Models: Choose your desired thinking model and response model.
    Show Reasoning: Decide whether to display the reasoning process or keep it hidden.
    Set Thinking Time: Specify the maximum time allowed for the reasoning model to process.
Save and Apply:
Once configured, save your settings to apply the changes.
You should now have o1 at home in your dorp down. 

These steps ensure the pipeline is set up correctly and functions according to your requirements.
"""

import json
from time import time
from pydantic import BaseModel, Field
from dataclasses import dataclass
from typing import (
    Dict,
    List,
    Optional,
    Callable,
    Awaitable,
    Any,
    AsyncGenerator
)
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
            description="Model used for the internal reasoning step.",
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
            description="Maximum time in seconds the thinking model can run.",
        )

    def __init__(self):
        self.type = "manifold"
        self.valves = self.Valves()
        self.start_thought_time = None
        self.max_thinking_time_reached = False
        self.__user__ = None

    
    def pipes(self):
        return [{"name": "o1 at home", "id": "o1_at_home"}]

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

    async def get_response(self, model: str, messages: List[Dict[str, str]], thinking: bool, stream: bool ):
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
            self.valves.USE_OPENAI_API_THINKING_MODEL if thinking 
            else self.valves.USE_OPENAI_API_RESPONDING_MODEL
        )

        # Select the appropriate API and identify the source
        if use_openai_api:
            generate_completion = openai.generate_chat_completion
        else:
            generate_completion = ollama.generate_openai_chat_completion


        # Generate response
        response = await generate_completion({"model": model, "messages": messages, "stream": stream}, user=self.__user__)

        return response
    
    async def get_completion(self, model: str, 
                             messages:list,
                             __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,):
        response = None
        try:
            thinking = False
            stream = False
            response = await self.get_response(model, messages, thinking, stream)
            return response["choices"][0]["message"]["content"]
        except Exception as e:
            await __event_emitter__({ "type": "status","data": {"description": f"Error: ensure {model} is a valid model option {e}", "done": True}})
        finally:
            if response and hasattr(response, 'close'):
                await response.close()


    async def stream_response(
        self,
        model: str,
        messages: List[Dict[str, str]],
        thinking:bool,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> AsyncGenerator[str, None]:
        
        try:
            stream = True
            response = await self.get_response(model, messages, thinking, stream)
            while True:
                chunk = await response.body_iterator.read(1024)
                if not chunk: # No more data
                    break
                for part in self.get_chunk_content(chunk):
                    yield part

                if thinking:
                    current_time = time() #check to see if thought time has been exceded 
                    if (current_time - self.start_thought_time) > self.valves.MAX_THINKING_TIME:
                        logger.info(f'Max thinking Time reached in stream_response of thinking model "')
                        self.max_thinking_time_reached = True
                        break

        except Exception as e:
            print('*********', e)
            await __event_emitter__({ "type": "status","data": {"description": f"Error: ensure {model} is a valid model option {e}", "done": True}})
            
        finally:
            if response and hasattr(response, 'close'):
                await response.close()


    async def run_thinking(
        self,
        messages: list,
        query: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> str:
        await __event_emitter__({ "type": "status","data": {"description": "Thinking...", "done": False}})

        # We will stream the reasoning steps. The reasoning prompt:
        thinking_messages = {
            "role": "user",
            "content": f"You are a reasoning model.\nThink carefully about the user's request and output your reasoning steps.\nDo not answer the user directly, just produce a hidden reasoning chain.\nUser Query: {query}"
        }
        # replace last message 
        messages[-1] = thinking_messages
       
        reasoning = ""
        thinking = True
        async for chunk in self.stream_response( self.valves.THINKING_MODEL.strip(), messages, thinking, __event_emitter__):
            reasoning += chunk 
            if self.valves.ENABLE_SHOW_THINKING_TRACE:
                # Emit chunk as a "thinking" type message
                await __event_emitter__({ "type": "message","data": {"content": chunk, "role": "assistant-thinking"}})

        
        await __event_emitter__({"type": "status","data": {"description": "Finished thinking.", "done": False}})
        await asyncio.sleep(0.2)
        return reasoning.strip()

    async def run_responding(
        self,
        messages: list,
        query: str,
        reasoning: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None
    ) -> Dict[str, Any]:
        await __event_emitter__({"type": "status","data": {"description": "Formulating response...", "done": False}})

        responding_messages = {
            "role": "user",
            "content": f"Here is some internal reasoning to guide your response:\n<reasoning>{reasoning}<reasoning-end>\nUse this reasoning to respond in concise and helpful manner to the user's query: {query}"
        }
        # replace last message 
        messages[-1] = responding_messages

        response_text = "\n\n### Response:\n"
        await __event_emitter__({ "type": "message", "data": {"content": response_text, "role": "assistant"}})

        thinking=False
        async for chunk in self.stream_response( self.valves.RESPONDING_MODEL.strip(), messages,thinking,  __event_emitter__ ):
            response_text += chunk
            # Emit response chunks as assistant message
            await __event_emitter__({ "type": "message", "data": {"content": chunk, "role": "assistant"}})

        await asyncio.sleep(0.2)
    
    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
        __task__=None,
    ) -> str:
        
        # Get relavant info
        self.start_thought_time = time()
        self.__user__ = User(**__user__)
        messages = body["messages"]
        query = get_last_user_message(messages)
        
        if __task__ == None: # only perform thinking when not a defined task like title generation
            # Run the "thinking" step 
            reasoning = await self.run_thinking(messages, query, __event_emitter__)
            thought_duration = int(time() - self.start_thought_time)

            # Run the "responding" step using the reasoning 
            await self.run_responding(messages, query, reasoning, __event_emitter__)

            if self.max_thinking_time_reached:
                await __event_emitter__({ "type": "status", "data": {"description": f"Thought for max allowed time of {thought_duration} seconds", "done": True} })
            else:
                await __event_emitter__({ "type": "status", "data": {"description": f"Thought for only {thought_duration} seconds", "done": True} })
            return ""
        else:
            # avoid thinking and just return a regular response or named task, like tags 
            return await self.get_completion(self.valves.RESPONDING_MODEL.strip(), messages, __event_emitter__)
