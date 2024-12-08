"""
title: Think-Respond Chain Pipe, o1 at home
author: latent-variable
github: https://github.com/latent-variable/o1_at_home
version: 0.2.1
Descrition: Think-Respond pipeline that has an internal reasoning steps and another for producing a final response based on the reasoning.

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
        RESPONDING_MODEL: str = Field(
            default="your_responding_model_id_here",
            description="Model used for producing the final response.",
        )
        ENABLE_EMITTERS: bool = Field(
            default=True,
            description="Toggle event emitters.",
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
        chunk_str = chunk.decode("utf-8").strip()
        if chunk_str.startswith("data: "):
            chunk_str = chunk_str[6:]

        if not chunk_str or chunk_str == "[DONE]":
            return

        try:
            chunk_data = json.loads(chunk_str)
            if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                delta = chunk_data["choices"][0].get("delta", {})
                if "content" in delta:
                    yield delta["content"]
        except json.JSONDecodeError as e:
            logger.error(f'ChunkDecodeError: unable to parse "{chunk_str[:100]}": {e}')


    async def get_completion(self, model: str, 
                             messages:list,
                             __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,):
        response = None
        try:
            response = await ollama.generate_openai_chat_completion(
                {"model": model, "messages": messages, "stream": False}, user=self.__user__)
            return response["choices"][0]["message"]["content"]
        except Exception as e:
            print('*********', e)
            await __event_emitter__({ "type": "status","data": {"description": f"Error: ensure {model} is a valid ollama option {e}", "done": True}})
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
            response = await  ollama.generate_openai_chat_completion(
                {"model": model, "messages": messages, "stream": True}, user=self.__user__)
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
            await __event_emitter__({ "type": "status","data": {"description": f"Error: ensure {model} is a valid ollama option {e}", "done": True}})
            
        finally:
            if response and hasattr(response, 'close'):
                await response.close()


    async def run_thinking(
        self,
        messages: list,
        query: str,
        __event_emitter__: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> str:
        if self.valves.ENABLE_EMITTERS:
            await __event_emitter__({ "type": "status","data": {"description": "Thinking...", "done": False}})

        # We will stream the reasoning steps. The reasoning prompt:
        thinking_messages = {
            "role": "user",
            "content": f"You are a reasoning model. Think carefully about the user's request and output your reasoning steps. Do not answer the user directly, just produce a hidden reasoning chain. User Query: {query}"
        }
        # replace last message 
        messages[-1] = thinking_messages
       
        reasoning = ""
        thinking = True
        async for chunk in self.stream_response( self.valves.THINKING_MODEL, messages, thinking, __event_emitter__):
            reasoning += chunk 
            if self.valves.ENABLE_SHOW_THINKING_TRACE:
                # Emit chunk as a "thinking" type message
                await __event_emitter__({ "type": "message","data": {"content": chunk, "role": "assistant-thinking"}})

        if self.valves.ENABLE_EMITTERS:
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
        if self.valves.ENABLE_EMITTERS:
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
        async for chunk in self.stream_response( self.valves.RESPONDING_MODEL, messages,thinking,  __event_emitter__ ):
            response_text += chunk
            if self.valves.ENABLE_EMITTERS:
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

            if self.valves.ENABLE_EMITTERS:
                if self.max_thinking_time_reached:
                    await __event_emitter__({ "type": "status", "data": {"description": f"Thought for max allowed time of {thought_duration} seconds", "done": True} })
                else:
                    await __event_emitter__({ "type": "status", "data": {"description": f"Thought for only {thought_duration} seconds", "done": True} })
            return ""
        else:
            # avoid thinking and just return a regular response or named task, like tags 
            return await self.get_completion(self.valves.RESPONDING_MODEL, messages, __event_emitter__)
