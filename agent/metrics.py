"""
Prometheus metrics for the voice agent.

All metric objects are module-level singletons — they accumulate across calls
and are scraped by Prometheus on demand via a background HTTP server.

Call start_metrics_server() once at process startup (in server.py main()).
"""

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# Latency buckets covering the realistic range for voice pipeline stages (seconds)
_LATENCY_BUCKETS = [0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, float("inf")]

# Call duration buckets (seconds)
_DURATION_BUCKETS = [5, 15, 30, 60, 120, 300, 600, float("inf")]

# ── Per-stage latency histograms ──────────────────────────────────────────────

stt_ttfb = Histogram(
    "voiceai_stt_ttfb_seconds",
    "STT time-to-first-byte (seconds)",
    labelnames=["agent_slug", "provider"],
    buckets=_LATENCY_BUCKETS,
)

llm_ttfb = Histogram(
    "voiceai_llm_ttfb_seconds",
    "LLM time-to-first-token (seconds)",
    labelnames=["agent_slug", "provider", "model"],
    buckets=_LATENCY_BUCKETS,
)

tts_ttfb = Histogram(
    "voiceai_tts_ttfb_seconds",
    "TTS time-to-first-audio (seconds)",
    labelnames=["agent_slug", "provider"],
    buckets=_LATENCY_BUCKETS,
)

text_agg_latency = Histogram(
    "voiceai_text_agg_seconds",
    "Time from first LLM token to first complete sentence (seconds)",
    labelnames=["agent_slug"],
    buckets=_LATENCY_BUCKETS,
)

turn_e2e = Histogram(
    "voiceai_turn_e2e_seconds",
    "End-to-end turn latency from user speech end to first bot audio (seconds)",
    labelnames=["agent_slug"],
    buckets=_LATENCY_BUCKETS,
)

# ── Usage counters ────────────────────────────────────────────────────────────

llm_tokens_total = Counter(
    "voiceai_llm_tokens_total",
    "Cumulative LLM tokens",
    labelnames=["agent_slug", "token_type"],  # token_type: prompt | completion
)

tts_chars_total = Counter(
    "voiceai_tts_chars_total",
    "Cumulative TTS characters processed",
    labelnames=["agent_slug"],
)

# ── Call-level counters and gauges ────────────────────────────────────────────

calls_active = Gauge(
    "voiceai_calls_active",
    "Number of calls currently in progress",
    labelnames=["agent_slug"],
)

calls_total = Counter(
    "voiceai_calls_total",
    "Total calls completed",
    labelnames=["agent_slug", "end_reason"],
)

call_duration = Histogram(
    "voiceai_call_duration_seconds",
    "Total call duration (seconds)",
    labelnames=["agent_slug"],
    buckets=_DURATION_BUCKETS,
)


def start_metrics_server(port: int = 9090) -> None:
    """
    Start the Prometheus metrics HTTP server on a background thread.
    Safe to call from an asyncio main loop — prometheus_client uses threading internally.
    """
    start_http_server(port)
