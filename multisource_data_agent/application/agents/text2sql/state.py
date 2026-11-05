"""
Text2SQL State Definitions

文本到 SQL 工作流使用到的 TypedDict 定义。
"""

import operator
from typing import Annotated, Any, Dict, List, Optional, TypedDict


class Text2SQLState(TypedDict, total=False):
    """Workflow state tracked across Text2SQL LangGraph nodes."""

    # 输入
    question: str
    connection_id: Optional[int]
    connection_string: Optional[str]
    db_type: Optional[str]
    max_rows: Optional[int]
    sql_timeout_ms: Optional[int]

    # Schema 检索
    schema_context: Dict[str, Any]
    value_mappings: Dict[str, Dict[str, str]]
    mappings_str: str

    # 查询分析
    analysis: Optional[Dict[str, Any]]
    analysis_text: Optional[str]

    # SQL 生成与验证
    sql_statement: str
    is_valid: bool
    # Latest validation report. These intentionally replace on each repair
    # attempt so a successful repair does not retain stale error codes.
    validation_errors: List[str]
    validation_error_codes: List[str]
    validation_warnings: List[str]
    validation_retryable: bool
    referenced_tables: List[str]
    referenced_columns: Dict[str, List[str]]

    # 执行阶段
    execution_results: Optional[List[Dict[str, Any]]]
    execution_error: Optional[str]
    execution_error_code: Optional[str]
    execution_retryable: bool
    row_limit_applied: bool
    requested_limit: Optional[int]
    effective_limit: int
    sql_timeout_ms: int
    timeout_supported: bool

    # 可视化与解释
    visualization: Optional[Dict[str, Any]]
    explanation: Optional[str]

    # 工作流控制
    steps: Annotated[List[str], operator.add]
    # retry_count/max_retries are kept for compatibility with older callers.
    # Phase 3 orchestration uses the bounded repair_count/max_repairs pair.
    retry_count: int
    max_retries: int
    repair_count: int
    max_repairs: int
    repair_reason: Optional[str]

    # 最终输出
    answer: str
    visualization_config: Optional[Dict[str, Any]]


class Text2SQLInputState(TypedDict):
    """Input payload for the Text2SQL workflow."""

    question: str
    connection_id: Optional[int]
    connection_string: Optional[str]
    max_retries: Optional[int]
    max_repairs: Optional[int]
    repair_count: Optional[int]
    db_type: Optional[str]
    max_rows: Optional[int]
    sql_timeout_ms: Optional[int]


class Text2SQLOutputState(TypedDict):
    """Workflow output returned to callers."""

    answer: str
    sql_statement: Optional[str]
    execution_results: Optional[List[Dict[str, Any]]]
    execution_error: Optional[str]
    explanation: Optional[str]
    visualization: Optional[Dict[str, Any]]
    visualization_config: Optional[Dict[str, Any]]
    validation_errors: List[str]
    validation_error_codes: List[str]
    validation_warnings: List[str]
    validation_retryable: bool
    referenced_tables: List[str]
    referenced_columns: Dict[str, List[str]]
    row_limit_applied: bool
    requested_limit: Optional[int]
    effective_limit: int
    sql_timeout_ms: int
    timeout_supported: bool
    execution_error_code: Optional[str]
    execution_retryable: bool
    repair_count: int
    max_repairs: int
    repair_reason: Optional[str]
    steps: List[str]
