"""
Prometheus Metrics

Provides metrics collection and endpoint for monitoring.
"""

from prometheus_client import Counter, Histogram, Gauge, generate_latest
from fastapi import Response


# Request metrics
REQUEST_COUNT = Counter(
    'app_requests_total',
    'Total number of requests',
    ['method', 'endpoint', 'status']
)

REQUEST_LATENCY = Histogram(
    'app_request_latency_seconds',
    'Request latency in seconds',
    ['endpoint']
)

# Bot metrics
ACTIVE_BOTS = Gauge(
    'app_active_bots',
    'Number of currently active bots'
)

BOT_MESSAGES_SENT = Counter(
    'app_bot_messages_sent_total',
    'Total messages sent by bots',
    ['bot_id']
)

BOT_MESSAGES_RECEIVED = Counter(
    'app_bot_messages_received_total',
    'Total messages received by bots',
    ['bot_id']
)

# AI provider metrics
AI_REQUESTS = Counter(
    'app_ai_requests_total',
    'Total AI provider requests',
    ['provider', 'status']
)

AI_TOKENS_USED = Counter(
    'app_ai_tokens_total',
    'Total tokens used by AI providers',
    ['provider', 'type']
)

AI_REQUEST_LATENCY = Histogram(
    'app_ai_request_latency_seconds',
    'AI request latency in seconds',
    ['provider']
)

# Database metrics
DB_CONNECTIONS = Gauge(
    'app_db_connections',
    'Number of active database connections'
)

# Hub metrics
ACTIVE_HUBS = Gauge(
    'app_active_hubs',
    'Number of active hubs'
)

SCHEDULED_CONTENT_PENDING = Gauge(
    'app_scheduled_content_pending',
    'Number of pending scheduled content items'
)


def metrics_endpoint():
    """Return Prometheus metrics in text format."""
    return Response(
        content=generate_latest(),
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )
