import logging
from datetime import datetime
from typing import List, Dict, Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# models.py에서 ChatLog 모델 임포트 시도
try:
    from models import ChatLog
except ImportError:
    import sys
    import os
    # 프로젝트 루트를 기준으로 경로 추가 (조정 필요할 수 있음)
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from models import ChatLog

logger = logging.getLogger("crud.chat_log")

async def create_chat_log(
    db: AsyncSession, 
    session_id: str, 
    user_id: str, 
    message_type: str, # 'user' 또는 'assistant'
    message: str,
    task: Optional[str] = None, # 선택적 task 정보
    company_code: int = 0 # 회사 코드
) -> ChatLog:
    """새로운 채팅 로그를 데이터베이스에 저장합니다."""
    
    # type 필드에 대한 유효성 검사 또는 기본값 설정 (선택 사항)
    valid_types = ['user', 'assistant', 'system', 'tool'] # 허용되는 타입 예시
    if message_type not in valid_types:
        logger.warning(f"Invalid message_type '{message_type}' provided. Defaulting to 'unknown'.")
        # message_type = 'unknown' # 또는 오류 발생
        # 여기서는 일단 받은 그대로 저장

    new_log = ChatLog(
        session_id=session_id,
        user_id=user_id,
        type=message_type, # 모델의 컬럼명 사용
        message=message,
        task=task,
        company_code=company_code
        # reg_date는 모델의 default 값 사용
    )
    db.add(new_log)
    # commit은 호출부(API 핸들러)에서 수행
    await db.flush() # ID 등 자동 생성 값 즉시 로드 위해
    await db.refresh(new_log) # DB 상태 반영 (선택 사항)
    logger.info(f"Chat log created - Session: {session_id}, User: {user_id}, Type: {message_type}")
    return new_log

async def get_chat_logs_by_session(
    db: AsyncSession, 
    session_id: str
    # limit 파라미터 제거 (또는 매우 큰 기본값으로 설정하거나, 별도 함수 고려)
) -> List[ChatLog]:
    """주어진 세션 ID에 대한 모든 채팅 로그 목록을 시간 오름차순으로 가져옵니다."""
    result = await db.execute(
        select(ChatLog)
        .where(ChatLog.session_id == session_id)
        .order_by(ChatLog.reg_date.asc()) # 시간 오름차순으로 정렬
        # .limit(limit) # limit 제거
    )
    logs = result.scalars().all()
    # return logs[::-1] # DB에서 이미 오름차순 정렬했으므로 뒤집을 필요 없음
    return logs

# 필요에 따라 다른 CRUD 함수 추가 가능 (예: 특정 기간 로그 조회 등) 