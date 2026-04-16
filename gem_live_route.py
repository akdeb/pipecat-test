"""Gemini Live native speech-to-speech pipeline builder."""

from __future__ import annotations

import os

from character_prompt import LANGUAGE_LEARNING_PAL_PROMPT
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class GeminiTurnBoundaryProcessor(FrameProcessor):
    """Bridge generic user turn frames into explicit Gemini VAD boundary frames."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction is FrameDirection.DOWNSTREAM:
            if isinstance(frame, UserStartedSpeakingFrame):
                await self.push_frame(VADUserStartedSpeakingFrame(), direction)
            elif isinstance(frame, UserStoppedSpeakingFrame):
                await self.push_frame(VADUserStoppedSpeakingFrame(), direction)

        await self.push_frame(frame, direction)


def build_gem_live_route(input_processor, context: LLMContext):
    try:
        from pipecat.services.google.gemini_live import GeminiLiveLLMService
        from pipecat.services.google.gemini_live.llm import GeminiVADParams
    except Exception as exc:
        raise RuntimeError(
            "Gemini Live route requires pipecat-ai[google]. Add the google extra and redeploy."
        ) from exc

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Gemini Live route requires GEMINI_API_KEY or GOOGLE_API_KEY.")

    voice = os.getenv("GEMINI_LIVE_VOICE", "Callirrhoe")
    model = os.getenv("GEMINI_LIVE_MODEL", "models/gemini-2.5-flash-native-audio-preview-12-2025")

    llm = GeminiLiveLLMService(
        api_key=api_key,
        inference_on_context_initialization=True,
        settings=GeminiLiveLLMService.Settings(
            model=model,
            voice=voice,
            system_instruction=LANGUAGE_LEARNING_PAL_PROMPT,
            vad=GeminiVADParams(disabled=True),
        ),
    )

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.6,
                    start_secs=0.1,
                    stop_secs=0.5,
                    min_volume=0.45,
                )
            )
        ),
    )
    turn_boundary_processor = GeminiTurnBoundaryProcessor()
    processors = [
        input_processor,
        user_aggregator,
        turn_boundary_processor,
        llm,
    ]

    return processors, assistant_aggregator
