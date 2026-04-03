#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Shared Pipecat bot logic for the local multi-transport server."""

import os
from typing import Literal

from dotenv import load_dotenv
from loguru import logger

logger.info("Loading Silero VAD model...")
from pipecat.audio.vad.silero import SileroVADAnalyzer

logger.info("Silero VAD model loaded")

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    LLMRunFrame,
    OutputTransportMessageFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport

logger.info("All components loaded successfully")

load_dotenv(override=True)


class Esp32ControlMessageProcessor(FrameProcessor):
    """Translate pipeline state changes into the ESP32's existing JSON control protocol."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, (UserStoppedSpeakingFrame, VADUserStoppedSpeakingFrame)):
                await self.push_frame(
                    OutputTransportMessageFrame(message={"type": "server", "msg": "AUDIO.COMMITTED"}),
                    direction,
                )
            elif isinstance(frame, BotStartedSpeakingFrame):
                await self.push_frame(
                    OutputTransportMessageFrame(message={"type": "server", "msg": "RESPONSE.CREATED"}),
                    direction,
                )
            elif isinstance(frame, BotStoppedSpeakingFrame):
                await self.push_frame(frame, direction)
                await self.push_frame(
                    OutputTransportMessageFrame(message={"type": "server", "msg": "RESPONSE.COMPLETE"}),
                    direction,
                )
                return
            elif isinstance(frame, ErrorFrame):
                await self.push_frame(
                    OutputTransportMessageFrame(message={"type": "server", "msg": "RESPONSE.ERROR"}),
                    direction,
                )

        await self.push_frame(frame, direction)


def create_esp32_auth_message() -> dict:
    return {
        "type": "auth",
        "volume_control": int(os.getenv("ESP32_DEFAULT_VOLUME", "70")),
        "pitch_factor": float(os.getenv("ESP32_DEFAULT_PITCH_FACTOR", "1.0")),
        "is_ota": False,
        "is_reset": False,
    }


async def run_bot_session(
    transport: BaseTransport,
    transport_kind: Literal["browser", "esp32"],
    handle_sigint: bool = False,
):
    logger.info(f"Starting bot session for {transport_kind}")

    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice="71a7ad14-091c-4e8e-a314-022ece01c121",
        ),
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        settings=OpenAILLMService.Settings(
            system_instruction=(
                "You are a friendly AI assistant. Respond naturally and keep your "
                "answers conversational."
            ),
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    processors = [
        transport.input(),
        stt,
        user_aggregator,
        llm,
        tts,
    ]

    if transport_kind == "esp32":
        processors.append(Esp32ControlMessageProcessor())

    processors.extend(
        [
            transport.output(),
            assistant_aggregator,
        ]
    )

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=24000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"{transport_kind} client connected")
        context.add_message(
            {
                "role": "developer",
                "content": "Say hello and briefly introduce yourself.",
            }
        )
        frames = [LLMRunFrame()]
        if transport_kind == "esp32":
            frames.insert(
                0,
                OutputTransportMessageFrame(
                    message={
                        "type": "server",
                        "msg": "SESSION.CONNECTED",
                    }
                ),
            )
        await task.queue_frames(frames)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"{transport_kind} client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)
