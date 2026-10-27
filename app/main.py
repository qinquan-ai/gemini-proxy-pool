"""装配层：只负责把 app 与各路由模块组装起来。

业务实现分布：
    runtime        进程级状态（app、Key 池、trace、鉴权、HTTP 客户端）
    scheduling     http 参数解析与候选构造
    producer       请求体构造
    execution      错误分类与响应解析
    consumer       settlement 与 retry 执行
    api            native / openai / admin 三组路由

对外仍以 app.main 为入口，故在此重导出历史符号，避免调用方与测试改动。
"""

from app.runtime import (  # noqa: F401
    REQUEST_TRACES,
    app,
    key_pool,
    record_trace,
)
from app.scheduling.policy import (  # noqa: F401
    build_route_candidates,
    extract_fallback_models,
    extract_route_mode,
    normalize_model,
)
from app.producer.request_spec import build_generation_config  # noqa: F401
from app.execution.classify import (  # noqa: F401
    extract_response_parts,
    is_allowed_native_gemini_path,
    retry_after_seconds,
    usage_metadata_from_sse_tail,
)
from app.api import admin, native, openai  # noqa: F401

__all__ = ["app"]
