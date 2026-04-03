# Pipecat Multi-Transport Bot

This workspace now exposes two access paths to the same Pipecat bot logic:

- Browser WebSocket test UI at `/browser`
- ESP32 WebSocket client at `/ws/esp32`
- Container entrypoint via `uv run server.py`

## What to do next

1. Copy the example environment file:

   ```bash
   cp env.example .env
   ```

2. Add your API keys to `.env`:

   - `DEEPGRAM_API_KEY`
   - `OPENAI_API_KEY`
   - `CARTESIA_API_KEY`

3. Install dependencies:

   ```bash
   uv sync
   ```

4. Run the local server:

   ```bash
   uv run server.py
   ```

5. Browser client:

   [http://localhost:7860/browser](http://localhost:7860/browser)

6. ESP32 client:

   `ws://localhost:7860/ws/esp32`

## Notes

- The first run can take around 20 seconds while Pipecat loads models and imports.
- The browser route uses plain WebSocket with raw PCM audio in/out.
- The ESP32 route accepts raw PCM audio input and returns Opus packets over WSS.
- The ESP32 route also emits JSON control messages such as `RESPONSE.CREATED`,
  `AUDIO.COMMITTED`, and `RESPONSE.COMPLETE`.
- The included [Dockerfile](/Users/akashdeepdeb/Desktop/Projects/pipecat-test/Dockerfile) is
  set up for generic container hosting of this FastAPI app.
