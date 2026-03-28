"""
Tool Monitoring - Centralized execution logging for all tools
"""

import json
import time
from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from contextlib import contextmanager

from app.database import ToolExecution, AIAgent


class ToolMonitor:
    """
    Centralized monitoring for all tool operations.
    Logs executions to the tool_executions table for auditing and debugging.
    """

    TOOL_TYPES = [
        'group_management',
        'content_generator',
        'contact_analyzer',
        'scheduled_content',
        'message_routing',
        'agent_execution',
        'ai_workspace',
        'chathub_agent'
    ]

    @staticmethod
    def log_execution(
        db: Session,
        tool_type: str,
        operation: str,
        hub_id: Optional[int] = None,
        input_data: Optional[Dict[str, Any]] = None,
        output_data: Optional[Dict[str, Any]] = None,
        status: str = "success",
        error_message: Optional[str] = None,
        execution_time_ms: Optional[int] = None,
        tokens_used: Optional[int] = None,
        triggered_by: str = "user",
        related_entity_type: Optional[str] = None,
        related_entity_id: Optional[int] = None,
        user_id: Optional[int] = None
    ) -> ToolExecution:
        """
        Log a tool execution to the database.

        Args:
            db: Database session
            tool_type: Type of tool ('group_management', 'content_generator', etc.)
            operation: Specific operation performed (e.g., 'add_bot', 'generate_content')
            hub_id: Associated hub ID (if applicable)
            input_data: Input parameters as dict
            output_data: Output/result data as dict
            status: Execution status ('pending', 'running', 'success', 'error')
            error_message: Error message if status is 'error'
            execution_time_ms: Execution time in milliseconds
            tokens_used: Number of AI tokens used (for AI operations)
            triggered_by: What triggered this ('user', 'scheduled', 'api', 'agent')
            related_entity_type: Type of related entity ('contact', 'content', 'bot', 'agent')
            related_entity_id: ID of related entity
            user_id: User who triggered this operation

        Returns:
            ToolExecution: The created execution log record
        """
        execution = ToolExecution(
            hub_id=hub_id,
            tool_type=tool_type,
            operation=operation,
            input_data=json.dumps(input_data) if input_data else None,
            output_data=json.dumps(output_data) if output_data else None,
            status=status,
            error_message=error_message,
            execution_time_ms=execution_time_ms,
            tokens_used=tokens_used,
            triggered_by=triggered_by,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            user_id=user_id
        )
        db.add(execution)
        db.commit()
        db.refresh(execution)
        return execution

    @staticmethod
    @contextmanager
    def track_execution(
        db: Session,
        tool_type: str,
        operation: str,
        hub_id: Optional[int] = None,
        input_data: Optional[Dict[str, Any]] = None,
        triggered_by: str = "user",
        related_entity_type: Optional[str] = None,
        related_entity_id: Optional[int] = None,
        user_id: Optional[int] = None
    ):
        """
        Context manager for tracking tool execution with automatic timing.

        Usage:
            with ToolMonitor.track_execution(db, 'content_generator', 'generate') as tracker:
                result = do_something()
                tracker.set_output({'result': result})
                tracker.set_tokens(100)

        Args:
            Same as log_execution

        Yields:
            ExecutionTracker: Object to set output data and tokens
        """
        start_time = time.time()
        tracker = ExecutionTracker()

        try:
            yield tracker
            execution_time_ms = int((time.time() - start_time) * 1000)

            ToolMonitor.log_execution(
                db=db,
                tool_type=tool_type,
                operation=operation,
                hub_id=hub_id,
                input_data=input_data,
                output_data=tracker.output_data,
                status="success",
                execution_time_ms=execution_time_ms,
                tokens_used=tracker.tokens_used,
                triggered_by=triggered_by,
                related_entity_type=related_entity_type,
                related_entity_id=related_entity_id,
                user_id=user_id
            )
        except Exception as e:
            execution_time_ms = int((time.time() - start_time) * 1000)

            ToolMonitor.log_execution(
                db=db,
                tool_type=tool_type,
                operation=operation,
                hub_id=hub_id,
                input_data=input_data,
                output_data=tracker.output_data,
                status="error",
                error_message=str(e),
                execution_time_ms=execution_time_ms,
                tokens_used=tracker.tokens_used,
                triggered_by=triggered_by,
                related_entity_type=related_entity_type,
                related_entity_id=related_entity_id,
                user_id=user_id
            )
            raise

    @staticmethod
    def update_execution_response(
        db: Session,
        execution_id: int,
        response_message: str
    ) -> bool:
        """
        Update a tool execution record with the bot's response message.

        Args:
            db: Database session
            execution_id: The tool execution ID to update
            response_message: The bot's response message

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            execution = db.query(ToolExecution).filter(ToolExecution.id == execution_id).first()
            if execution and execution.output_data:
                output_data = json.loads(execution.output_data)
                output_data['response_message'] = response_message[:500] if response_message else None  # Limit to 500 chars
                execution.output_data = json.dumps(output_data)
                db.commit()
                return True
            return False
        except Exception as e:
            print(f"Failed to update execution response: {e}")
            return False

    @staticmethod
    def update_agent_stats(
        db: Session,
        agent_id: int,
        success: bool,
        tokens_used: int = 0
    ):
        """
        Update agent statistics after an execution.

        Args:
            db: Database session
            agent_id: Agent ID
            success: Whether execution was successful
            tokens_used: Number of tokens used
        """
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if agent:
            agent.total_executions = (agent.total_executions or 0) + 1
            if success:
                agent.successful_executions = (agent.successful_executions or 0) + 1
            agent.total_tokens_used = (agent.total_tokens_used or 0) + tokens_used
            agent.last_run_at = datetime.utcnow()
            agent.status = "idle" if success else "error"
            db.commit()

    @staticmethod
    def set_agent_status(db: Session, agent_id: int, status: str, error: Optional[str] = None):
        """
        Set agent status.

        Args:
            db: Database session
            agent_id: Agent ID
            status: Status ('idle', 'running', 'error')
            error: Error message if status is 'error'
        """
        agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
        if agent:
            agent.status = status
            if error:
                agent.last_error = error
            db.commit()

    @staticmethod
    def get_recent_executions(
        db: Session,
        tool_type: Optional[str] = None,
        hub_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0
    ):
        """
        Get recent tool executions with optional filtering.

        Args:
            db: Database session
            tool_type: Filter by tool type
            hub_id: Filter by hub ID
            limit: Max results to return
            offset: Results offset for pagination

        Returns:
            List of ToolExecution records
        """
        query = db.query(ToolExecution)

        if tool_type:
            query = query.filter(ToolExecution.tool_type == tool_type)
        if hub_id:
            query = query.filter(ToolExecution.hub_id == hub_id)

        return query.order_by(ToolExecution.created_at.desc()).offset(offset).limit(limit).all()

    @staticmethod
    def get_execution_stats(
        db: Session,
        tool_type: Optional[str] = None,
        hub_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get execution statistics.

        Args:
            db: Database session
            tool_type: Filter by tool type
            hub_id: Filter by hub ID

        Returns:
            Dict with statistics (total, success_count, error_count, avg_time, total_tokens)
        """
        from sqlalchemy import func, case

        query = db.query(
            func.count(ToolExecution.id).label('total'),
            func.sum(case((ToolExecution.status == 'success', 1), else_=0)).label('success_count'),
            func.sum(case((ToolExecution.status == 'error', 1), else_=0)).label('error_count'),
            func.avg(ToolExecution.execution_time_ms).label('avg_time_ms'),
            func.sum(ToolExecution.tokens_used).label('total_tokens')
        )

        if tool_type:
            query = query.filter(ToolExecution.tool_type == tool_type)
        if hub_id:
            query = query.filter(ToolExecution.hub_id == hub_id)

        result = query.first()

        return {
            'total': result.total or 0,
            'success_count': result.success_count or 0,
            'error_count': result.error_count or 0,
            'avg_time_ms': round(result.avg_time_ms or 0, 2),
            'total_tokens': result.total_tokens or 0,
            'success_rate': round((result.success_count or 0) / max(result.total or 1, 1) * 100, 2)
        }


class ExecutionTracker:
    """Helper class for track_execution context manager."""

    def __init__(self):
        self.output_data: Optional[Dict[str, Any]] = None
        self.tokens_used: Optional[int] = None

    def set_output(self, data: Dict[str, Any]):
        """Set output data for the execution."""
        self.output_data = data

    def set_tokens(self, tokens: int):
        """Set tokens used for the execution."""
        self.tokens_used = tokens
