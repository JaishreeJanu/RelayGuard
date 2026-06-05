import asyncio
import logging
from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MockVendor")

app = FastAPI(title="Mock Email API Provider (SendGrid/Twilio Simulator)")

# =============================================================================
# 🔒 ENABLE CORS FOR FRONTEND VISIBILITY
# =============================================================================
# Allow your React application on port 5173 to dynamically alter the vendor state
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state to control our chaos testing scenarios
# Options: "HEALTHY", "ERROR_500", "LATENCY_STORM"
CURRENT_STATE = "HEALTHY"


class ChaosConfig(BaseModel):
    state: str  # "HEALTHY", "ERROR_500", or "LATENCY_STORM"


@app.post("/chaos/state", tags=["Chaos Engineering"])
async def set_chaos_state(config: ChaosConfig):
    """Endpoint that your frontend/tests will call to dynamically change vendor behavior."""
    global CURRENT_STATE
    allowed_states = ["HEALTHY", "ERROR_500", "LATENCY_STORM"]
    
    if config.state.upper() not in allowed_states:
        return {"error": f"Invalid state. Choose from {allowed_states}"}
        
    CURRENT_STATE = config.state.upper()
    logger.warning(f"🚨 CHAOS MODE UPDATED: Mock Vendor is now in {CURRENT_STATE} mode.")
    return {"message": f"Mock vendor state changed to {CURRENT_STATE}"}


@app.get("/chaos/state", tags=["Chaos Engineering"])
async def get_chaos_state():
    return {"current_state": CURRENT_STATE}


@app.post("/mock/send", tags=["Delivery"])
async def simulate_send(payload: dict, response: Response):
    """Simulates an outbound third-party email delivery API endpoint."""
    global CURRENT_STATE
    
    logger.info(f"Received outbound payload for processing: {payload.get('recipient')}")

    # Scenario A: Everything is working perfectly
    if CURRENT_STATE == "HEALTHY":
        return {"status": "success", "message": "Email dispatched to upstream network."}

    # Scenario B: Vendor suffers a critical service failure
    elif CURRENT_STATE == "ERROR_500":
        response.status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return {"status": "error", "message": "Internal Server Error. Service Unavailable."}

    # Scenario C: Vendor encounters major connection lag (will trigger timeouts)
    elif CURRENT_STATE == "LATENCY_STORM":
        logger.warning("Simulating latency storm. Freezing connection response for 10 seconds...")
        await asyncio.sleep(10.0) # This will force your worker's 5.0s httpx timeout to trigger!
        return {"status": "success", "message": "Delayed success after lag."}