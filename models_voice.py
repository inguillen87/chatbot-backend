from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Boolean
from database import db

class VoiceCall(db.Model):
    """Represents an inbound/outbound voice call session (WebRTC or PSTN/Twilio)."""
    __tablename__ = "voice_calls"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    session_id = Column(String(100), unique=True, nullable=False, index=True) # WebRTC ID or Twilio CallSid
    channel = Column(String(50), nullable=False) # 'webrtc', 'twilio_pstn'

    actor_id = Column(String(100), nullable=True) # Could be user ID or Anon ID
    caller_phone = Column(String(50), nullable=True) # For PSTN calls

    status = Column(String(50), default="in_progress") # 'in_progress', 'completed', 'failed', 'transferred'
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime, nullable=True)

    transcript = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    quality_score = Column(Integer, nullable=True) # 1-100 score from Call QA
    needs_review = Column(Boolean, default=False)

class VoiceTurn(db.Model):
    """Represents a single conversational turn in a Voice Call."""
    __tablename__ = "voice_turns"
    id = Column(Integer, primary_key=True)
    call_id = Column(Integer, ForeignKey("voice_calls.id"), nullable=False)

    speaker = Column(String(50), nullable=False) # 'user', 'agent'
    text = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class VoiceHandoff(db.Model):
    """Represents a transfer from the AI Voice Agent to a Human."""
    __tablename__ = "voice_handoffs"
    id = Column(Integer, primary_key=True)
    call_id = Column(Integer, ForeignKey("voice_calls.id"), nullable=False)

    reason = Column(String(200), nullable=True)
    target_agent_id = Column(Integer, ForeignKey("user.id"), nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    successful = Column(Boolean, default=True)
