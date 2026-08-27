"""
Logs Router - Real-time server logs API
"""
from fastapi import APIRouter
from typing import List, Dict
import logging
import time
from collections import deque

router = APIRouter(prefix="/api/logs", tags=["Logs"])

# In-memory log buffer (last 500 entries)
log_buffer = deque(maxlen=500)


class BufferHandler(logging.Handler):
    """Custom handler that stores logs in memory"""
    
    def emit(self, record):
        try:
            log_entry = {
                "timestamp": record.created,
                "time": self.format_time(record.created),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            log_buffer.append(log_entry)
        except Exception:
            self.handleError(record)
    
    @staticmethod
    def format_time(timestamp: float) -> str:
        """Format timestamp as HH:MM:SS"""
        return time.strftime("%H:%M:%S", time.localtime(timestamp))


# Install the buffer handler
buffer_handler = BufferHandler()
buffer_handler.setLevel(logging.DEBUG)

# Add to root logger to capture all logs
root_logger = logging.getLogger()
root_logger.addHandler(buffer_handler)

# Also add to specific loggers
for logger_name in ["src", "uvicorn", "fastapi"]:
    logger = logging.getLogger(logger_name)
    logger.addHandler(buffer_handler)


@router.get("/recent", response_model=List[Dict])
async def get_recent_logs(limit: int = 100, level: str = None):
    """
    Get recent log entries
    
    Args:
        limit: Maximum number of log entries to return (default: 100)
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    
    Returns:
        List of log entries
    """
    logs = list(log_buffer)
    
    # Filter by level if specified
    if level:
        level_upper = level.upper()
        logs = [log for log in logs if log["level"] == level_upper]
    
    # Return most recent entries
    return logs[-limit:]


@router.get("/stream", response_model=List[Dict])
async def stream_logs(since: float = 0, level: str = None):
    """
    Get logs since a specific timestamp (for polling)
    
    Args:
        since: Unix timestamp - only return logs after this time
        level: Filter by log level
    
    Returns:
        List of new log entries
    """
    logs = list(log_buffer)
    
    # Filter by timestamp
    if since > 0:
        logs = [log for log in logs if log["timestamp"] > since]
    
    # Filter by level if specified
    if level:
        level_upper = level.upper()
        logs = [log for log in logs if log["level"] == level_upper]
    
    return logs


@router.delete("/clear")
async def clear_logs():
    """Clear all log entries from buffer"""
    log_buffer.clear()
    return {"message": "Log buffer cleared", "success": True}


@router.get("/stats")
async def get_log_stats():
    """Get statistics about logs"""
    logs = list(log_buffer)
    
    level_counts = {}
    for log in logs:
        level = log["level"]
        level_counts[level] = level_counts.get(level, 0) + 1
    
    return {
        "total_entries": len(logs),
        "by_level": level_counts,
        "buffer_size": log_buffer.maxlen,
        "oldest_timestamp": logs[0]["timestamp"] if logs else None,
        "newest_timestamp": logs[-1]["timestamp"] if logs else None,
    }
